#!/usr/bin/env python3
"""Connect the owner's third-party apps (Station connectors) from chat, and
resume their request once the apps are connected.

  find <query...>                   the catalog app whose slug or name is exactly <query>
  status [slug...] [--room R]       the owner's connections, pending and recently resumed waits
  await <slug...> --room R --reply-to E --task T --owner O (--request TEXT | --request-file PATH|-)
        [--private --line TEXT ...]  record a wait (merged into the room's pending waits for the
                                    same apps and AG2 Cloud account) and start its detached waiter;
                                    --private shows the card and the lines only to the owner, under
                                    message E, instead of in the room
  note <wait-id> TEXT               add a line to the wait's private card
  claim <room> [--force]            claim the room's pending waits whose apps are connected, each
                                    with whether its AG2 Cloud account checks out
  verify-account <wait-id>          is the claimed wait's AG2 Cloud account the one signed in and
                                    the one the running core's station acts as?
  rearm                             restart the waiter of every unclaimed wait that has none

Output is one JSON object on stdout. Exit codes: 0 ok; 1 a negative answer (no
exact match, not every app connected, nothing claimed, the account not the
wait's or unknown); 2 a setup problem to relay, not retry (not signed in,
connectors disabled, unknown app, too many apps, bad arguments, not the owner's
own AG2 Space task, cloud unreachable).

A wait is `<workspace>/state/connect-waits/<wait-id>.json`. Claiming it is a
rename to `<wait-id>.claimed`, so exactly one of the waiter, the deadline,
`claim` and a superseding `await` wins, and only the winner acts. The claimed
record keeps who claimed it and when, so `claim` and `status` can report that a
room's request is already answered or being answered. The waiter writes one
resume task, `tasks/task-connect-<wait-id>.txt`, which the core processes like
any other. Only a live owner-tier AG2 Space task can record a wait, and one task
has at most one wait per room. A wait whose AG2 Cloud account is unknown never
answers with data (outcome `unverified`), and waits of different accounts are
never merged; a `connected` resume runs `verify-account` and `claim` reports each
wait's account verdict, because the account signed in, or the one the running
core's station was started for, may no longer be the wait's.

A private card is the owner-only face of a wait asked from a room with other
people: one record in `<workspace>/state/connect-cards.json`, which the desktop
client reads over the engine's loopback media route and draws inside the "Only
visible to you" card under message `reply_to`. It never becomes a Matrix event,
so no other member of the room receives it. The record carries the apps, the
lines the agent says about connecting and the wait's status; never the request.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))
import cloud_auth  # noqa: E402
import local_task_protocol as ltp  # noqa: E402

from station_stamp import read_station_stamp  # noqa: E402
from task_body_guard import confine_user_content, header_safe_value  # noqa: E402

EXIT_OK, EXIT_NO, EXIT_SETUP = 0, 1, 2
WAIT_S = 30 * 60
POLL_S = 3.0
# A wait whose deadline passed this long ago is dropped silently: an answer or
# a timeout note hours later (a Mac that slept through it) reads as a glitch.
STALE_GRACE_S = 6 * 3600
PRUNE_S = 7 * 86400
# A wait claimed this recently is reported by `claim` and `status`, so an owner's "done"
# right after the waiter fired is not answered a second time.
RECENT_S = WAIT_S
MAX_TOOLKITS = 5
MAX_REQUEST_CHARS = 500
MAX_REQUEST_READ = 64 * 1024
# Pauses between the tries of reading the account a new wait is made under.
ACCOUNT_RETRY_S = (0.5, 1.0)
SLUG_RE = re.compile(r"^[a-z0-9_]{1,64}\Z")
WAIT_ID_RE = re.compile(r"^[0-9]{13}-[0-9a-f]{8}\Z")
# Room ids from room version 12 carry no ":server" part.
ROOM_RE = re.compile(r"^!\S{1,255}\Z")
MXID_RE = re.compile(r"^@[^\s:]+:[^\s]+\Z")
TOKEN_RE = re.compile(r"^\S{1,255}\Z")
RESUME_SOURCE = "connector-resume"
ORIGIN_SOURCE = "ag2space"
# claimed_by values whose request is answered (or the owner told why not).
WAITER_OUTCOMES = ("connected", "timeout", "user_changed", "unverified")
RESUMED_BY = WAITER_OUTCOMES + ("claim",)
CLAIM_KEYS = ("claimed_by", "claimed_at")
# Private cards: the line cap keeps a runaway loop from growing the card; the text cap matches the client's.
CARDS_VERSION = 1
MAX_CARD_LINES = 20
MAX_LINE_CHARS = 300
MAX_AWAIT_LINES = 3
CARD_PRUNE_S = 86400
# claimed_by -> the card's status. `invalid` leaves the card as it was: there is no wait to describe.
CARD_STATUS = {"connected": "connected", "timeout": "timeout", "user_changed": "user_changed",
               "unverified": "unverified", "claim": "claimed", "superseded": "superseded", "expired": "expired"}


class Setup(Exception):
    """A problem the owner or the agent has to fix; exit 2."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


# --------------------------------------------------------------------------- cloud


