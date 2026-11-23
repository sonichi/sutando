#!/usr/bin/env python3
"""Sign the owner in to an MCP server that needs OAuth, from a card in their DM.

  start <name> --task T [--request TEXT | --request-file PATH|-]
                          start `claude mcp login <name> --no-browser` in a private tmux
                          session, read the authorization link it prints, record a wait and
                          print "message": the room.message.send payload of the sign-in card
  paste <wait-id> --task T
                          the owner sent the address of the page that did not load: read it
                          from STDIN, check it is this wait's callback, type it into the login
  card <wait-id>          the card for how a wait ended ("Connected", or the link expired),
                          under the sign-in card's id
  status [--room R]       pending waits and the ones that ended recently
  rearm                   restart the waiter of every pending wait that has none

Output is one JSON object on stdout. Exit codes: 0 ok; 1 a negative answer (already signed
in, nothing to paste into, the address is not this wait's callback); 2 a setup problem to
relay, not retry (not the owner's DM, unknown server, no link printed, bad arguments).

A wait is `<workspace>/state/connect-mcp-waits/<wait-id>.json`, held by the shared wait store
(`src/wait_store.py`): the waiter, `start` for the same server and the deadline race for one
rename, and the winner writes one resume task, `tasks/task-connect-mcp-<wait-id>.txt`.

Only the owner's own DM task can start a sign-in or paste into one. The pasted address carries
a single-use authorization code: it is read from STDIN, never taken as an argument, never
printed, logged or stored; the wait keeps only the callback port and a hash of the link's
`state`, which is what a pasted address is checked against.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[3]
sys.path.insert(0, str(REPO_ROOT / "src"))
import agent_card as ac  # noqa: E402
import wait_store  # noqa: E402
from task_body_guard import confine_user_content, header_safe_value  # noqa: E402

EXIT_OK, EXIT_NO, EXIT_SETUP = 0, 1, 2
WAITS_DIRNAME = "connect-mcp-waits"
WAITER_LOG = "connect-mcp-waits.log"
# Claude Code's callback listener gives up after five minutes.
LOGIN_S = 5 * 60
POLL_S = 2.0
LINK_WAIT_S = 20.0
LINK_POLL_S = 0.5
# A wait this far past its deadline (a Mac that slept through it) is dropped without a word.
STALE_GRACE_S = 3600
PRUNE_S = 7 * 86400
RECENT_S = 30 * 60
MAX_ATTEMPTS = 2
MAX_REQUEST_CHARS = 500
MAX_REQUEST_READ = 64 * 1024
MAX_PASTE_CHARS = 4096
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
ROOM_RE = re.compile(r"^!\S{1,255}\Z")
EVENT_RE = re.compile(r"^\$\S{1,255}\Z")
TASK_RE = re.compile(r"^task-[A-Za-z0-9._~-]{1,120}\Z")
URL_RE = re.compile(r"https://[^\s\"'<>]+")
STATUS_RE = re.compile(r"^\s*Status:\s*(.+?)\s*$", re.M)
ORIGIN_SOURCE = "ag2space"
RESUME_SOURCE = "connector-resume"
# A private tmux server: the login never shares a server with the core's own session.
TMUX_SERVER = "sutando-connect-mcp"
CALLBACK_HOSTS = ("localhost", "127.0.0.1")
PHONE_LINE = "On a phone the last page will not load. Send me its address here."
WAITER_OUTCOMES = ("connected", "timeout")

# run(argv, timeout) -> (exit code, stdout + stderr). Every tmux and claude call goes through one.
Run = Callable[[list, float], "tuple[int, str]"]


class Setup(Exception):
    """A problem the owner or the agent has to fix; exit 2."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def run_command(argv: list, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=timeout, cwd=tempfile.gettempdir())
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return proc.returncode, proc.stdout or ""


# --------------------------------------------------------------------------- wait files


def _valid_marker(data: Any, wait_id: str) -> bool:
    return (
        isinstance(data, dict)
        and data.get("wait_id") == wait_id
        and all(isinstance(data.get(k), str) and data[k]
                for k in ("name", "room", "reply_to", "task", "owner", "session", "state_sha256"))
        and bool(NAME_RE.match(data["name"]))
        and isinstance(data.get("port"), int)
        and isinstance(data.get("attempt"), int)
        and isinstance(data.get("deadline"), (int, float))
    )


