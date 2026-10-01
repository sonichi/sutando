"""Ask the owner a pending question in a conversation he reads; the per-host
pending-questions.md is the ledger of what was sent, not the channel.

Order, fail-open at every step: (1) the ledger entry is inserted at the top of
the active region; (2) the question is SENT — to the task's own conversation
when a task file is given (`.to-<bridge>` name + `[channel:]` body marker), else
to the owner's DM on the bridge he was last active on (an untagged proactive
file); (3) the send is recorded on the entry's `**Sent:**` line; (4) the macOS
notification fires last, and a refusal prints the fix instead of a success.
Delivery itself stays with the bridges' proactive drains — nothing here sends.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from local_task_protocol import parse_task_headers_lenient
from proactive_routing import BRIDGE_CHANNELS, proactive_filename
from util_paths import _host_label, personal_path

# A question sent to the owner this recently is not re-raised by the reminder.
SENT_QUIET_SEC = 3600
LOCK_WAIT_SEC = 10
SENDING = "**Sent:** (sending)"
# Grammar of the ledger's send record; the notifier reads the stamp back.
SENT_RE = re.compile(
    r"^\*\*Sent:\*\*\s+(?!\(sending\)|FAILED)(?P<what>.*?)\s+at\s+"
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*$", re.MULTILINE)
# Bridges whose drain honours a `[channel:]` room redirect (telegram drops it).
_ROOM_MARKER_BRIDGES = frozenset({"discord", "slack", "ag2space"})
MACOS_FIX = ("allow notifications for your terminal app under System Settings > "
             "Notifications, or run from a session where osascript is permitted")


@dataclass
class Destination:
    bridge: Optional[str] = None     # None: the owner's last-active bridge
    channel: Optional[str] = None    # room/channel/chat id when the task names one


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sent_at(body: str) -> Optional[float]:
    """Epoch of the entry's send record, None when it carries none."""
    m = SENT_RE.search(body or "")
    if not m:
        return None
    return datetime.strptime(m.group("ts"), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).timestamp()


def recently_sent(body: str, now: Optional[float] = None,
                  within: float = SENT_QUIET_SEC) -> bool:
    ts = sent_at(body)
    return ts is not None and (time.time() if now is None else now) - ts < within


def destination_from_task(text: str) -> Destination:
    """The conversation a task came from, as a bridge + channel the proactive
    drains can address; a non-bridge source (voice, chat, cron) yields the default."""
    h = parse_task_headers_lenient(text).headers
    source = (h.get("source") or "").strip()
    if source not in BRIDGE_CHANNELS:
        return Destination()
    channel = h.get("channel_id") or h.get("source_room_id") or h.get("chat_id")
    return Destination(bridge=source, channel=(channel or "").strip() or None)


def ledger_path(workspace: Path, host: str) -> Path:
    """The file the readers resolve when one exists; else the per-host home,
    which personal_path probes first on read but never hands out as a write target."""
    p = Path(personal_path("pending-questions.md", workspace))
    return p if p.exists() else workspace / "hosts" / host / "pending-questions.md"


def entry_heading(question: str, now: float) -> str:
    title = " ".join(question.split())[:120] or "(empty question)"
    return f"## {_iso(now)} — {title}"


def ledger_entry(question: str, context: Optional[str], now: float) -> str:
    lines = [entry_heading(question, now), "", question.strip()]
    if context and context.strip():
        lines += ["", f"Context: {context.strip()}"]
    lines += ["", "**Status:** open", SENDING, ""]
    return "\n".join(lines) + "\n"


def proactive_body(question: str, context: Optional[str], host: str,
                   dest: Destination) -> str:
    lines = []
    if dest.bridge in _ROOM_MARKER_BRIDGES and dest.channel:
        lines.append(f"[channel: {dest.channel}]")
    else:
        lines.append("[dm-only]")
    lines += [f"Question for you from the {host} core — it needs your word:", "",
              question.strip()]
    if context and context.strip():
        lines += ["", context.strip()]
    lines += ["", f"Reply here. Ledger: hosts/{host}/pending-questions.md"]
    return "\n".join(lines) + "\n"


def _with_lock(pq: Path, fn):
    """mkdir lock shared with every other writer of this file; a timeout leaves
    both the file and the foreign lock alone and reports it."""
    lock = Path(str(pq) + ".lock")
    pq.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT_SEC
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                return f"could not acquire {lock} within {LOCK_WAIT_SEC}s"
            time.sleep(0.1)
    try:
        return fn()
    finally:
        try:
            lock.rmdir()
        except OSError:
            pass