class Cloud:
    """The owner's cloud session. Auth is re-read while missing, so a waiter
    started before a sign-in picks the session up once it exists."""

    def __init__(
        self,
        workspace: Path,
        read_auth: Callable[[Path], tuple] = cloud_auth.read_cloud_auth,
        request: Callable[..., Any] = cloud_auth.cloud_request,
    ) -> None:
        self.workspace = workspace
        self._read_auth = read_auth
        self._request = request
        self.base: str | None = None
        self.token: str | None = None

    def signed_in(self) -> bool:
        if not self.token:
            self.base, self.token = self._read_auth(self.workspace)
        return bool(self.token)

    def get(self, path: str) -> dict:
        if not self.signed_in():
            raise Setup("not_signed_in", "Not signed in to AG2 Cloud: sign in from the desktop app.")
        try:
            data = self._request(self.base or cloud_auth.DEFAULT_CLOUD_ORIGIN, self.token, "GET", path)
        except cloud_auth.CloudError as exc:
            if exc.status == 401:
                self.token = None
            raise
        except (OSError, ValueError) as exc:
            # cloud_request leaves errors raised while reading a response (a reset, a read timeout) unwrapped.
            raise cloud_auth.CloudError(0, "network", str(exc)) from None
        return data if isinstance(data, dict) else {}

    def user_id(self) -> str | None:
        """The signed-in account's id, with auth re-read so a sign-in as someone else is seen."""
        self.token = None
        return str(self.get("/api/me").get("id") or "") or None

    def active_toolkits(self) -> set[str]:
        rows = self.get("/api/connectors").get("connections") or []
        return {
            str(r.get("toolkit") or "").lower()
            for r in rows
            if isinstance(r, dict) and str(r.get("status") or "").lower() == "active"
        }

    def connector_search(self, query: str) -> list[dict]:
        # q is a substring filter over slug, name and description, so a slug needs a wide page.
        data = self.get(f"/api/station/catalog?kind=connector&limit=100&q={urllib.parse.quote(query)}")
        if not (data.get("enabledKinds") or {}).get("connector", True):
            raise Setup("connectors_disabled", "Connected apps are not enabled on this AG2 Cloud.")
        return [i for i in data.get("items") or [] if isinstance(i, dict)]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def app_row(item: dict) -> dict:
    return {
        "toolkit": item.get("slug"),
        "name": item.get("name") or item.get("slug"),
        "icon_url": item.get("iconUrl"),
        "connected": bool(item.get("acquired")),
        "auth_mode": item.get("authMode"),
        "coming_soon": bool(item.get("comingSoon")),
    }


def exact_app(items: list[dict], query: str) -> dict | None:
    """The item whose slug is the query (spaces and case ignored), else whose name is."""
    want = _norm(query)
    for key in ("slug", "name"):
        for item in items:
            if want and _norm(str(item.get(key) or "")) == want:
                return item
    return None


def join_names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


# --------------------------------------------------------------------------- wait files


def waits_dir(ws: Path) -> Path:
    return ws / "state" / "connect-waits"


def marker_path(ws: Path, wait_id: str) -> Path:
    return waits_dir(ws) / f"{wait_id}.json"


def claimed_path(ws: Path, wait_id: str) -> Path:
    return waits_dir(ws) / f"{wait_id}.claimed"


def lock_path(ws: Path, wait_id: str) -> Path:
    return waits_dir(ws) / f"{wait_id}.lock"


def now_iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def read_marker(ws: Path, wait_id: str) -> dict | None:
    """The unclaimed marker, or None once it is claimed. Raises ValueError when unreadable."""
    try:
        data = json.loads(marker_path(ws, wait_id).read_text())
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(str(exc)) from None
    if not _valid_marker(data, wait_id):
        raise ValueError(f"malformed wait marker {wait_id}")
    return data


def _valid_marker(data: Any, wait_id: str) -> bool:
    if not isinstance(data, dict) or data.get("wait_id") != wait_id:
        return False
    toolkits = data.get("toolkits")
    return (
        all(isinstance(data.get(k), str) and data[k] for k in ("room", "reply_to", "task", "owner"))
        and isinstance(data.get("deadline"), (int, float))
        and isinstance(toolkits, list)
        and bool(toolkits)
        and all(isinstance(t, dict) and SLUG_RE.match(str(t.get("slug") or "")) for t in toolkits)
    )


def list_markers(ws: Path) -> list[dict]:
    out = []
    d = waits_dir(ws)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        if not WAIT_ID_RE.match(p.stem):
            continue
        try:
            marker = read_marker(ws, p.stem)
        except ValueError:
            continue
        if marker is not None:
            out.append(marker)
    return out


def write_marker(ws: Path, marker: dict) -> Path:
    waits_dir(ws).mkdir(parents=True, exist_ok=True)
    final = marker_path(ws, marker["wait_id"])
    _write_json(final, marker)
    return final


def claim(ws: Path, wait_id: str, by: str = "claim", at: float | None = None) -> dict | None:
    """Atomically take the wait. The claimed record, stamped with who took it and when,
    or None when someone else won."""
    try:
        os.rename(marker_path(ws, wait_id), claimed_path(ws, wait_id))
    except FileNotFoundError:
        return None
    data = _read_json(claimed_path(ws, wait_id))
    if not _valid_marker(data, wait_id):
        return {"wait_id": wait_id, "invalid": True}
    record = {**data, "claimed_by": by, "claimed_at": time.time() if at is None else at}
    try:
        _write_json(claimed_path(ws, wait_id), record)
    except OSError:
        pass  # the rename already decided the claim; only its report is lost
    if data.get("private") and by in CARD_STATUS:
        set_card(ws, wait_id, status=CARD_STATUS[by], at=record["claimed_at"])
    return record


def release(ws: Path, record: dict) -> bool:
    """Undo a claim whose resume task could not be written, so the next poll or `rearm` retries."""
    wait_id = record["wait_id"]
    try:
        _write_json(claimed_path(ws, wait_id), {k: v for k, v in record.items() if k not in CLAIM_KEYS})
    except OSError:
        pass
    try:
        os.rename(claimed_path(ws, wait_id), marker_path(ws, wait_id))
    except OSError:
        return False
    if record.get("private"):
        set_card(ws, wait_id, status="waiting")
    return True


def summary(marker: dict) -> dict:
    keys = ("wait_id", "toolkits", "room", "reply_to", "request", "task", "owner", "deadline_at")
    return {**{k: marker.get(k) for k in keys}, "private": bool(marker.get("private"))}


# --------------------------------------------------------------------------- private cards