def store(ws: Path) -> wait_store.WaitStore:
    return wait_store.WaitStore(ws, WAITS_DIRNAME, _valid_marker)


def display_name(name: str) -> str:
    return name[:1].upper() + name[1:]


def card_id(name: str) -> str:
    return f"mcp-signin:{name}"


def summary(marker: dict) -> dict:
    keys = ("wait_id", "name", "room", "reply_to", "task", "owner", "attempt", "deadline_at")
    return {k: marker.get(k) for k in keys}


def recent_claims(ws: Path, now: float) -> list[dict]:
    out = []
    d = store(ws).dir()
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.claimed")):
        record = wait_store.read_json(p)
        if _valid_marker(record, p.stem) and now - float(record.get("claimed_at") or 0) < RECENT_S:
            out.append(record)
    return out


# --------------------------------------------------------------------------- the owner's DM


def dm_task(ws: Path, task: str) -> dict:
    """{owner, room, reply_to} of `task` when it is the owner's own request in their DM."""
    if not TASK_RE.match(task or ""):
        raise Setup("invalid_arguments", "not a task id")
    try:
        owner, text = wait_store.owner_task(ws, task, ORIGIN_SOURCE, "s.space")
    except wait_store.NotOwnerTask as exc:
        raise Setup("not_owner_dm", f"{task} {exc}: a sign-in card goes only to the owner, in their own DM.") from None
    if set(wait_store.header_values(text, "channel_kind")) != {"dm"}:
        raise Setup("not_owner_dm", f"{task} is not from the owner's DM: a sign-in card goes only there.")
    rooms = set(wait_store.header_values(text, "channel_id"))
    events = set(wait_store.header_values(text, "source_message_id"))
    if len(rooms) != 1 or not ROOM_RE.match(next(iter(rooms))):
        raise Setup("not_owner_dm", f"{task} names no single room.")
    if len(events) != 1 or not EVENT_RE.match(next(iter(events))):
        raise Setup("not_owner_dm", f"{task} names no single message to reply to.")
    return {"owner": owner, "room": next(iter(rooms)), "reply_to": next(iter(events))}


# --------------------------------------------------------------------------- claude and tmux


def tmux(*args: str) -> list:
    return ["tmux", "-L", TMUX_SERVER, *args]


def session_name(wait_id: str) -> str:
    return f"mcp-{wait_id}"


def server_status(run: Run, name: str) -> str | None:
    """The server's Status line from `claude mcp get`, or None when Claude Code has no such server."""
    code, out = run(["claude", "mcp", "get", name], 60.0)
    found = STATUS_RE.search(out)
    if code != 0 or not found:
        return None
    return found.group(1)


def is_connected(status: str | None) -> bool:
    return bool(status) and "connected" in status.lower() and "not connected" not in status.lower()


def session_alive(run: Run, session: str) -> bool:
    return run(tmux("has-session", "-t", f"={session}"), 10.0)[0] == 0


def kill_session(run: Run, session: str) -> None:
    run(tmux("kill-session", "-t", f"={session}"), 10.0)


def pane_text(run: Run, session: str) -> str:
    code, out = run(tmux("capture-pane", "-p", "-J", "-S", "-200", "-t", f"={session}:"), 10.0)
    return out if code == 0 else ""


def login_command(session: str, name: str) -> list:
    # Started outside the checkout, so the project's own session hooks do not fire for the login.
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    return tmux("new-session", "-d", "-s", session, "-x", "250", "-y", "50", "-c", tempfile.gettempdir(),
                *(["-e", f"CLAUDE_CONFIG_DIR={config}"] if config else []),
                "claude", "mcp", "login", name, "--no-browser")


def callback_of(link: str) -> tuple[int, str] | None:
    """(callback port, state) of an authorization link whose redirect is this machine's listener."""
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
        redirect = urllib.parse.urlsplit((query.get("redirect_uri") or [""])[0])
        port = redirect.port
    except ValueError:
        return None
    state = (query.get("state") or [""])[0]
    if redirect.scheme != "http" or redirect.hostname not in CALLBACK_HOSTS or redirect.path != "/callback":
        return None
    if not port or not state:
        return None
    return port, state


