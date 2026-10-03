"""Queue a pending question to the owner in a conversation he reads, and hold it locally.

The skill's provider-neutral half of an ask, fail-open at every step: (1) the question is
QUEUED as a proactive file — to the task's own conversation only for an owner-tier task
in the owner's own DM (`.to-<bridge>` name + `[channel:]` marker), to the owner's DM on
the task's bridge for any other bridge task, else to the owner's DM on the bridge he was
last active on; a drain delivering the file is what makes it sent; (2) the question and
its queue record are saved to the outbox (`pending_questions_outbox`), atomically; (3) the
macOS notification fires last, and a refusal prints the fix instead of a success. The
room-database adapter builds its row on top of `queue_question`; with no room, `ask_owner`
here is the whole ask and the outbox is the record.

Question and context text is embedded, never interpolated: result markers are
neutralized (`[ file:`), so no text can become a drain action.
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
HERE = Path(__file__).resolve().parent
for _p in (REPO / "src", HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from local_record import new_name, write_text_whole
from pending_questions_outbox import Outbox, one_line_title
from proactive_routing import BRIDGE_CHANNELS, proactive_filename
from result_markers import neutralize_markers
from undelivered_quarantine import quarantine_dir
from util_paths import host_label

# A question queued and drained this recently is not re-raised by the reminder.
SENT_QUIET_SEC = 3600
# Grammar of the queue record; the reminder reads it back.
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
    """Unique per call: the row key, the outbox file, the proactive file stem and the report share it."""
    return new_name("ask", now)


def entry_heading(question: str, now: float) -> str:
    return f"## {_iso(now)} — {one_line_title(question)}"


def question_dict(ask_id: str, question: str, context: Optional[str], asked_at: float,
                  default_action: Optional[str], reason: Optional[str], options, priority: str) -> dict:
    """The outbox's `question` record; the store skill's Question dataclass reads the same keys."""
    return {"ask_id": ask_id, "question": question, "context": context, "asked_at": asked_at,
            "default_action": default_action, "reason": reason,
            "options": [list(o) for o in options or ()], "priority": priority or "Medium"}


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
    # one scan recognises the live file and its claims together, so a transient retry that
    # renames a claim back to the live name between two checks cannot read as delivered
    for d, hit in ((Path(results_dir), lambda n: n == name or n.startswith(p.stem + ".sending")),
                   (quarantine_dir(Path(results_dir)), lambda n: n.startswith(p.stem))):
        try:  # scandir raises where glob() reads an unreadable directory as empty; fail closed
            with os.scandir(d) as it:
                if any(hit(e.name) for e in it):
                    return False
        except FileNotFoundError:
            continue
        except OSError:
            return False  # an uninspectable claim or quarantine is not evidence of delivery
    return not p.exists()


def asked_recently(body: str, results_dir: Path, now: Optional[float] = None,
                   within: float = SENT_QUIET_SEC) -> bool:
    """Queued within `within` AND drained. An undrained file is not a delivery
    (the reminder's own contract), so that entry stays due."""
    q = queued_send(body)
    if q is None:
        return False
    name, ts = q
    if (time.time() if now is None else now) - ts >= within:
        return False
    return drained(results_dir, name)


def destination_from_task(text: str, workspace: Optional[Path] = None) -> Destination:
    """Where the task's bridge should put the question. The task's own channel
    only for an owner-tier task in the owner's DM; any other bridge task goes to
    the owner's DM on that bridge; a non-bridge source (voice, chat, cron) yields
    the default. Headers come only from an attested shape, so a body line cannot
    supply a missing tier or DM field; unattested, the verdict is the owner's DM."""
    from task_envelope import attested_task_headers  # noqa: PLC0415
    h = attested_task_headers(text, workspace).headers
    source = (h.get("source") or "").strip()
    if source not in BRIDGE_CHANNELS:
        return Destination()
    tier = (h.get("access_tier") or "").strip().lower()
    channel = (h.get("channel_id") or h.get("source_room_id") or h.get("chat_id") or "").strip()
    if tier == "owner" and channel and _DM_EVIDENCE[source](h):
        return Destination(bridge=source, channel=channel)
    return Destination(bridge=source)


def proactive_body(question: str, context: Optional[str], host: str,
                   dest: Destination, link: Optional[str] = None, intro: Optional[str] = None):
    """(body, routed): routed is True when a `[channel:]` redirect leads the body.
    `link` is where the owner can also answer, when the store knows it; `intro` is one
    extra paragraph the store adds the first time."""
    routed = dest.bridge in _ROOM_MARKER_BRIDGES and bool(dest.channel)
    lines = [f"[channel: {dest.channel}]" if routed else "[dm-only]",
             f"Question for you from the {host} core — it needs your word:", "",
             neutralize_markers(question).strip()]
    if context and context.strip():
        lines += ["", neutralize_markers(context).strip()]
    if link:
        lines += ["", f"Reply here, or set its Status on the row: {neutralize_markers(link)}"]
    else:
        lines += ["", "Reply here."]
    body = "\n".join(lines) + "\n"
    if intro and intro.strip():
        body += "\n" + neutralize_markers(intro).strip() + "\n"
    return body, routed


def write_proactive(results: Path, name: str, body: str) -> Path:
    """Publish atomically: a drain claims proactive-*.txt on sight."""
    return write_text_whole(Path(results) / name, body)


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


def queue_question(question: str, context: Optional[str] = None, task_file: Optional[str] = None,
                   workspace: Optional[Path] = None, host: Optional[str] = None, now: Optional[float] = None,
                   link: Optional[str] = None, intro: Optional[str] = None,
                   default_action: Optional[str] = None, reason: Optional[str] = None,
                   options=(), priority: str = "Medium") -> dict:
    """Queue the proactive file, then hold the question in the outbox. Nothing raises past here:
    `send_error` says why no file was queued, `outbox_error` why no record was saved — and a
    caller with a store must not write its row without `outbox`."""
    from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
    ws = Path(workspace) if workspace else resolve_workspace(migrate=False)
    host = host or host_label()
    now = time.time() if now is None else now
    ask_id = new_ask_id(now)
    out = {"ask_id": ask_id, "heading": entry_heading(question, now), "workspace": str(ws), "host": host,
           "bridge": None, "channel": None, "where": None, "proactive_file": None, "send_error": None,
           "sent_line": None, "outbox": None, "outbox_error": None,
           "question": question_dict(ask_id, question, context, now, default_action, reason, options, priority)}
    dest = Destination()
    if task_file:
        try:
            dest = destination_from_task(Path(task_file).read_text(encoding="utf-8"), ws)
        except (OSError, UnicodeDecodeError) as e:
            out["send_error"] = f"task file unreadable ({e}); queued for the owner's DM instead"
    body, routed = proactive_body(question, context, host, dest, link, intro)
    out["bridge"], out["channel"] = dest.bridge, dest.channel if routed else None
    out["where"] = (f"{dest.bridge} {dest.channel}" if routed else
                    f"{dest.bridge} owner-dm" if dest.bridge else "owner-dm (last-active bridge)")
    try:
        name = proactive_filename(ask_id, dest.bridge)
        write_proactive(ws / "results", name, body)
        out["proactive_file"] = name
    except Exception as e:  # noqa: BLE001 — the question is still recorded below
        out["send_error"] = f"{type(e).__name__}: {e}"
    if out["proactive_file"]:
        out["sent_line"] = f"**Sent:** queued {out['where']} via {out['proactive_file']} at {_iso(time.time())}"
    else:
        out["sent_line"] = f"**Sent:** FAILED — {out['send_error']} at {_iso(time.time())}"
    try:
        out["outbox"] = str(Outbox(ws).save(out["question"], out["sent_line"], now))
    except Exception as e:  # noqa: BLE001 — reported; the caller must not proceed as if held
        out["outbox_error"] = f"outbox: {type(e).__name__}: {e}"
    return out


def ask_owner(question: str, context: Optional[str] = None, urgency: str = "live",
              task_file: Optional[str] = None, workspace: Optional[Path] = None,
              host: Optional[str] = None, now: Optional[float] = None,
              default_action: Optional[str] = None, reason: Optional[str] = None,
              options=(), priority: str = "Medium") -> dict:
    """The whole ask with no room store: queue, hold in the outbox, notify."""
    out = queue_question(question, context, task_file, workspace, host, now, None, None,
                         default_action, reason, options, priority)
    out.update({"record": f"outbox {out['outbox']}" if out["outbox"] else None,
                "db_error": out["outbox_error"] or "no room database store", "link": None,
                "macos": None, "macos_fix": None})
    if urgency == "live":
        out["macos"], out["macos_fix"] = notify_macos(f"Question: {question}")
    return out


def report_lines(out: dict) -> list:
    """What happened, one line per step; a store's own lines follow these."""
    lines = []
    if out.get("outbox") is None and out.get("record"):
        lines.append(f"recorded: {out['record']} — {out['heading'][3:]}")
    elif out.get("outbox"):
        lines.append(f"recorded: OUTBOX {out['outbox']} — {out['heading'][3:]}")
        if out.get("db_error"):
            lines.append(f"recorded: ROOM DATABASE WRITE FAILED — {out['db_error']}; the outbox holds it and "
                         "the next reconcile files it")
    else:
        lines.append(f"recorded: FAILED — {out.get('db_error')} (NOT recorded anywhere; ask by hand)")
    if out.get("link"):
        lines.append(f"row: {out['link']}")
    if out.get("proactive_file"):
        lines.append(f"sent: queued {out['where']} via results/{out['proactive_file']} "
                     "(a bridge drain delivers it; the reminder re-raises an undrained file)")
    else:
        lines.append(f"sent: FAILED — {out.get('send_error')} (the record stands; ask by hand)")
    if out.get("send_error") and out.get("proactive_file"):
        lines.append(f"note: {out['send_error']}")
    if out.get("macos") is True:
        lines.append("macos: notification sent")
    elif out.get("macos") is False:
        lines.append(f"macos: FAILED — {out.get('macos_fix')}")
    return lines