def cards_path(ws: Path) -> Path:
    return ws / "state" / "connect-cards.json"


@contextlib.contextmanager
def cards_locked(ws: Path):
    """Every reader-modifier-writer of the cards file holds this, so the waiter, `claim`, `await`
    and `note` never lose each other's change. Blocking: each hold is one small rewrite."""
    path = cards_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name("connect-cards.lock"), "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield path


def _valid_card(card: Any) -> bool:
    return (
        isinstance(card, dict)
        and isinstance(card.get("id"), str) and bool(WAIT_ID_RE.match(card["id"]))
        and isinstance(card.get("room"), str) and bool(ROOM_RE.match(card["room"]))
        and isinstance(card.get("event"), str) and bool(TOKEN_RE.match(card["event"]))
        and isinstance(card.get("for"), str) and bool(MXID_RE.match(card["for"]))
        and isinstance(card.get("toolkits"), list)
        and isinstance(card.get("lines"), list)
    )


def read_cards(ws: Path) -> list[dict]:
    """The valid cards on disk; a missing, torn or foreign file reads as none, and is rebuilt on write."""
    data = _read_json(cards_path(ws))
    cards = data.get("cards") if isinstance(data, dict) else None
    return [c for c in cards if _valid_card(c)] if isinstance(cards, list) else []


def _write_cards(path: Path, cards: list[dict]) -> None:
    _write_json(path, {"version": CARDS_VERSION, "cards": cards})


def _card_line(text: str, at: float) -> dict:
    return {"ts": round(at, 3), "text": " ".join(str(text).split())[:MAX_LINE_CHARS]}


def put_card(ws: Path, marker: dict, lines: list[str], at: float, into_of: list[str] | None = None) -> None:
    """Write (or replace) the wait's card; the cards of the waits it absorbed point at it."""
    card = {
        "id": marker["wait_id"],
        "room": marker["room"],
        "event": marker["reply_to"],
        "for": marker["owner"],
        "toolkits": [{"slug": t["slug"], "name": t.get("name") or t["slug"]} for t in marker["toolkits"]],
        "lines": [_card_line(x, at) for x in lines if str(x).strip()][:MAX_CARD_LINES],
        "status": "waiting",
        "created": round(at, 3),
        "updated": round(at, 3),
    }
    with cards_locked(ws) as path:
        cards = [c for c in read_cards(ws) if c["id"] != card["id"]]
        for c in cards:
            if c["id"] in (into_of or []):
                c.update(status="superseded", into=card["id"], updated=round(at, 3))
        _write_cards(path, cards + [card])


def set_card(ws: Path, wait_id: str, status: str | None = None, line: str | None = None,
             at: float | None = None) -> dict | None:
    """Update the wait's card: its status, one more line, or both. None when it has no card."""
    at = time.time() if at is None else at
    try:
        with cards_locked(ws) as path:
            cards = read_cards(ws)
            card = next((c for c in cards if c["id"] == wait_id), None)
            if card is None:
                return None
            if status:
                card["status"] = status
                if status == "waiting":
                    card.pop("into", None)  # a released merge gives the card back its own wait
            if line and line.strip():
                card["lines"] = (card["lines"] + [_card_line(line, at)])[-MAX_CARD_LINES:]
            card["updated"] = round(at, 3)
            _write_cards(path, cards)
            return card
    except OSError as exc:
        # A card that could not be updated must never cost the claim or the resume.
        print(f"connect-apps: private card {wait_id} not updated: {exc}", file=sys.stderr)
        return None


def prune_cards(ws: Path, now: float) -> int:
    """Drop cards whose wait is no longer pending and that nothing touched for a day."""
    if not cards_path(ws).exists():
        return 0
    with cards_locked(ws) as path:
        cards = read_cards(ws)
        keep = [c for c in cards
                if marker_path(ws, c["id"]).exists() or now - float(c.get("updated") or 0) < CARD_PRUNE_S]
        if len(keep) != len(cards):
            _write_cards(path, keep)
        return len(cards) - len(keep)


def resumed_summary(ws: Path, record: dict) -> dict:
    by = record.get("claimed_by")
    task = f"task-connect-{record['wait_id']}" if by in WAITER_OUTCOMES else None
    return {
        **summary(record),
        "claimed_by": by,
        "claimed_at": now_iso(float(record.get("claimed_at") or 0)),
        "resume_task": task,
        "resume_pending": task is not None and (ws / "tasks" / f"{task}.txt").exists(),
    }


def recent_claims(ws: Path, room: str | None, now: float) -> list[dict]:
    """Waits (of `room`, or every room) claimed in the last RECENT_S whose request is
    answered or being answered."""
    out: list[dict] = []
    d = waits_dir(ws)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.claimed")):
        data = _read_json(p) if WAIT_ID_RE.match(p.stem) else None
        if not _valid_marker(data, p.stem) or (room is not None and data.get("room") != room):
            continue
        at = data.get("claimed_at")
        if data.get("claimed_by") in RESUMED_BY and isinstance(at, (int, float)) and now - at < RECENT_S:
            out.append(resumed_summary(ws, data))
    return out


def _header_values(text: str, key: str) -> list[str]:
    prefix = f"{key}:"
    return [ln[len(prefix):].strip() for ln in text.splitlines() if ln.startswith(prefix)]