def find_link(text: str) -> str | None:
    for link in URL_RE.findall(text):
        if callback_of(link):
            return link
    return None


def state_hash(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def read_link(run: Run, session: str, now: Callable[[], float], sleep: Callable[[float], None]) -> str | None:
    """The authorization link the login printed, waiting up to LINK_WAIT_S for it."""
    end = now() + LINK_WAIT_S
    while True:
        link = find_link(pane_text(run, session))
        if link or now() >= end or not session_alive(run, session):
            return link
        sleep(LINK_POLL_S)


# --------------------------------------------------------------------------- cards


def signin_message(marker: dict, link: str) -> dict:
    name = display_name(marker["name"])
    body, card = ac.build(
        card_id(marker["name"]), "pending", title=f"Sign in to {name}",
        text=f"Sign in and I'll carry on with your request. The link works for {LOGIN_S // 60} minutes.",
        buttons=[ac.button("Sign in", "url", style="primary", url=link)],
        note=PHONE_LINE, note_tone="dim", expires_at_ms=int(marker["deadline"]) * 1000,
        body=ac.body_line(f"Sign in to {name}. {PHONE_LINE}", link_label="Sign in", url=link))
    return {"body": body, "extra_content": {ac.CARD_KEY: card}, "reply_to": marker["reply_to"],
            "operation_id": f"{marker['wait_id']}:signin-card"}


def outcome_message(record: dict) -> dict | None:
    """The card for how a wait ended, under the sign-in card's id; None for an end that says nothing."""
    name, by = display_name(record["name"]), record.get("claimed_by")
    if by == "connected":
        body, card = ac.build(card_id(record["name"]), "done", title=name, status="Connected",
                              text=f"{name} is signed in.", body=f"{name} is connected.")
    elif by == "timeout":
        again = record.get("attempt", 1) < MAX_ATTEMPTS
        text = (f"The {name} sign-in link expired. Say \"try again\" and I'll send a new one."
                if again else f"The {name} sign-in link expired again. Ask me whenever you're ready to sign in.")
        body, card = ac.build(card_id(record["name"]), "expired", title=f"Sign in to {name}", status="Expired",
                              text=text, body=text)
    else:
        return None
    return {"body": body, "extra_content": {ac.CARD_KEY: card}, "reply_to": record["reply_to"],
            "operation_id": f"{record['wait_id']}:{by}-card"}


# --------------------------------------------------------------------------- resume task


def resume_text(record: dict, outcome: str) -> str:
    name, wait_id = display_name(record["name"]), record["wait_id"]
    room, reply_to = record["room"], record["reply_to"]
    request = header_safe_value(record.get("request") or "")
    origin = f"(wait {wait_id}, from task {record['task']})"
    card = (f"Run the connect-mcp helper: connect_mcp.py card {wait_id}, and post its \"message\" in room {room} "
            "with room.message.send.")
    if outcome == "connected":
        text = (
            f"{name} is now signed in {origin}. {card} Then do the owner's earlier request \"{request}\" with "
            f"the {record['name']} MCP server's tools and reply in room {room} (reply_to {reply_to}, "
            f"operation_id {wait_id}:answer). If those tools are not in this session, say in one line that "
            f"{name} is signed in but its tools have not loaded here yet; never ask the owner to open a terminal. "
            "Write the result [no-send]. Follow the connect-mcp skill, Resume."
        )
    elif record.get("attempt", 1) < MAX_ATTEMPTS:
        text = (
            f"The {name} sign-in link expired before the owner finished {origin}. Their earlier request was "
            f"\"{request}\". {card} Send no new link now: when the owner says \"try again\", run connect_mcp.py "
            "start once more for that task. Write the result [no-send]. Follow the connect-mcp skill, Resume."
        )
    else:
        text = (
            f"The second {name} sign-in link also expired {origin}. Their earlier request was \"{request}\". "
            f"{card} Send no further link by yourself. Write the result [no-send]. "
            "Follow the connect-mcp skill, Resume."
        )
    return confine_user_content(text)


def write_resume_task(ws: Path, record: dict, outcome: str, now: float) -> Path:
    return wait_store.write_resume_task(
        ws, f"task-connect-mcp-{record['wait_id']}", source=RESUME_SOURCE, room=record["room"],
        owner=record["owner"], body=resume_text(record, outcome), now=now)


# --------------------------------------------------------------------------- waiter


def run_waiter(ws: Path, wait_id: str, run: Run, now: Callable[[], float] = time.time,
               sleep: Callable[[float], None] = time.sleep) -> str:
    """Watch the login until it says it is done or its time is up, then claim and write the resume
    task. Returns connected | timeout | expired | invalid | claimed_elsewhere | write_failed | busy."""
    waits = store(ws)
    lock = wait_store.hold_lock(waits, wait_id)
    if lock is None:
        return "busy"
    try:
        while True:
            try:
                marker = waits.read_marker(wait_id)
            except ValueError:
                return "invalid" if waits.claim(wait_id, "invalid", now()) is not None else "claimed_elsewhere"
            if marker is None:
                return "claimed_elsewhere"
            t = now()
            deadline = float(marker["deadline"])
            session = marker["session"]
            if t >= deadline + STALE_GRACE_S:
                kill_session(run, session)
                return "expired" if waits.claim(wait_id, "expired", t) is not None else "claimed_elsewhere"
            # The pane is the cheap probe; `claude mcp get` runs only when the login looks finished.
            finished = "Authenticated" in pane_text(run, session) or not session_alive(run, session)
            outcome = None
            if (finished or t >= deadline) and is_connected(server_status(run, marker["name"])):
                outcome = "connected"
            elif finished or t >= deadline:
                outcome = "timeout"
            if outcome is not None:
                won = waits.claim(wait_id, outcome, now())
                if won is None:
                    return "claimed_elsewhere"
                kill_session(run, session)
                if won.get("invalid"):
                    return "invalid"
                try:
                    write_resume_task(ws, won, outcome, now())
                    return outcome
                except (OSError, ValueError) as exc:
                    print(f"connect-mcp: resume task for {wait_id} not written, retrying: {exc}", file=sys.stderr)
                    if not waits.release(won):
                        return "write_failed"
            sleep(POLL_S)
    finally:
        lock.close()


def spawn_waiter(ws: Path, wait_id: str) -> int:
    return wait_store.spawn_detached(SCRIPT_PATH, ws, wait_id, WAITER_LOG)


def _try_spawn(spawn: Callable[[Path, str], int], ws: Path, wait_id: str) -> int | None:
    """The waiter's pid, or None when it could not start; the wait stays on disk for `rearm`."""
    try:
        return spawn(ws, wait_id)
    except OSError as exc:
        print(f"connect-mcp: could not start the waiter for {wait_id}: {exc}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------- commands


def emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2))