def _replace_file(pq: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(pq.parent), prefix=f".{pq.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, pq)


def insert_entry(pq: Path, entry: str) -> Optional[str]:
    """Top of the active region (below a `# ` title line when the file has one),
    never an EOF append, which lands under the `# Resolved` divider uncounted."""
    def _do():
        old = pq.read_text(encoding="utf-8") if pq.exists() else ""
        first, _, rest = old.partition("\n")
        if re.match(r"^# \S", first):
            new = f"{first}\n\n{entry}{rest.lstrip(chr(10))}"
        else:
            new = entry + old
        _replace_file(pq, new)
        return None
    return _with_lock(pq, _do)


def record_sent(pq: Path, heading: str, sent_line: str) -> Optional[str]:
    """Rewrite the entry's `(sending)` placeholder with the outcome."""
    def _do():
        text = pq.read_text(encoding="utf-8")
        start = text.find(heading + "\n")
        if start < 0:
            return "ledger entry not found"
        end = text.find("\n## ", start + 1)
        end = len(text) if end < 0 else end
        section = text[start:end]
        if SENDING not in section:
            return "send placeholder not found"
        _replace_file(pq, text[:start] + section.replace(SENDING, sent_line, 1) + text[end:])
        return None
    return _with_lock(pq, _do)


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
              host: Optional[str] = None, now: Optional[float] = None) -> dict:
    """Ledger, send, record, notify — each step reported, none raising past here."""
    from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
    ws = Path(workspace) if workspace else resolve_workspace(migrate=False)
    host = host or _host_label()
    now = time.time() if now is None else now
    out = {"ledger": None, "heading": entry_heading(question, now), "ledger_error": None,
           "bridge": None, "channel": None, "proactive_file": None, "send_error": None,
           "macos": None, "macos_fix": None}

    pq = ledger_path(ws, host)
    out["ledger"] = str(pq)
    try:
        out["ledger_error"] = insert_entry(pq, ledger_entry(question, context, now))
    except Exception as e:  # noqa: BLE001 — a lost ledger line must not lose the send
        out["ledger_error"] = f"{type(e).__name__}: {e}"

    dest = Destination()
    if task_file:
        try:
            dest = destination_from_task(Path(task_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as e:
            out["send_error"] = f"task file unreadable ({e}); sent to the owner's DM instead"
    out["bridge"], out["channel"] = dest.bridge, dest.channel
    try:
        name = proactive_filename(f"ask-{int(now * 1000)}-{os.getpid()}", dest.bridge)
        write_proactive(ws / "results", name, proactive_body(question, context, host, dest))
        out["proactive_file"] = name
    except Exception as e:  # noqa: BLE001 — the ledger entry already stands
        out["send_error"] = f"{type(e).__name__}: {e}"

    if out["proactive_file"]:
        where = f"{dest.bridge} {dest.channel}" if dest.bridge and dest.channel else (
            f"{dest.bridge} owner-dm" if dest.bridge else "owner-dm (last-active bridge)")
        sent_line = f"**Sent:** {where} via {out['proactive_file']} at {_iso(time.time())}"
    else:
        sent_line = f"**Sent:** FAILED — {out['send_error']} at {_iso(time.time())}"
    if out["ledger_error"] is None:
        try:
            out["ledger_error"] = record_sent(pq, out["heading"], sent_line)
        except Exception as e:  # noqa: BLE001
            out["ledger_error"] = f"{type(e).__name__}: {e}"

    if urgency == "live":
        out["macos"], out["macos_fix"] = notify_macos(f"Question: {question}")
    return out


def report_lines(out: dict) -> list:
    lines = [f"ledger: {out['ledger']} — {out['heading'][3:]}"]
    if out["ledger_error"]:
        lines.append(f"ledger: FAILED — {out['ledger_error']}")
    if out["proactive_file"]:
        where = (f"{out['bridge']} {out['channel']}" if out["bridge"] and out["channel"]
                 else (f"{out['bridge']} owner-dm" if out["bridge"] else "owner's DM on the last-active bridge"))
        lines.append(f"sent: {where} via results/{out['proactive_file']}")
    else:
        lines.append(f"sent: FAILED — {out['send_error']} (the ledger entry stands; ask by hand)")
    if out["send_error"] and out["proactive_file"]:
        lines.append(f"note: {out['send_error']}")
    if out["macos"] is True:
        lines.append("macos: notification sent")
    elif out["macos"] is False:
        lines.append(f"macos: FAILED — {out['macos_fix']}")
    return lines