def origin_owner(ws: Path, task: str) -> str:
    """The owner mxid of `task` when it is a live owner-tier, non-collaborator AG2 Space task.
    Anything else raises not_owner_task: only the owner's own request may arm an owner resume."""

    def refuse(why: str) -> Setup:
        return Setup("not_owner_task", f"{task} {why}: connect waits are only for the owner's own AG2 Space requests.")

    try:
        text = (ws / "tasks" / f"{task}.txt").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise refuse("is not a live task in this workspace") from None
    above_body = ltp.parse_task_headers(text).headers
    if above_body.get("id") != task:
        raise refuse("does not carry its own id")
    # The gateway writes source above the body and one-lines every value; any other
    # header line anywhere in the file counts against the task, never for it.
    if above_body.get("source") != ORIGIN_SOURCE or set(_header_values(text, "source")) != {ORIGIN_SOURCE}:
        raise refuse("is not from AG2 Space")
    if _header_values(text, "collaborator"):
        raise refuse("is a collaborator task")
    tiers = {ltp.canonical_access_tier(v) for v in _header_values(text, "access_tier")}
    if tiers != {"owner"}:
        raise refuse("is not owner tier")
    users = set(_header_values(text, "user_id"))
    if len(users) != 1 or not MXID_RE.match(next(iter(users))):
        raise refuse("has no single user_id")
    try:
        import task_envelope  # noqa: PLC0415

        verdict = task_envelope.verify_text(text, ws).get("verdict")
    except Exception:  # noqa: BLE001 — an unverifiable stamp is soak-mode telemetry, not a refusal
        verdict = None
    if verdict == "invalid":
        raise refuse("has an envelope stamp that does not verify")
    return next(iter(users))