def read_request(args: argparse.Namespace) -> str:
    if args.request is not None:
        text = args.request
    elif args.request_file == "-":
        text = sys.stdin.read(MAX_REQUEST_READ)
    elif args.request_file:
        text = Path(args.request_file).read_text(encoding="utf-8")[:MAX_REQUEST_READ]
    else:
        text = ""
    return " ".join(text.split())[:MAX_REQUEST_CHARS]


def cmd_start(ws: Path, args: argparse.Namespace, *, run: Run, spawn: Callable[[Path, str], int],
              now: Callable[[], float], sleep: Callable[[float], None], **_: Any) -> int:
    name = args.name
    if not NAME_RE.match(name):
        raise Setup("invalid_arguments", "not an MCP server name")
    origin = dm_task(ws, args.task)
    request = read_request(args)
    waits = store(ws)

    for marker in waits.list_markers():
        if marker["name"] != name:
            continue
        link = find_link(pane_text(run, marker["session"])) if now() < float(marker["deadline"]) else None
        if link and marker["room"] == origin["room"] and state_hash(callback_of(link)[1]) == marker["state_sha256"]:
            pid = None if waits.waiter_alive(marker["wait_id"]) else _try_spawn(spawn, ws, marker["wait_id"])
            emit({**summary(marker), "reused": True, "waiter_pid": pid, "card_id": card_id(name),
                  "message": signin_message(marker, link)})
            return EXIT_OK
        # One login per server: an older one that cannot be reused is ended before a new link exists.
        if waits.claim(marker["wait_id"], "superseded", now()) is not None:
            kill_session(run, marker["session"])

    status = server_status(run, name)
    if status is None:
        raise Setup("unknown_server", f"Claude Code has no MCP server named {name}.")
    if is_connected(status):
        emit({"wait_id": None, "reason": "already_connected", "name": name, "message": None})
        return EXIT_NO

    t = now()
    wait_id = wait_store.new_wait_id(t)
    session = session_name(wait_id)
    code, _out = run(login_command(session, name), 30.0)
    if code != 0:
        raise Setup("login_not_started", f"The {name} sign-in could not be started on this machine.")
    link = read_link(run, session, now, sleep)
    if not link:
        kill_session(run, session)
        raise Setup("no_link", f"The {name} sign-in printed no link: this server may not use OAuth sign-in.")
    port, state = callback_of(link)
    earlier = [r for r in recent_claims(ws, t)
               if r["name"] == name and r["room"] == origin["room"] and r.get("claimed_by") == "timeout"]
    marker = {
        "version": 1,
        "wait_id": wait_id,
        "name": name,
        **origin,
        "task": args.task,
        "request": request,
        "session": session,
        "port": port,
        "state_sha256": state_hash(state),
        "attempt": min(MAX_ATTEMPTS, 1 + max((r.get("attempt", 1) for r in earlier), default=0)),
        "created_at": wait_store.now_iso(t),
        "deadline": int(t + LOGIN_S),
        "deadline_at": wait_store.now_iso(t + LOGIN_S),
    }
    try:
        message = signin_message(marker, link)
        path = waits.write_marker(marker)
    except (OSError, ac.CardError):
        kill_session(run, session)
        raise
    pid = _try_spawn(spawn, ws, wait_id)
    emit({**summary(marker), "marker": str(path), "reused": False, "waiter_pid": pid, "card_id": card_id(name),
          "message": message})
    return EXIT_OK


