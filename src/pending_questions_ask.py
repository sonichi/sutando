"""Ask the owner a pending question in a conversation he reads; the per-host
pending-questions.md is the ledger of what was queued, not the channel.

Order, fail-open at every step: (1) the entry is written, keyed by a unique ask
id, through pending_questions_store.write_question — to the room database the
caller injected, and always to the top of the file's active region, the shadow
every file-only reader reads; (2) the question is QUEUED as a proactive file — to the task's own conversation only for an owner-tier task in
the owner's own DM (`.to-<bridge>` name + `[channel:]` marker), to the owner's
DM on the task's bridge for any other bridge task, else to the owner's DM on
the bridge he was last active on; (3) the queue record is stamped on the entry's
`**Sent:**` line in every store holding it — a drain delivering the file is what makes it sent, and the
reminder treats an undrained file as not yet asked; (4) the macOS notification
fires last, and a refusal prints the fix instead of a success.

Question and context text is embedded, never interpolated: result markers and
ledger field tokens are neutralized (`[ file:`, `** Status:`), the ledger copy is
a block quote whose open backtick spans are closed, so no text can cut the active
region, split the entry or become a drain action.
"""
from __future__ import annotations

import glob
import os
import re
import secrets
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from local_task_protocol import parse_task_headers_lenient
from pending_questions_store import (FileStore, Question, entry_heading,  # noqa: F401
                                     placeholder, quote_for_ledger, resync, write_question)
from pending_questions_store import ledger_entry as _store_ledger_entry
from proactive_routing import BRIDGE_CHANNELS, proactive_filename
from result_markers import neutralize_markers
from undelivered_quarantine import quarantine_dir
from util_paths import host_label, personal_path