def acquire_lock(ws: Path, wait_id: str):
    """An exclusive, non-blocking hold on the wait's lock file; None when a live waiter has it."""
    waits_dir(ws).mkdir(parents=True, exist_ok=True)
    fh = open(lock_path(ws, wait_id), "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def waiter_alive(ws: Path, wait_id: str) -> bool:
    fh = acquire_lock(ws, wait_id)
    if fh is None:
        return True
    fh.close()
    return False


# --------------------------------------------------------------------------- resume task


def resume_text(marker: dict, outcome: str) -> str:
    if marker.get("private"):
        return private_resume_text(marker, outcome)
    names = [str(t.get("name") or t.get("slug")) for t in marker.get("toolkits") or []]
    apps = join_names(names)
    wait_id, room, reply_to = marker["wait_id"], marker["room"], marker["reply_to"]
    origin = f"(wait {wait_id}, from task {marker['task']})"
    request = header_safe_value(marker.get("request") or "")
    is_are = "is" if len(names) == 1 else "are"
    if outcome == "connected":
        text = (
            f"{apps} {is_are} now connected {origin}. "
            f'Answer the owner\'s earlier request "{request}" in room {room} (reply_to {reply_to}). '
            f"First run the connect-apps helper: connectors.py verify-account {wait_id}. Only if it exits 0, "
            f"get the data with composio_exec and post the answer with room.message.send "
            f"(operation_id {wait_id}:answer). Otherwise share no data: tell the owner you didn't check "
            f"their {apps} because a different or unconfirmed AG2 Cloud account is signed in now, and to "
            f"ask again once the right account is (room.message.send, operation_id {wait_id}:account). "
            "Either way write the result [no-send]. Follow the connect-apps skill, Resume."
        )
    elif outcome == "unverified":
        text = (
            f"{apps} {is_are} now connected {origin}, but the AG2 Cloud account this wait was made under "
            f'could not be confirmed, so the owner\'s earlier request "{request}" was not answered. '
            f"In room {room} (reply_to {reply_to}), tell the owner {apps} {is_are} connected now but you "
            "couldn't confirm which AG2 Cloud account asked, so you didn't check it: ask again to get the "
            f"answer. Post it with room.message.send (operation_id {wait_id}:unverified), then write the "
            "result [no-send]. Share no data from any app. Follow the connect-apps skill, Resume."
        )
    elif outcome == "user_changed":
        text = (
            f"While waiting for {apps} {origin}, the AG2 Cloud account signed in on this Mac changed, "
            f'so the owner\'s earlier request "{request}" was not answered. In room {room} '
            f"(reply_to {reply_to}), tell the owner you didn't check their {apps} because a different "
            "AG2 Cloud account is signed in now, and to ask again once the right account is. "
            f"Post it with room.message.send (operation_id {wait_id}:account), then write the result "
            "[no-send]. Share no data from the new account. Follow the connect-apps skill, Resume."
        )
    else:
        text = (
            f"Connecting {apps} did not finish within {WAIT_S // 60} minutes {origin}. "
            f'The owner\'s earlier request was "{request}". In room {room} (reply_to {reply_to}), '
            "tell the owner it timed out: tap Connect on the card again and tell me once it's connected. "
            f"Post it with room.message.send (operation_id {wait_id}:timeout), then write the result "
            "[no-send]. Follow the connect-apps skill, Resume."
        )
    return confine_user_content(text)


def private_resume_text(marker: dict, outcome: str) -> str:
    """The resume of a wait asked from a room with other people: everything about connecting goes to
    the owner's private card (`note`), never to the room; only the answer itself is posted there."""
    names = [str(t.get("name") or t.get("slug")) for t in marker.get("toolkits") or []]
    apps = join_names(names)
    wait_id, room, reply_to = marker["wait_id"], marker["room"], marker["reply_to"]
    origin = f"(wait {wait_id}, from task {marker['task']}, private card)"
    request = header_safe_value(marker.get("request") or "")
    is_are = "is" if len(names) == 1 else "are"
    note = f"connectors.py note {wait_id}"
    private = (
        "This wait was asked from a room with other people, so anything about connecting, sign-in or "
        f"accounts goes only to the owner's private card with `{note} \"<text>\"`, never to the room or the DM. "
    )
    if outcome == "connected":
        text = (
            f"{apps} {is_are} now connected {origin}. {private}"
            f"First run the connect-apps helper: connectors.py verify-account {wait_id}. If it exits 0, "
            f'add the note "{apps} {is_are} connected. On it." and do the owner\'s earlier request "{request}": '
            f"get it done with composio_exec and reply in room {room} with room.message.send (reply_to "
            f"{reply_to}, operation_id {wait_id}:answer), except that private content (mail, calendar events, "
            "files, messages, contacts) goes to the owner's DM with only a one-line pointer in the room, as "
            "the connect-apps skill says. Any other exit: share no data and post nothing in the room; add the "
            f"note that you didn't use their {apps} because a different or unconfirmed AG2 Cloud account is "
            "signed in now, and to ask again once the right account is. "
            "Either way write the result [no-send]. Follow the connect-apps skill, Resume."
        )
    elif outcome == "unverified":
        text = (
            f"{apps} {is_are} now connected {origin}, but the AG2 Cloud account this wait was made under "
            f'could not be confirmed, so the owner\'s earlier request "{request}" was not done. {private}'
            f"Add the note that {apps} {is_are} connected now but you couldn't confirm which AG2 Cloud account "
            "asked, so you didn't use it: ask again to get it done. Post nothing in the room, share no data, "
            "and write the result [no-send]. Follow the connect-apps skill, Resume."
        )
    elif outcome == "user_changed":
        text = (
            f"While waiting for {apps} {origin}, the AG2 Cloud account signed in on this Mac changed, "
            f'so the owner\'s earlier request "{request}" was not done. {private}'
            f"Add the note that you didn't use their {apps} because a different AG2 Cloud account is signed in "
            "now, and to ask again once the right account is. Post nothing in the room, share no data from the "
            "new account, and write the result [no-send]. Follow the connect-apps skill, Resume."
        )
    else:
        text = (
            f"Connecting {apps} did not finish within {WAIT_S // 60} minutes {origin}. "
            f'The owner\'s earlier request was "{request}". {private}'
            "Add the note that it timed out: tap Connect again and ask me once it's connected. Post nothing in "
            "the room and write the result [no-send]. Follow the connect-apps skill, Resume."
        )
    return confine_user_content(text)


def _stamp(text: str, ws: Path) -> str:
    try:
        import task_envelope  # noqa: PLC0415

        return task_envelope.stamp_text(text, ws)
    except Exception:  # noqa: BLE001 — a stamping error costs the stamp, never the task
        return text


def write_resume_task(ws: Path, marker: dict, outcome: str, now: float) -> Path:
    task_id = f"task-connect-{marker['wait_id']}"
    headers = [
        ("id", task_id),
        ("timestamp", now_iso(now)),
        ("source", RESUME_SOURCE),
        ("interaction_type", "message"),
        ("channel_id", header_safe_value(marker["room"])),
        ("user_id", header_safe_value(marker["owner"])),
        ("access_tier", "owner"),
        ("priority", "normal"),
    ]
    ltp.set_task_stamper(lambda text: _stamp(text, ws))
    try:
        return ltp.write_task_file(ws / "tasks", task_id, headers, resume_text(marker, outcome))
    finally:
        ltp.set_task_stamper(None)


# --------------------------------------------------------------------------- waiter


def account_outcome(cloud: Cloud, marker: dict) -> str | None:
    """connected when the signed-in account is the one the wait was made under, user_changed
    when it isn't, unverified when the wait never knew its account, None when it can't be checked now."""
    want = marker.get("cloud_user_id")
    if not want:
        return "unverified"
    try:
        current = cloud.user_id()
    except (cloud_auth.CloudError, Setup, OSError, ValueError):
        return None
    if not current:
        return None
    return "connected" if current == want else "user_changed"


def run_waiter(
    ws: Path,
    wait_id: str,
    cloud: Cloud,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Poll until every app is active or the deadline passes, then claim and
    write the resume task. Returns connected | timeout | user_changed | unverified | expired |
    invalid | claimed_elsewhere | write_failed | busy (another waiter holds the lock)."""
    lock = acquire_lock(ws, wait_id)
    if lock is None:
        return "busy"
    try:
        lock.seek(0)
        lock.truncate()
        lock.write(f"{os.getpid()}\n")
        lock.flush()
        while True:
            try:
                marker = read_marker(ws, wait_id)
            except ValueError:
                return "invalid" if claim(ws, wait_id, "invalid", now()) is not None else "claimed_elsewhere"
            if marker is None:
                return "claimed_elsewhere"
            t = now()
            deadline = float(marker.get("deadline") or 0)
            if t >= deadline + STALE_GRACE_S:
                return "expired" if claim(ws, wait_id, "expired", t) is not None else "claimed_elsewhere"
            slugs = [str(x.get("slug")) for x in marker.get("toolkits") or []]
            try:
                active = cloud.active_toolkits()
                connected = bool(slugs) and all(s in active for s in slugs)
            except (cloud_auth.CloudError, Setup, OSError, ValueError):
                connected = False
            outcome = account_outcome(cloud, marker) if connected else None
            if outcome is None and t >= deadline:
                outcome = "timeout"
            if outcome is not None:
                won = claim(ws, wait_id, outcome, now())
                if won is None:
                    return "claimed_elsewhere"
                if won.get("invalid"):
                    return "invalid"
                try:
                    write_resume_task(ws, won, outcome, now())
                    return outcome
                except (OSError, ValueError) as exc:
                    print(f"connect-apps: resume task for {wait_id} not written, retrying: {exc}", file=sys.stderr)
                    if not release(ws, won):
                        return "write_failed"
            sleep(POLL_S)
    finally:
        lock.close()


def spawn_waiter(ws: Path, wait_id: str) -> int:
    """Start a waiter in its own session, so it outlives the core that asked for it."""
    logs = ws / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    with open(logs / "connect-waits.log", "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH), "--workspace", str(ws), "waiter", wait_id],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(ws),
            start_new_session=True,
            close_fds=True,
        )
    return proc.pid


def _try_spawn(spawn: Callable[[Path, str], int], ws: Path, wait_id: str) -> int | None:
    """The waiter's pid, or None when it could not start; the wait stays on disk for `rearm`."""
    try:
        return spawn(ws, wait_id)
    except OSError as exc:
        print(f"connect-apps: could not start the waiter for {wait_id}: {exc}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------- commands


def emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2))


def cmd_find(ws: Path, cloud: Cloud, args: argparse.Namespace, **_: Any) -> int:
    query = " ".join(args.query).strip()
    items = cloud.connector_search(query)
    match = exact_app(items, query)
    suggestions = [app_row(i) for i in items if i is not match][:5]
    emit({"match": app_row(match) if match else None, "suggestions": suggestions})
    return EXIT_OK if match else EXIT_NO


def cmd_status(
    ws: Path, cloud: Cloud, args: argparse.Namespace, now: Callable[[], float] = time.time, **_: Any
) -> int:
    rows = cloud.get("/api/connectors").get("connections") or []
    connections = [
        {"toolkit": r.get("toolkit"), "name": r.get("name"), "status": r.get("status")}
        for r in rows
        if isinstance(r, dict)
    ]
    active = {str(c["toolkit"]).lower() for c in connections if str(c["status"]).lower() == "active"}
    wanted = [s.lower() for s in args.slugs]
    room = (args.room or "").strip() or None
    apps = [{"toolkit": s, "connected": s in active} for s in wanted]
    payload = {
        "connections": connections,
        "apps": apps,
        "all_connected": all(a["connected"] for a in apps),
        "pending_waits": [summary(m) for m in list_markers(ws) if room is None or m.get("room") == room],
        "resumed_waits": recent_claims(ws, room, now()),
        "private_cards": [{k: c.get(k) for k in ("id", "event", "status", "toolkits")}
                          for c in read_cards(ws) if room is None or c["room"] == room],
    }
    emit(payload)
    return EXIT_OK if payload["all_connected"] else EXIT_NO


def _require(value: str, pattern: re.Pattern, what: str) -> str:
    value = (value or "").strip()
    if not pattern.match(value):
        raise Setup("invalid_arguments", f"{what} is missing or malformed: {value[:80]!r}")
    return value


def read_request(args: argparse.Namespace) -> str:
    if args.request is not None:
        return args.request
    try:
        if args.request_file == "-":
            return sys.stdin.read(MAX_REQUEST_READ)
        with open(args.request_file, encoding="utf-8") as fh:
            return fh.read(MAX_REQUEST_READ)
    except (OSError, UnicodeDecodeError) as exc:
        raise Setup("invalid_arguments", f"--request-file could not be read: {exc}") from None


def merge_requests(requests: list[str]) -> str:
    seen, out = set(), []
    for r in requests:
        if r and _norm(r) not in seen:
            seen.add(_norm(r))
            out.append(r)
    return " / ".join(out)[:MAX_REQUEST_CHARS]


def account_for_wait(cloud: Cloud, sleep: Callable[[float], None]) -> str | None:
    """The signed-in account's id, tried a few times; None when it stays unknown."""
    found = None
    for pause in (0.0, *ACCOUNT_RETRY_S):
        if pause:
            sleep(pause)
        try:
            found = cloud.user_id()
        except (cloud_auth.CloudError, Setup, OSError, ValueError):
            found = None
        if found:
            break
    return found


def cmd_await(
    ws: Path,
    cloud: Cloud,
    args: argparse.Namespace,
    spawn: Callable[[Path, str], int] = spawn_waiter,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    **_: Any,
) -> int:
    slugs = list(dict.fromkeys(s.strip().lower() for s in args.slugs))
    if not 1 <= len(slugs) <= MAX_TOOLKITS or not all(SLUG_RE.match(s) for s in slugs):
        raise Setup("invalid_arguments", f"give 1-{MAX_TOOLKITS} app slugs (a-z, 0-9, _)")
    room = _require(args.room, ROOM_RE, "--room")
    owner = _require(args.owner, MXID_RE, "--owner")
    reply_to = _require(args.reply_to, TOKEN_RE, "--reply-to")
    task = (args.task or "").strip()
    if not ltp.valid_archive_lookup_id(task):
        raise Setup("invalid_arguments", f"--task is missing or malformed: {task[:80]!r}")
    request = header_safe_value(read_request(args)).strip()[:MAX_REQUEST_CHARS]
    if not request:
        raise Setup("invalid_arguments", "--request is empty")
    private = bool(args.private)
    lines = [x for x in (args.line or []) if x.strip()]
    if lines and not private:
        raise Setup("invalid_arguments", "--line only goes on a --private card; in the owner's DM, send the lines as messages")
    if len(lines) > MAX_AWAIT_LINES:
        raise Setup("invalid_arguments", f"give at most {MAX_AWAIT_LINES} --line")
    if origin_owner(ws, task) != owner:
        raise Setup("not_owner_task", f"--owner is not the user of {task}.")
    if not cloud.signed_in():
        raise Setup("not_signed_in", "Not signed in to AG2 Cloud: sign in from the desktop app.")
    cloud_user_id = account_for_wait(cloud, sleep)

    # This task's own waits in the room come first: always reused or merged, so it has one wait per room.
    same_room = sorted((m for m in list_markers(ws) if m.get("room") == room), key=lambda m: m.get("task") != task)
    same_task = [m for m in same_room if m.get("task") == task]
    for marker in same_task:
        if set(slugs) <= {x.get("slug") for x in marker["toolkits"] if isinstance(x, dict)} \
                and bool(marker.get("private")) == private:
            pid = None if waiter_alive(ws, marker["wait_id"]) else _try_spawn(spawn, ws, marker["wait_id"])
            if private and not any(c["id"] == marker["wait_id"] for c in read_cards(ws)):
                put_card(ws, marker, lines, now())
            emit({**summary(marker), "reused": True, "waiter_pid": pid})
            return EXIT_OK
    combined = dict.fromkeys(slugs)
    for marker in same_task:
        combined.update(dict.fromkeys(str(x.get("slug")) for x in marker["toolkits"] if isinstance(x, dict)))
    if len(combined) > MAX_TOOLKITS:
        raise Setup("too_many_apps", f"{task} would wait for {len(combined)} apps; one card lists at most {MAX_TOOLKITS}.")

    toolkits = []
    for slug in slugs:
        item = next((i for i in cloud.connector_search(slug) if i.get("slug") == slug), None)
        if item is None:
            raise Setup("unknown_app", f"No app with the slug {slug!r} in the catalog.")
        if item.get("comingSoon"):
            raise Setup("coming_soon", f"{item.get('name') or slug} is not available yet.")
        toolkits.append({"slug": slug, "name": item.get("name") or slug})

    t = now()
    # Another task's wait in this room is folded in only when it shares an app and was made under
    # this same known account, so asking again gets one answer and no request crosses accounts.
    requests, taken, resumed = [], [], []
    own_handled = False
    for marker in same_room:
        if bool(marker.get("private")) != private:
            continue  # a private card is never folded into a room-visible wait, nor the other way round
        mine = {k["slug"] for k in toolkits}
        theirs = [x for x in marker["toolkits"] if isinstance(x, dict)]
        extra = [x for x in theirs if x.get("slug") not in mine]
        own = marker.get("task") == task
        if not own and (not cloud_user_id or marker.get("cloud_user_id") != cloud_user_id
                        or len(extra) == len(theirs) or len(toolkits) + len(extra) > MAX_TOOLKITS):
            continue
        won = claim(ws, marker["wait_id"], "superseded", t)
        if won is None:
            record = _read_json(claimed_path(ws, marker["wait_id"]))
            by = record.get("claimed_by") if _valid_marker(record, marker["wait_id"]) else None
            if by in (RESUMED_BY if own else ("connected", "claim")):
                resumed.append(resumed_summary(ws, record))
                if own:
                    own_handled = True
                    break
            continue
        if won.get("invalid"):
            continue
        toolkits += [{"slug": x["slug"], "name": x.get("name") or x["slug"]} for x in extra]
        requests.append(str(won.get("request") or ""))
        taken.append(won)
    if own_handled:
        # This task's request already has its answer or note coming: a second wait would answer it twice.
        for won in taken:
            release(ws, won)
        taken = []
    superseded = [w["wait_id"] for w in taken]
    answered = {str(x.get("slug")) for r in resumed for x in r.get("toolkits") or [] if isinstance(x, dict)}
    if own_handled or (resumed and not superseded and set(slugs) <= answered):
        emit({"wait_id": None, "reused": False, "superseded": [], "resumed": resumed, "waiter_pid": None})
        return EXIT_OK

    wait_id = f"{int(t * 1000):013d}-{secrets.token_hex(4)}"
    marker = {
        "version": 1,
        "wait_id": wait_id,
        "toolkits": toolkits,
        "room": room,
        "reply_to": reply_to,
        "request": merge_requests(requests + [request]),
        "task": task,
        "owner": owner,
        "cloud_user_id": cloud_user_id if all(w.get("cloud_user_id") == cloud_user_id for w in taken) else None,
        "superseded": superseded,
        "private": private,
        "created_at": now_iso(t),
        "deadline": int(t + WAIT_S),
        "deadline_at": now_iso(t + WAIT_S),
    }
    try:
        if private:
            # The card first: a waiter must never fire for a wait whose card the owner cannot see yet.
            put_card(ws, marker, lines, t, into_of=superseded)
        path = write_marker(ws, marker)
    except OSError:
        if private:
            set_card(ws, wait_id, status="expired")
        for won in taken:
            release(ws, won)
        raise
    pid = _try_spawn(spawn, ws, wait_id)
    emit({**summary(marker), "marker": str(path), "reused": False, "superseded": superseded,
          "resumed": resumed, "waiter_pid": pid})
    return EXIT_OK


def cmd_claim(
    ws: Path, cloud: Cloud, args: argparse.Namespace, now: Callable[[], float] = time.time, **_: Any
) -> int:
    room = (args.room or "").strip()
    pending = [m for m in list_markers(ws) if m.get("room") == room]
    claimed, waiting = [], []
    if pending:
        active = None if args.force else cloud.active_toolkits()
        ready = [m for m in pending
                 if active is None or all(str(x.get("slug")) in active for x in m.get("toolkits") or [])]
        current = (lambda: None) if args.force else functools.lru_cache(maxsize=None)(cloud.user_id)
        # Every verdict before any claim, so a cloud error leaves every wait armed.
        verdicts = {m["wait_id"]: account_mismatch(ws, m.get("cloud_user_id"), current) for m in ready}
        for marker in pending:
            if marker["wait_id"] not in verdicts:
                waiting.append(marker)
                continue
            won = claim(ws, marker["wait_id"], "claim", now())
            if won is not None and not won.get("invalid"):
                reason = verdicts[marker["wait_id"]]
                claimed.append({**summary(won), "account_ok": reason is None, "account_reason": reason})
    taken = {c["wait_id"] for c in claimed}
    # A wait the waiter claimed meanwhile is no longer pending: it shows under resumed.
    emit({
        "claimed": claimed,
        "pending": [summary(m) for m in waiting if marker_path(ws, m["wait_id"]).exists()],
        "resumed": [r for r in recent_claims(ws, room, now()) if r["wait_id"] not in taken],
    })
    return EXIT_OK if claimed else EXIT_NO


def account_mismatch(ws: Path, want: Any, current: Callable[[], str | None]) -> str | None:
    """None when the wait's account is known, signed in, and the one the running core's station was
    started for (when the desktop stamped one); else account_unknown or account_changed."""
    if not want:
        return "account_unknown"
    stamped = (read_station_stamp(ws) or {}).get("cloud_user_id")
    if stamped and stamped != want:
        return "account_changed"
    found = current()
    if not found:
        return "account_unknown"
    return None if found == want else "account_changed"


def cmd_verify_account(ws: Path, cloud: Cloud, args: argparse.Namespace, **_: Any) -> int:
    """Exit 0 only when the claimed wait's account is known and account_mismatch finds nothing."""
    wait_id = _require(args.wait_id, WAIT_ID_RE, "wait id")
    record = _read_json(claimed_path(ws, wait_id))
    if _valid_marker(record, wait_id):
        reason = account_mismatch(ws, record.get("cloud_user_id"), cloud.user_id)
    else:
        reason = "no_such_wait"
    emit({"wait_id": wait_id, "ok": reason is None, "reason": reason})
    return EXIT_OK if reason is None else EXIT_NO


def cmd_note(ws: Path, cloud: Cloud | None, args: argparse.Namespace, now: Callable[[], float] = time.time,
             **_: Any) -> int:
    """One more line on the wait's private card (pending or claimed); exit 1 when it has no card."""
    wait_id = _require(args.wait_id, WAIT_ID_RE, "wait id")
    text = " ".join(" ".join(args.text).split())
    if not text:
        raise Setup("invalid_arguments", "the note is empty")
    if args.status and args.status not in CARD_STATUS.values() and args.status != "waiting":
        raise Setup("invalid_arguments", f"unknown card status {args.status!r}")
    card = set_card(ws, wait_id, status=args.status, line=text, at=now())
    emit({"wait_id": wait_id, "noted": card is not None,
          "card": {k: card.get(k) for k in ("id", "room", "event", "status")} if card else None})
    return EXIT_OK if card else EXIT_NO


def prune(ws: Path, now: float) -> int:
    removed = 0
    d = waits_dir(ws)
    if not d.is_dir():
        return removed
    for p in d.iterdir():
        if p.suffix not in (".claimed", ".lock") or (p.suffix == ".lock" and marker_path(ws, p.stem).exists()):
            continue
        try:
            if now - p.stat().st_mtime >= PRUNE_S:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def cmd_rearm(
    ws: Path,
    spawn: Callable[[Path, str], int] = spawn_waiter,
    now: Callable[[], float] = time.time,
) -> int:
    rearmed, running = [], []
    for marker in list_markers(ws):
        wait_id = marker["wait_id"]
        if waiter_alive(ws, wait_id):
            running.append(wait_id)
        else:
            rearmed.append({"wait_id": wait_id, "waiter_pid": _try_spawn(spawn, ws, wait_id)})
    emit({"rearmed": rearmed, "running": running, "pruned": prune(ws, now()) + prune_cards(ws, now())})
    return EXIT_OK


# --------------------------------------------------------------------------- main


def _workspace() -> Path:
    try:
        from sutando_config import resolve_workspace  # noqa: PLC0415

        return Path(resolve_workspace())
    except Exception:  # noqa: BLE001 — an unreadable config still has the in-repo default
        return REPO_ROOT / "workspace"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workspace", help="workspace dir (default: the configured workspace)")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("find")
    f.add_argument("query", nargs="+")
    s = sub.add_parser("status")
    s.add_argument("slugs", nargs="*")
    s.add_argument("--room", help="only this room's waits")
    a = sub.add_parser("await")
    a.add_argument("slugs", nargs="+")
    a.add_argument("--room", required=True)
    a.add_argument("--reply-to", required=True)
    a.add_argument("--task", required=True)
    a.add_argument("--owner", required=True)
    req = a.add_mutually_exclusive_group(required=True)
    req.add_argument("--request")
    req.add_argument("--request-file", help="read the request from this file; - reads stdin")
    a.add_argument("--private", action="store_true",
                   help="asked from a room with other people: show the card only to the owner, under --reply-to")
    a.add_argument("--line", action="append", help="a line the private card shows above the apps (repeatable)")
    n = sub.add_parser("note")
    n.add_argument("wait_id")
    n.add_argument("text", nargs="+")
    n.add_argument("--status", help="also set the card's status (e.g. connected)")
    c = sub.add_parser("claim")
    c.add_argument("room")
    c.add_argument("--force", action="store_true", help="claim even when the apps are not connected")
    v = sub.add_parser("verify-account")
    v.add_argument("wait_id")
    sub.add_parser("rearm")
    w = sub.add_parser("waiter", help=argparse.SUPPRESS)
    w.add_argument("wait_id")
    return p


COMMANDS = {"find": cmd_find, "status": cmd_status, "await": cmd_await, "claim": cmd_claim,
            "verify-account": cmd_verify_account, "note": cmd_note}


def main(
    argv: list[str] | None = None,
    cloud: Cloud | None = None,
    spawn: Callable[[Path, str], int] | None = None,
) -> int:
    args = parser().parse_args(argv)
    ws = Path(args.workspace) if args.workspace else _workspace()
    spawn = spawn or spawn_waiter
    try:
        if args.cmd == "rearm":
            return cmd_rearm(ws, spawn)
        if args.cmd == "note":
            return cmd_note(ws, None, args)
        cloud = cloud or Cloud(ws)
        if args.cmd == "waiter":
            if not WAIT_ID_RE.match(args.wait_id):
                raise Setup("invalid_arguments", f"not a wait id: {args.wait_id[:40]!r}")
            outcome = run_waiter(ws, args.wait_id, cloud)
            print(f"{now_iso(time.time())} waiter {args.wait_id}: {outcome}", flush=True)
            return EXIT_OK
        return COMMANDS[args.cmd](ws, cloud, args, spawn=spawn)
    except Setup as exc:
        emit({"error": exc.code, "detail": str(exc)})
        return EXIT_SETUP
    except cloud_auth.CloudError as exc:
        emit({"error": "cloud_error", "status": exc.status, "code": exc.code, "detail": exc.detail})
        return EXIT_SETUP


if __name__ == "__main__":
    sys.exit(main())