def callback_problem(pasted: str, marker: dict) -> str | None:
    """Why `pasted` is not the address this wait's sign-in ends on, or None. Never echoes the address."""
    if not pasted or len(pasted) > MAX_PASTE_CHARS or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7F
                                                          for c in pasted):
        return "not_one_address"
    try:
        parts = urllib.parse.urlsplit(pasted)
        port = parts.port
        query = urllib.parse.parse_qs(parts.query)
    except ValueError:
        return "not_an_address"
    if parts.scheme != "http" or parts.hostname not in CALLBACK_HOSTS or parts.username or parts.password:
        return "not_this_machine"
    if port != marker["port"] or parts.path != "/callback" or parts.fragment:
        return "not_this_sign_in"
    if not (query.get("code") or query.get("error")):
        return "no_code"
    if state_hash((query.get("state") or [""])[0]) != marker["state_sha256"]:
        return "not_this_sign_in"
    return None


def cmd_paste(ws: Path, args: argparse.Namespace, *, run: Run, now: Callable[[], float],
              stdin: Any = None, **_: Any) -> int:
    pasted = (stdin if stdin is not None else sys.stdin).read(MAX_PASTE_CHARS + 1).strip()
    if not wait_store.WAIT_ID_RE.match(args.wait_id):
        raise Setup("invalid_arguments", "not a wait id")
    origin = dm_task(ws, args.task)
    try:
        marker = store(ws).read_marker(args.wait_id)
    except ValueError:
        marker = None
    if marker is None or marker["room"] != origin["room"] or marker["owner"] != origin["owner"]:
        emit({"status": "refused", "reason": "no_pending_wait"})
        return EXIT_NO
    if now() >= float(marker["deadline"]):
        emit({"status": "refused", "reason": "expired", "wait_id": marker["wait_id"]})
        return EXIT_NO
    problem = callback_problem(pasted, marker)
    if problem:
        emit({"status": "refused", "reason": problem, "wait_id": marker["wait_id"]})
        return EXIT_NO
    session = marker["session"]
    if not session_alive(run, session):
        emit({"status": "refused", "reason": "login_gone", "wait_id": marker["wait_id"]})
        return EXIT_NO
    typed = run(tmux("send-keys", "-t", f"={session}:", "-l", pasted), 10.0)[0]
    entered = run(tmux("send-keys", "-t", f"={session}:", "Enter"), 10.0)[0] if typed == 0 else 1
    if typed != 0 or entered != 0:
        emit({"status": "refused", "reason": "not_typed", "wait_id": marker["wait_id"]})
        return EXIT_NO
    emit({"status": "sent", "wait_id": marker["wait_id"], "name": marker["name"]})
    return EXIT_OK


