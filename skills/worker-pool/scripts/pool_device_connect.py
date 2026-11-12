#!/usr/bin/env python3
"""A pool worker's own agent through s.space's device-connect flow: the agent starts it, the owner approves.

The core asks its own server for a device code (`POST /api/connect/start` with the agent's `label` and
`display_name`), sends the owner the approval link in their DM as an approval card (src/agent_card.py; the
text note when no card can be posted), and asks again (`POST /api/connect/poll`)
until the owner has approved it in a browser or the code expires. The answered bearer goes straight into
the vault as the worker's connection, and only after the gateway confirms it is the agent the poll
named, a new agent owned by the core's owner. `rotate=True` asks the same way for a new token of the
worker's existing agent and replaces its secret in place.

The device code and the bearer stay in this process and the vault: nothing here returns, prints, logs,
writes or raises either; callers get the agent id and a status.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path[:0] = [p for p in (str(_HERE), str(_REPO / "src")) if p not in sys.path]

import agent_card as ac
import issued_cards
import connections
import pool_provision as pp
import pool_roster as pr

START_PATH = "/api/connect/start"
POLL_PATH = "/api/connect/poll"
# The server's own bounds on a code's life and on how often it may be asked.
DEFAULT_TTL_S, MAX_TTL_S = 900, 3600
MIN_INTERVAL_S, MAX_INTERVAL_S = 3, 60
SLOW_DOWN_STEP_S = 5
KEEP_POLLING = ("authorization_pending", "slow_down")
# An approval that lands just before expiry is still collected for this long after it.
COLLECT_GRACE_S = 60
LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")


class DeviceConnectError(pp.ProvisionError):
    def __init__(self, message: str, status: str = "failed"):
        super().__init__(message)
        self.status = status


@contextlib.contextmanager
def fenced(worker: str, workspace: Optional[Path] = None):
    """Hold the roster lock retirement sets `retired` under, while `worker` is still live: a retire then
    lands first and is refused here, or waits and removes what was saved (pool_manage._retire_identity)."""
    ws = connections._workspace(workspace)
    with pr._locked(ws):
        row = ((pr.load_roster(ws) or {}).get("workers") or {}).get(worker)
        if not isinstance(row, dict) or row.get("state") == "retired":
            raise DeviceConnectError(f"worker {worker} was retired while the approval was pending; nothing "
                                     "was saved and no lane started", status="worker_retired")
        yield


def _bounded(value, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return default


def start(base: str, label: str, display_name: str, *, server: str = "", welcome_core: str = "",
          request=pp.http_json, clock=time.time) -> dict:
    """A new device code for agent `label`: {device_code, verify_url, interval, expires_at}. The approval
    link must be on `base`'s origin or on `server`'s own https host, and must not carry the device code,
    which alone collects the bearer."""
    if not LABEL_RE.fullmatch(label or ""):
        raise DeviceConnectError("the label must be 1-32 lowercase letters, digits or inner dashes; it names "
                                 "the agent, and without one the server would hand back the owner's own agent")
    body = {"label": label}
    if display_name:
        body["display_name"] = display_name[:64]
    if welcome_core:
        body.update(welcome="worker", welcome_core=welcome_core)
    status, _, out = request("POST", base + START_PATH, payload=body)
    code, url = out.get("device_code"), out.get("verify_url")
    if status != 200 or not isinstance(code, str) or not code or not isinstance(url, str):
        raise DeviceConnectError(f"the server did not start a device connect (HTTP {status}"
                                 f"{', ' + pp.error_code(out) if pp.error_code(out) else ''})")
    url = urllib.parse.urljoin(base + "/", url)
    hosts = {pp.origin_of(base)} | ({pp.origin_of("https://" + server)} if server else set())
    if pp.origin_of(url) not in hosts or not url.startswith("https://") or code in url:
        raise DeviceConnectError("the approval link the server answered is not a link on its own host that is "
                                 "safe to send; nothing was sent")
    ttl = _bounded(out.get("expires_in"), DEFAULT_TTL_S, 60, MAX_TTL_S)
    return {"device_code": code, "verify_url": url, "expires_at": clock() + ttl,
            "interval": _bounded(out.get("interval"), MIN_INTERVAL_S, MIN_INTERVAL_S, MAX_INTERVAL_S)}


def collect(base: str, pending: dict, *, request=pp.http_json, sleep=time.sleep, clock=time.time) -> dict:
    """Ask until the owner has approved: {relay_url, bearer, agent_id}. A code that expires or is gone is
    refused with the time it expired; a refusal never carries the code or a bearer."""
    known = [pending["device_code"]]
    interval = wait = pending["interval"]
    while True:
        sleep(wait)
        try:
            status, headers, out = request("POST", base + POLL_PATH, payload={"device_code": pending["device_code"]})
        except pp.TransportError:
            status, headers, out = 0, {}, {}
        reason = pp.error_code(out)
        # RFC 8628 device-flow answers: still waiting, or asked to poll more slowly.
        if reason == "slow_down":
            interval = min(MAX_INTERVAL_S, interval + SLOW_DOWN_STEP_S)
        wait = interval
        if status == 429:
            wait = max(interval, _bounded(headers.get("retry-after"), interval, 0, MAX_INTERVAL_S))
        if status == 200 and all(isinstance(out.get(k), str) and out.get(k)
                                 for k in ("relay_url", "bearer", "agent_id")):
            return {k: out[k] for k in ("relay_url", "bearer", "agent_id")}
        expired = time.strftime("%H:%M %Z", time.localtime(pending["expires_at"]))
        if status in (404, 410) or clock() > pending["expires_at"] + COLLECT_GRACE_S:
            raise DeviceConnectError(f"the approval link expired at {expired} without an approval; "
                                     "nothing was saved, so start again for a new link", status="expired")
        if reason in KEEP_POLLING:
            continue
        if status not in (0, 200, 202, 429, 503) and status < 500:
            raise DeviceConnectError(pp.scrub(f"the server refused the device connect (HTTP {status}"
                                              f"{', ' + pp.error_code(out) if pp.error_code(out) else ''})", known))


def _at(ts: float, now: float) -> str:
    """`ts` as the owner reads a clock: "3:52 PM", with "tomorrow" (or the date) when it is not today."""
    t, today = time.localtime(ts), time.localtime(now)
    hm = time.strftime("%I:%M %p", t).lstrip("0")
    if t[:3] == today[:3]:
        return f"{hm} today"
    if t[:3] == time.localtime(now + 86400)[:3]:
        return f"{hm} tomorrow"
    return f"{hm} on {time.strftime('%b', t)} {t.tm_mday}"


def _minutes(seconds: float) -> str:
    n = max(1, int(round(seconds / 60)))
    return f"{n} minute{'' if n == 1 else 's'}"


_md_url, _md = ac.md_url, ac.md_escape


def approval_note(worker_label: str, label: str, verify_url: str, expires_at: float, *, now: float,
                  display_name: str = "", rotated: bool = False) -> str:
    """The owner's DM: what they approve, on which server, a labelled link (the code-bearing URL is never
    the visible text), and how long it works."""
    host = _md(urllib.parse.urlsplit(verify_url).hostname or "")
    on = f" on {host}" if host else ""
    name, worker_label = _md(display_name or label), _md(worker_label)
    if rotated:
        head = f"**Approve a new token for {worker_label}'s agent**"
        what = f"Its agent \"{name}\"{on} gets a new token; the old one stops working once you approve."
    else:
        head = f"**Approve an agent for your worker {worker_label}**"
        what = f"It will work{on} as its own agent, \"{name}\"."
    return (f"[dm-only]\n{head}\n\n{what}\n\n**[Approve {name}{on} \u2192]({_md_url(verify_url)})**\n\n"
            f"Works for {_minutes(expires_at - now)}, until {_at(expires_at, now)}. Nothing is saved until you "
            "approve, and I check that the agent is yours before the worker uses it.")


def expiry_note(worker_label: str, label: str, expires_at: float, *, now: float) -> str:
    """The owner's DM once an unapproved link has lapsed: nothing changed, and how to get a new one."""
    worker_label, label = _md(worker_label), _md(label)
    return (f"[dm-only]\n**The approval link for {worker_label} expired**\n\nIt expired at {_at(expires_at, now)} "
            f"before agent \"{label}\" was approved, so nothing was set up. Ask me for a new link whenever "
            "you are ready.")