# A question queued and drained this recently is not re-raised by the reminder.
SENT_QUIET_SEC = 3600
# Grammar of the ledger's queue record; the notifier reads the stamp back.
SENT_RE = re.compile(
    r"^\*\*Sent:\*\*\s+queued\s+(?P<where>.*?)\s+via\s+(?P<file>proactive-\S+\.txt)\s+at\s+"
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*$", re.MULTILINE)
# Bridges whose drain honours a `[channel:]` room redirect (telegram drops it).
_ROOM_MARKER_BRIDGES = frozenset({"discord", "slack", "ag2space"})
MACOS_FIX = ("allow notifications for your terminal app under System Settings > "
             "Notifications, or run from a session where osascript is permitted")

# Positive evidence, per bridge, that the task's channel is the owner's own DM.
_DM_EVIDENCE = {
    "ag2space": lambda h: (h.get("channel_kind") or "").strip().lower() == "dm",
    "discord": lambda h: (h.get("guild_name") or "").strip() == "DM"
    and (h.get("channel_name") or "").strip() == "DM",
    "slack": lambda h: (h.get("channel_id") or "").strip().startswith("D"),
    "telegram": lambda h: True,  # the bridge delivers to the owner's chat whatever the id
}


@dataclass
class Destination:
    bridge: Optional[str] = None     # None: the owner's last-active bridge
    channel: Optional[str] = None    # the task's conversation; only an owner DM qualifies


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_ask_id(now: float) -> str:
    """Unique per call: the ledger token, the proactive file stem and the report share it."""
    return f"ask-{int(now * 1000)}-{os.getpid()}-{secrets.token_hex(3)}"


def queued_send(body: str):
    """(proactive file name, epoch) of the entry's queue record; None when it
    carries none or the stamp does not parse (a bad stamp reads as no stamp)."""
    m = SENT_RE.search(body or "")
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group("ts"), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    return m.group("file"), ts


def sent_at(body: str) -> Optional[float]:
    q = queued_send(body)
    return None if q is None else q[1]


def drained(results_dir: Path, name: str) -> bool:
    """A queued file some drain took: neither the file, any claim of it (`.sending`,
    `.sending.<pid>`) nor a parked copy in the undelivered/ quarantine remains."""
    if not name.startswith("proactive-"):
        return False
    p = Path(results_dir) / name
    stem = glob.escape(p.stem)
    left = [Path(results_dir).glob(stem + ".sending*"),
            quarantine_dir(Path(results_dir)).glob(stem + "*")]
    return not p.exists() and all(next(g, None) is None for g in left)


def asked_here(results_dir: Path, ask_id: str) -> bool:
    """This host queued that ask: its proactive file, a claim, an archived or a
    parked copy is in this host's results/ (which no other host shares)."""
    if not ask_id.startswith("ask-"):
        return False
    pattern = str(Path(results_dir) / "**" / f"proactive-{glob.escape(ask_id)}*")
    return next(iter(glob.iglob(pattern, recursive=True)), None) is not None


def asked_recently(body: str, results_dir: Path, now: Optional[float] = None,
                   within: float = SENT_QUIET_SEC) -> bool:
    """Queued within `within` AND drained. An undrained file is not a delivery
    (check-pending-questions' own contract), so that entry stays due."""
    q = queued_send(body)
    if q is None:
        return False
    name, ts = q
    if (time.time() if now is None else now) - ts >= within:
        return False
    return drained(results_dir, name)


def destination_from_task(text: str) -> Destination:
    """Where the task's bridge should put the question. The task's own channel
    only for an owner-tier task in the owner's DM; any other bridge task goes to
    the owner's DM on that bridge; a non-bridge source (voice, chat, cron) yields
    the default."""
    h = parse_task_headers_lenient(text).headers
    source = (h.get("source") or "").strip()
    if source not in BRIDGE_CHANNELS:
        return Destination()
    tier = (h.get("access_tier") or "").strip().lower()
    channel = (h.get("channel_id") or h.get("source_room_id") or h.get("chat_id") or "").strip()
    if tier == "owner" and channel and _DM_EVIDENCE[source](h):
        return Destination(bridge=source, channel=channel)
    return Destination(bridge=source)


def ledger_path(workspace: Path, host: str) -> Path:
    """The file the readers resolve when one exists; else the per-host home,
    which personal_path probes first on read but never hands out as a write target."""
    p = Path(personal_path("pending-questions.md", workspace))
    return p if p.exists() else workspace / "hosts" / host / "pending-questions.md"


def ledger_entry(question: str, context: Optional[str], now: float, ask_id: str) -> str:
    return _store_ledger_entry(Question(ask_id, question, context, now))


def proactive_body(question: str, context: Optional[str], host: str,
                   dest: Destination, link: Optional[str] = None):
    """(body, routed): routed is True when a `[channel:]` redirect leads the body.
    `link` is the question's row in the owner's room database, when it has one."""
    routed = dest.bridge in _ROOM_MARKER_BRIDGES and bool(dest.channel)
    lines = [f"[channel: {dest.channel}]" if routed else "[dm-only]",
             f"Question for you from the {host} core — it needs your word:", "",
             neutralize_markers(question).strip()]
    if context and context.strip():
        lines += ["", neutralize_markers(context).strip()]
    if link:
        lines += ["", f"Reply here, or set its Status on the row: {neutralize_markers(link)}"]
    else:
        lines += ["", f"Reply here. Ledger: hosts/{host}/pending-questions.md"]
    return "\n".join(lines) + "\n", routed


def write_proactive(results: Path, name: str, body: str) -> Path:
    """Publish atomically: a drain claims proactive-*.txt on sight."""
    results.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(results), prefix=f".{name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, results / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return results / name


def notify_macos(text: str) -> tuple:
    """(ok, fix). A refused or failed osascript names the fix; it never claims ok."""
    esc = text.replace("\\", "\\\\").replace('"', '\\"')[:200]
    try:
        r = subprocess.run(["osascript", "-e",
                            f'display notification "{esc}" with title "Sutando"'],
                           capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, OSError):
        return False, "osascript not found on PATH (not macOS, or a bare PATH); " + MACOS_FIX
    except subprocess.TimeoutExpired:
        return False, "osascript did not return within 15s; " + MACOS_FIX
    if r.returncode != 0:
        err = " ".join((r.stderr or "").split())[:120]
        return False, f"osascript exit {r.returncode} ({err or 'no stderr'}); {MACOS_FIX}"
    return True, None


def ask_owner(question: str, context: Optional[str] = None, urgency: str = "live",
              task_file: Optional[str] = None, workspace: Optional[Path] = None,
              host: Optional[str] = None, now: Optional[float] = None, store=None,
              default_action: Optional[str] = None, reason: Optional[str] = None,
              options=(), priority: str = "Medium") -> dict:
    """Ledger, queue, stamp, notify — each step reported, none raising past here.
    `store` is a room-database store the adapter injected; None keeps the file only."""
    from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
    ws = Path(workspace) if workspace else resolve_workspace(migrate=False)
    host = host or host_label()
    now = time.time() if now is None else now
    ask_id = new_ask_id(now)
    out = {"ask_id": ask_id, "ledger": None, "heading": entry_heading(question, now),
           "ledger_error": None, "bridge": None, "channel": None, "where": None,
           "proactive_file": None, "send_error": None, "macos": None, "macos_fix": None,
           "store": None, "link": None, "db_error": None, "resync": None}

    pq = ledger_path(ws, host)
    out["ledger"] = str(pq)
    file_store = FileStore(pq)
    if store is not None:
        synced, errors = resync(file_store, store, claim=lambda a: asked_here(ws / "results", a))
        out["resync"] = {"synced": synced, "errors": errors}
    q = Question(ask_id, question, context, now, default_action, reason, tuple(options or ()),
                 priority)
    w = write_question(q, file_store, store)
    out["ledger_error"], out["db_error"], out["link"] = w.error, w.db_error, w.link
    if w.stores:
        out["store"] = "+".join(h.kind for h in w.stores)
        out["ledger"] = " and ".join(h.where(ask_id) for h in w.stores)

    dest = Destination()
    if task_file:
        try:
            dest = destination_from_task(Path(task_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as e:
            out["send_error"] = f"task file unreadable ({e}); queued for the owner's DM instead"
    body, routed = proactive_body(question, context, host, dest, out["link"])
    out["bridge"], out["channel"] = dest.bridge, dest.channel if routed else None
    out["where"] = (f"{dest.bridge} {dest.channel}" if routed else
                    f"{dest.bridge} owner-dm" if dest.bridge else "owner-dm (last-active bridge)")
    try:
        name = proactive_filename(ask_id, dest.bridge)
        write_proactive(ws / "results", name, body)
        out["proactive_file"] = name
    except Exception as e:  # noqa: BLE001 — the ledger entry already stands
        out["send_error"] = f"{type(e).__name__}: {e}"

    if out["proactive_file"]:
        sent_line = f"**Sent:** queued {out['where']} via {out['proactive_file']} at {_iso(time.time())}"
    else:
        sent_line = f"**Sent:** FAILED — {out['send_error']} at {_iso(time.time())}"
    for holder in w.stores:
        try:
            holder.stamp(ask_id, sent_line)
        except Exception as e:  # noqa: BLE001
            if _carries(holder, ask_id, sent_line):
                continue  # a reconciling pass copied this run's record across first
            err = f"{type(e).__name__}: {e}"
            if holder.kind == "file":
                out["ledger_error"] = err
            else:
                out["db_error"] = f"stamp: {err}"

    if urgency == "live":
        out["macos"], out["macos_fix"] = notify_macos(f"Question: {question}")
    return out


def _carries(holder, ask_id: str, sent_line: str) -> bool:
    try:
        return any(e["ask_id"] == ask_id and sent_line in e["body"] for e in holder.entries())
    except Exception:  # noqa: BLE001
        return False


def report_lines(out: dict) -> list:
    lines = [f"ledger: {out['ledger']} — {out['heading'][3:]}"]
    if out.get("db_error"):
        lines.append(f"ledger: ROOM DATABASE WRITE FAILED — {out['db_error']}; the file holds it, "
                     "and the next run with the database copies it there")
    if out.get("link"):
        lines.append(f"row: {out['link']}")
    for err in (out.get("resync") or {}).get("errors") or []:
        lines.append(f"resync: FAILED — {err}")
    if (out.get("resync") or {}).get("synced"):
        lines.append(f"resync: brought {len(out['resync']['synced'])} entr(ies) level between the file "
                     "and the room database")
    if out["ledger_error"]:
        lines.append(f"ledger: FAILED — {out['ledger_error']}")
    if out["proactive_file"]:
        lines.append(f"sent: queued {out['where']} via results/{out['proactive_file']} "
                     "(a bridge drain delivers it; the reminder re-raises an undrained file)")
    else:
        lines.append(f"sent: FAILED — {out['send_error']} (the ledger entry stands; ask by hand)")
    if out["send_error"] and out["proactive_file"]:
        lines.append(f"note: {out['send_error']}")
    if out["macos"] is True:
        lines.append("macos: notification sent")
    elif out["macos"] is False:
        lines.append(f"macos: FAILED — {out['macos_fix']}")
    return lines