def cmd_card(ws: Path, args: argparse.Namespace, **_: Any) -> int:
    if not wait_store.WAIT_ID_RE.match(args.wait_id):
        raise Setup("invalid_arguments", "not a wait id")
    record = wait_store.read_json(store(ws).claimed_path(args.wait_id))
    message = outcome_message(record) if _valid_marker(record, args.wait_id) else None
    emit({"wait_id": args.wait_id, "outcome": record.get("claimed_by") if message else None,
          "card_id": card_id(record["name"]) if message else None, "message": message})
    return EXIT_OK if message else EXIT_NO


def cmd_status(ws: Path, args: argparse.Namespace, *, now: Callable[[], float], **_: Any) -> int:
    def here(m: dict) -> bool:
        return not args.room or m.get("room") == args.room

    waits = store(ws)
    emit({"pending": [{**summary(m), "waiter_alive": waits.waiter_alive(m["wait_id"])}
                      for m in waits.list_markers() if here(m)],
          "ended": [{**summary(r), "outcome": r.get("claimed_by")} for r in recent_claims(ws, now()) if here(r)]})
    return EXIT_OK


def cmd_rearm(ws: Path, args: argparse.Namespace, *, spawn: Callable[[Path, str], int],
              now: Callable[[], float], **_: Any) -> int:
    waits = store(ws)
    rearmed, running = [], []
    for marker in waits.list_markers():
        wait_id = marker["wait_id"]
        if waits.waiter_alive(wait_id):
            running.append(wait_id)
        else:
            rearmed.append({"wait_id": wait_id, "waiter_pid": _try_spawn(spawn, ws, wait_id)})
    emit({"rearmed": rearmed, "running": running, "pruned": waits.prune(now(), PRUNE_S)})
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
    s = sub.add_parser("start")
    s.add_argument("name", help="the MCP server's name in Claude Code")
    s.add_argument("--task", required=True, help="the owner's DM task that needs the server")
    req = s.add_mutually_exclusive_group()
    req.add_argument("--request", help="what the owner asked for, to carry on with after sign-in")
    req.add_argument("--request-file", help="read the request from this file; - reads stdin")
    pa = sub.add_parser("paste", help="type the callback address read from STDIN into the login")
    pa.add_argument("wait_id")
    pa.add_argument("--task", required=True, help="the owner's DM task that carried the address")
    c = sub.add_parser("card")
    c.add_argument("wait_id")
    st = sub.add_parser("status")
    st.add_argument("--room", help="only this room's waits")
    sub.add_parser("rearm")
    w = sub.add_parser("waiter", help=argparse.SUPPRESS)
    w.add_argument("wait_id")
    return p


COMMANDS = {"start": cmd_start, "paste": cmd_paste, "card": cmd_card, "status": cmd_status, "rearm": cmd_rearm}


def main(argv: list[str] | None = None, *, run: Run | None = None,
         spawn: Callable[[Path, str], int] | None = None, now: Callable[[], float] | None = None,
         sleep: Callable[[float], None] | None = None, stdin: Any = None) -> int:
    args = parser().parse_args(argv)
    ws = Path(args.workspace) if args.workspace else _workspace()
    run, spawn = run or run_command, spawn or spawn_waiter
    now, sleep = now or time.time, sleep or time.sleep
    try:
        if args.cmd == "waiter":
            if not wait_store.WAIT_ID_RE.match(args.wait_id):
                raise Setup("invalid_arguments", "not a wait id")
            outcome = run_waiter(ws, args.wait_id, run, now, sleep)
            print(f"{wait_store.now_iso(now())} waiter {args.wait_id}: {outcome}", flush=True)
            return EXIT_OK
        return COMMANDS[args.cmd](ws, args, run=run, spawn=spawn, now=now, sleep=sleep, stdin=stdin)
    except Setup as exc:
        emit({"error": exc.code, "detail": str(exc)})
        return EXIT_SETUP
    except (OSError, ac.CardError) as exc:
        emit({"error": "not_written", "detail": type(exc).__name__})
        return EXIT_SETUP


if __name__ == "__main__":
    sys.exit(main())