def done_note(worker_label: str, label: str, agent_id: str, *, rotated: bool = False) -> str:
    """The owner's DM once the approval is saved: which agent it is and where to find it."""
    worker_label, label = _md(worker_label), _md(label)
    if rotated:
        return (f"[dm-only]\n**Done: {worker_label}'s agent has a new token**\n\nAgent \"{label}\" "
                f"(`{agent_id}`) now uses it, and the old token no longer works.")
    return (f"[dm-only]\n**Done: {worker_label} has its own agent**\n\nAgent \"{label}\" (`{agent_id}`) "
            "is ready. Find it in your agents list or your DMs, and mention it in a room to give it work there.")


def send_to_owner(workspace: Path, text: str) -> str:
    """Queue `text` to the owner's DM through the proactive path; the file name it was queued as."""
    from local_record import new_name, write_text_whole
    from proactive_routing import proactive_filename
    return write_text_whole(Path(workspace) / "results" / proactive_filename(new_name("worker-connect"), None),
                            text).name


RUNTIME_NAMES = {"claude": "Claude", "codex": "Codex"}
CARD_KIND = "worker-approval"
CARD_HANDLER = "skills/worker-pool/scripts/pool_identity.py card-reply"
RESEND = "resend"


def _until(expires_at: float) -> str:
    return time.strftime("%H:%M %Z", time.localtime(expires_at))


def card_id(worker: str, server: str) -> str:
    return f"{CARD_KIND}:{worker}:{server or 'server'}"


def worker_subtitle(workspace: Optional[Path], worker: str, host: str = "") -> str:
    """"Worker · runs on <runtime> on <host>", from the roster's runtime and this machine's label."""
    row = ((pr.load_roster(connections._workspace(workspace)) or {}).get("workers") or {}).get(worker) or {}
    runtime = str(row.get("runtime") or "claude").lower()
    if not host:
        from util_paths import _host_label
        host = _host_label()
    return f"Worker · runs on {RUNTIME_NAMES.get(runtime, runtime.title())}" + (f" on {host}" if host else "")


def approval_card(worker: str, worker_label: str, label: str, verify_url: str, expires_at: float, *,
                  server: str, subtitle: str, rotate: bool = False, now: Optional[float] = None) -> tuple:
    """(body, card) asking the owner to approve agent `label` for the worker; the link is labelled, never bare."""
    until = _until(expires_at)
    what = (f"a new token for the agent {label}" if rotate else f"the agent {label}")
    host = urllib.parse.urlsplit(verify_url).hostname or ""
    approve = f"Approve on {host}" if host else "Approve"
    return ac.build(
        card_id(worker, server), "pending", title=worker_label, subtitle=subtitle, avatar_=ac.avatar(seed=label),
        text=(f"Approve {what} so {worker_label} works as its own agent. Nothing is saved until you approve, "
              "and I check that the agent is yours before the worker uses it."),
        fields=[{"label": "Agent", "value": label}] + ([{"label": "Server", "value": server or host}]
                                                        if server or host else []),
        buttons=[ac.button(f"{approve} →", "url", style="primary", url=verify_url)],
        note=f"Works for {max(1, round((expires_at - (time.time() if now is None else now)) / 60))} minutes, until {until}.", note_tone="dim",
        expires_at_ms=int(expires_at * 1000),
        body=ac.body_line(f"Approve agent \"{label}\" for pool worker {worker_label} (works until {until}):",
                          link_label=approve, url=verify_url))


def done_card(worker: str, worker_label: str, label: str, agent_id: str, *, server: str, subtitle: str,
              rotated: bool = False) -> tuple:
    """(body, card) once the approval is saved: the agent, a DM with it, and a way to give it a room."""
    text = (f"{label} has a new token, and {worker_label} uses it. The old token no longer works." if rotated
            else f"{label} is {worker_label}'s own agent now. Mention it in a room to give it work there.")
    buttons = [ac.button(f"Message {label}", "open_dm", style="primary", user_id=agent_id, disabled=False)]
    if not rotated:
        buttons.append(ac.button(f"Add {label} to a room", "reply", style="secondary",
                                 reply=f"Add {label} to a room", disabled=False))
    return ac.build(card_id(worker, server), "done", title=worker_label, subtitle=subtitle,
                    avatar_=ac.avatar(mxid=agent_id), status="Approved", text=text,
                    fields=[{"label": "Agent", "value": agent_id}], buttons=buttons,
                    body=ac.body_line(f"Done: agent \"{label}\" ({agent_id}) "
                                      + ("has a new token." if rotated else f"is {worker_label}'s own agent.")))


def resend_text(worker_label: str) -> str:
    """What the owner's click on the expired card says: the button's own words, so it needs no second tap."""
    return f"Get a new link for {worker_label}"


def _resend_button(worker: str, worker_label: str) -> dict:
    return ac.button(resend_text(worker_label), "reply", style="primary", reply=resend_text(worker_label),
                     command=ac.reply(CARD_KIND, worker, RESEND), disabled=False)


def failed_card(worker: str, worker_label: str, label: str, why: str, *, server: str, subtitle: str) -> tuple:
    """(body, card) once a resend ended without a new link: why, and the same one tap to try again."""
    why = " ".join(str(why).split())[:300]
    return ac.build(card_id(worker, server), "error", title=worker_label, subtitle=subtitle,
                    avatar_=ac.avatar(seed=label), status="Couldn't make a link",
                    text=f"I couldn't make a new approval link for {label}: {why}.",
                    buttons=[_resend_button(worker, worker_label)],
                    body=ac.body_line(f"I couldn't make a new approval link for agent \"{label}\" (pool worker "
                                      f"{worker_label}): {why}. Ask me for a new link."))


def expired_card(worker: str, worker_label: str, label: str, expires_at: float, *, server: str,
                 subtitle: str) -> tuple:
    """(body, card) once the link lapsed unapproved: nothing changed, and one tap for a new link."""
    until = _until(expires_at)
    return ac.build(card_id(worker, server), "expired", title=worker_label, subtitle=subtitle,
                    avatar_=ac.avatar(seed=label), status="Link expired",
                    text=f"The approval link for {label} expired at {until} before it was approved, so nothing "
                         "was set up.",
                    buttons=[_resend_button(worker, worker_label)],
                    note="A new link works for 15 minutes.", note_tone="dim",
                    body=ac.body_line(f"The approval link for agent \"{label}\" (pool worker {worker_label}) expired "
                                      f"at {until}; nothing was set up. Ask me for a new link."))


@dataclass
class CardSink:
    """Approval cards in the owner's checked DM, posted as the core's own agent on connection `lane`;
    with `workspace`, each posted card is recorded (issued_cards) so only a click on it can act, and the
    card it replaces stops acting."""
    execute: ac.Execute
    room: str
    workspace: Optional[Path] = None
    lane: str = ""

    def show(self, body: str, card: dict, operation_id: str, previous: str = "") -> str:
        """The event now showing `card`; "" when it could not be posted (the caller sends the text)."""
        try:
            if previous:
                eid = ac.update_card(self.execute, self.room, previous, body, card, operation_id=operation_id,
                                     owner_dm=self.room)
            else:
                eid = ac.send_card(self.execute, self.room, body, card, operation_id=operation_id, owner_dm=self.room)
        except (ac.CardError, pp.ProvisionError, pp.TransportError):
            return ""
        if self.workspace is not None:
            try:
                issued_cards.record(self.workspace, eid, card, room=self.room, lane=self.lane, handler=CARD_HANDLER)
            except OSError:
                pass  # unrecorded, the card's own buttons do nothing; the flow itself still completes
        return eid


def default_cards(workspace: Optional[Path], core: dict, vault=None, request=None) -> Optional[CardSink]:
    """A CardSink for the core's lane and owner DM; None when either is unknown, so the text path runs."""
    room = connections.owner_dm(core, workspace)
    if not room:
        return None
    try:
        actions, _ = pp.core_actions(workspace, str(core.get("instance") or ""), vault=vault, request=request)
    except (pp.ProvisionError, connections.ConnectionsError):
        return None
    return CardSink(actions.execute, room, connections._workspace(workspace), str(core.get("instance") or ""))


def record_path(workspace: Optional[Path], worker: str) -> Path:
    return Path(connections._workspace(workspace)) / "state" / "agent-cards" / f"{CARD_KIND}-{worker}.json"


def read_record(workspace: Optional[Path], worker: str) -> dict:
    try:
        rec = json.loads(record_path(workspace, worker).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def write_record(workspace: Optional[Path], worker: str, record: dict) -> None:
    """What a resend needs to start the same flow again; never the device code or a bearer."""
    from local_record import write_whole
    path = record_path(workspace, worker)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_whole(path, record)


RESEND_STALE_S = 15 * 60


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _resend_live(rec: dict, now: float) -> bool:
    """Whether a "resending" record still has its flow on the way: younger than RESEND_STALE_S and, when its
    process is known, that process still running. A dead or silent attempt leaves the button usable again."""
    if rec.get("state") != "resending" or now - float(rec.get("resend_at") or 0) > RESEND_STALE_S:
        return False
    pid = rec.get("resend_pid")
    return not isinstance(pid, int) or _alive(pid)


def _why_not(worker: str, rec: dict, now: float) -> Optional[str]:
    who = rec.get("worker_label") or worker
    if not rec.get("label"):
        return f"no approval card was sent for worker {worker}"
    if _resend_live(rec, now):
        return f"a new approval link for {who} is already on its way to your DM"
    if rec.get("state") == "pending" and float(rec.get("expires_at") or 0) > now:
        return f"the approval link for {who} still works; use Approve on the newest card"
    if rec.get("state") == "done":
        return f"{who} already has its own agent"
    return None


def resend(worker: str, *, workspace: Optional[Path] = None, spawn=None, clock=time.time) -> dict:
    """Start a fresh device flow for `worker` from its expired or failed card, in the background: {status, why?}.
    The claim is one locked read-check-write, so two clicks start one flow. The caller has already checked
    that the owner clicked (agent_card.click_verdict)."""
    from file_lock import locked_file
    path = record_path(workspace, worker)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_file(path.with_name(path.name + ".lock")):
        rec, now = read_record(workspace, worker), clock()
        why = _why_not(worker, rec, now)
        if why:
            return {"status": "refused", "why": why}
        resend_id = secrets.token_hex(8)
        argv = [sys.executable, str(_HERE / "pool_identity.py"), "--workspace", str(connections._workspace(workspace)),
                "rotate" if rec.get("rotate") else "add", worker, "--device-flow", "--label", str(rec["label"]),
                "--resend-id", resend_id]
        if rec.get("display_name"):
            argv += ["--display-name", str(rec["display_name"])]
        if rec.get("lane") is not None:
            argv += ["--lane", str(rec["lane"])]
        claimed = {**rec, "state": "resending", "resend_id": resend_id, "resend_at": now}
        claimed.pop("resend_pid", None)
        write_record(workspace, worker, claimed)
        logs = Path(connections._workspace(workspace)) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        try:
            if spawn is not None:
                pid = spawn(argv)
            else:
                with open(logs / "worker-approval-resend.log", "ab") as log:
                    pid = getattr(subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                                   start_new_session=True), "pid", None)
        except OSError as e:
            write_record(workspace, worker, rec)
            return {"status": "failed", "why": f"the new approval flow did not start ({type(e).__name__})"}
        if isinstance(pid, int):
            write_record(workspace, worker, {**claimed, "resend_pid": pid})
    return {"status": "started"}


def resend_failed(workspace: Optional[Path], worker: str, resend_id: str, why: str,
                  deliver: Optional[Callable[[str], object]] = None, cards: Optional[CardSink] = None) -> bool:
    """A resend attempt that ended before its new card was posted: its record becomes "failed" (the
    button works again) and the owner sees why, on a card (`cards`; by default the core's own, unless
    `deliver` is given) or as text. Only the same attempt is touched; False when it is not."""
    if not resend_id:
        return False
    from file_lock import locked_file
    path = record_path(workspace, worker)
    with locked_file(path.with_name(path.name + ".lock")):
        rec = read_record(workspace, worker)
        if rec.get("state") != "resending" or rec.get("resend_id") != resend_id:
            return False
        write_record(workspace, worker, {**{k: v for k, v in rec.items() if not k.startswith("resend_")},
                                         "state": "failed", "why": why})
    who = rec.get("worker_label") or worker
    if cards is None and deliver is None:
        try:
            cards = default_cards(workspace, pp.core_lane(workspace, rec.get("lane")))
        except (pp.ProvisionError, connections.ConnectionsError, OSError):
            cards = None
    shown = ""
    if cards is not None:
        try:
            card = failed_card(worker, who, str(rec.get("label") or who), why, server=str(rec.get("server") or ""),
                               subtitle=worker_subtitle(workspace, worker))
            shown = cards.show(*card, f"{CARD_KIND}:{worker}:{resend_id}:failed",
                               previous=str(rec.get("event_id") or ""))
        except (ac.CardError, OSError):
            shown = ""
    if shown:
        with locked_file(path.with_name(path.name + ".lock")):
            now = read_record(workspace, worker)
            if now.get("state") == "failed":
                _keep(workspace, worker, {**now, "room": cards.room, "event_id": shown})
        return True
    send = deliver or (lambda text: send_to_owner(workspace or connections._workspace(None), text))
    send(f"[dm-only]\nI couldn't make a new approval link for {who}: {why}. Tap \"{resend_text(who)}\" on the "
         "card to try again.")
    return True


def connect(worker: str, *, label: str, display_name: str = "", worker_label: str = "", rotate: bool = False,
            workspace: Optional[Path] = None, vault=None, lane: Optional[str] = None, base: str = "",
            request=pp.http_json, preview=None, deliver: Optional[Callable[[str], object]] = None,
            cards: Optional[CardSink] = None, say: Callable[[str], None] = lambda text: None, sleep=time.sleep,
            clock=time.time, repo: Optional[Path] = None, logs_dir: Optional[Path] = None,
            start_lane: Optional[Callable[[dict], Optional[dict]]] = None) -> dict:
    """Give `worker` its own agent, or with `rotate` a new token for the one it has: {agent_id, status, row}.
    The owner gets an approval card in their DM (`cards`; by default the core's own, unless `deliver` is
    given), and `deliver(text)` the text note when no card was posted. The token is saved only when
    the gateway confirms the agent the poll named, on the core's server and owned by the core's owner, and
    the save and `start_lane(row)` run under `fenced`, so a worker retired during the wait gets neither."""
    current = connections.worker_identity(worker, workspace)
    if rotate and current is None:
        raise DeviceConnectError(f"worker {worker} has no agent of its own to rotate")
    if not rotate and current is not None:
        raise DeviceConnectError(f"worker {worker} already has an agent of its own; rotate it instead")
    core = pp.core_lane(workspace, lane)
    gateway = pp.origin_of(str(core["gateway_url"]))
    base = (base or gateway).rstrip("/")
    if base != gateway:
        raise DeviceConnectError("the connect base must be the core's own gateway origin, so the approval link "
                                 "cannot come from another host")
    pending = start(base, label, display_name, server=str(core.get("server") or ""),
                    welcome_core="" if rotate else str(core.get("agent_mxid") or ""), request=request, clock=clock)
    known = [pending["device_code"]]
    who = worker_label or worker
    send = deliver or (lambda text: send_to_owner(workspace or connections._workspace(None), text))
    if cards is None and deliver is None:
        cards = default_cards(workspace, core, vault)
    server = str(core.get("server") or "")
    subtitle = worker_subtitle(workspace, worker)
    op = f"{CARD_KIND}:{worker}:{secrets.token_hex(6)}"
    record = {"label": label, "display_name": display_name, "worker_label": who, "rotate": rotate,
              "lane": core.get("instance"), "server": server, "expires_at": pending["expires_at"],
              "state": "pending"}
    shown = ""
    if cards is not None:
        shown = cards.show(*approval_card(worker, who, label, pending["verify_url"], pending["expires_at"],
                                          server=server, subtitle=subtitle, rotate=rotate, now=clock()), f"{op}:pending")
    if shown:
        record.update(room=cards.room, event_id=shown)
    else:
        send(approval_note(who, label, pending["verify_url"], pending["expires_at"], now=clock(),
                           display_name=display_name, rotated=rotate))
    _keep(workspace, worker, record)
    say(f"approval link sent to the owner; it works until {_until(pending['expires_at'])}")

    def tell(card: tuple, note: str, state: str) -> None:
        nonlocal shown
        moved = cards.show(*card, f"{op}:{state}", previous=shown) if shown else ""
        if not moved:
            send(note)
        shown = moved or shown
        _keep(workspace, worker, {**record, "state": state, **({"event_id": moved} if moved else {})})

    try:
        got = collect(base, pending, request=request, sleep=sleep, clock=clock)
    except DeviceConnectError as e:
        if e.status == "expired":
            tell(expired_card(worker, who, label, pending["expires_at"], server=server, subtitle=subtitle),
                 expiry_note(who, label, pending["expires_at"], now=clock()), "expired")
        else:
            _keep(workspace, worker, {**record, "state": e.status})
        raise
    known.append(got["bearer"])
    token = f"{got['relay_url']}|{got['bearer']}"
    try:
        with fenced(worker, workspace):
            now = connections.worker_identity(worker, workspace)
            if (now or {}).get("instance") != (current or {}).get("instance"):
                raise DeviceConnectError(f"worker {worker}'s own identity changed while the approval was "
                                         "pending; nothing was saved", status="identity_changed")
            out = _save(worker, token, got["agent_id"], core, current, workspace=workspace, vault=vault,
                        preview=preview, repo=repo, logs_dir=logs_dir)
            if start_lane is not None and not rotate:
                out["launch"] = start_lane(out["row"])
    except DeviceConnectError as e:
        _keep(workspace, worker, {**record, "state": e.status})
        raise DeviceConnectError(pp.scrub(str(e), known), status=e.status) from None
    except (pp.ProvisionError, connections.ConnectionsError) as e:
        _keep(workspace, worker, {**record, "state": "failed"})
        raise DeviceConnectError(pp.scrub(f"{pp._said(e)}; the approved agent {got['agent_id']} was not "
                                          "saved", known)) from None
    tell(done_card(worker, who, label, out["agent_id"], server=server, subtitle=subtitle, rotated=rotate),
         done_note(who, label, out["agent_id"], rotated=rotate), "done")
    return out


def _keep(workspace: Optional[Path], worker: str, record: dict) -> None:
    try:
        write_record(workspace, worker, record)
    except OSError:
        pass  # only a later resend reads it; the flow itself never depends on it


def _save(worker, token, agent_id, core, current, *, workspace, vault, preview, repo, logs_dir) -> dict:
    if not pp._MXID_RE.fullmatch(agent_id):
        raise DeviceConnectError("the poll answer names no agent id")
    if pp.origin_of(connections.gateway_url_of(token)) != pp.origin_of(str(core["gateway_url"])):
        raise DeviceConnectError("the poll answer names a gateway that is not the core's")
    if agent_id == core.get("agent_mxid"):
        raise DeviceConnectError("the server answered with the core's own agent, not a new one for the worker")
    if current is not None and agent_id != current.get("agent_mxid"):
        raise DeviceConnectError(f"a rotate must answer for {current.get('agent_mxid')}, not another agent")
    seen = (preview or connections.preview_connection)(token)
    mxid = str(seen.get("agent_mxid") or "") if seen.get("verified") else ""
    if seen.get("authenticated") is not True or mxid != agent_id:
        raise DeviceConnectError(f"the gateway does not confirm the token is agent {agent_id}'s "
                                 f"({seen.get('error') or 'it names ' + (mxid or 'no agent')})")
    if seen.get("owner_mxid") != core.get("owner_mxid"):
        raise DeviceConnectError(f"agent {agent_id} is not owned by the core's owner")
    if current is not None:
        row = connections.update_token(str(current["instance"]), token, repo=repo or _REPO,
                                       logs_dir=logs_dir or Path(connections._workspace(workspace)) / "logs",
                                       workspace=workspace, vault=vault)
        return {"agent_id": agent_id, "status": "rotated", "row": row}
    # A pasted-token add takes no roster lock, so one can still land after `fenced` looked.
    row, created = connections.add_worker_identity(worker, token, expected_server=str(seen.get("server") or ""),
                                                   source="agent_link", preview=seen, workspace=workspace,
                                                   vault=vault)
    if not created:
        raise DeviceConnectError(f"worker {worker} already had a connection ({row['instance']}) by the time the "
                                 "approved token was saved; it was left as it is", status="identity_changed")
    return {"agent_id": agent_id, "status": "ok", "row": row}
