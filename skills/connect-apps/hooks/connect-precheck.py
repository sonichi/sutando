#!/usr/bin/env python3
"""Claude Code hook: tells the agent up front which apps a task needs and whether they are connected.

Reads the hook payload on stdin ({hook_event_name, session_id, tool_name, tool_input}). The FIRST time
a session's tool input names a task file (tasks/task-….txt), the task's message text is matched
against precheck_apps.json (keyword -> Station toolkit slug), the connected set is read from the
connect-apps read cache (<workspace>/state/connect-cache.json, written by connectors.py; never the
network: a cold cache says nothing about connectedness), and one line of `additionalContext` is
printed:

  connect-apps precheck: needs_connect=googlecalendar (Google Calendar); connected=linear;
  room_kind=dm; run: … connectors.py card googlecalendar --room … --owner-from-task …

The same script is declared for PreToolUse and PostToolUse (manifest.json): Claude Code documents
`additionalContext` for PostToolUse, and the PreToolUse copy reaches the agent on versions that honor
it there too; each event speaks once per task per session (state/connect-precheck.sessions.json,
bounded). Advisory only: a miss changes nothing, the skill still decides. Always exits 0; fail-open.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import secrets
import sys
import time
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
TABLE_PATH = SKILL_DIR / "precheck_apps.json"
SCRIPT = SKILL_DIR / "scripts" / "connectors.py"
# Mirrors connectors.py (CACHE_NAME / CACHE_TTL_S); the CLI test pins them equal.
CACHE_NAME = "connect-cache.json"
CACHE_TTL_S = 30.0
SESSIONS_NAME = "connect-precheck.sessions.json"
MAX_SESSIONS = 50
MAX_TEXT = 2000
MAX_CONNECTED = 15
TASK_ID = r"task-(?!cron-|bench-|workstream-|project-grouping-)[A-Za-z0-9][\w-]*"
# An optional absolute prefix, then tasks/<id>.txt; tasks/archive/… never matches.
TASK_FILE = re.compile(rf"((?:/[^\s\"'`]*?/)?tasks/({TASK_ID})\.txt)\b")
HEADER_LINE = re.compile(r"^([A-Za-z_]+): ?(.*)$")
BELOW_TASK_FIELD = re.compile(r"^[a-z_]+: ")
EVENTS = ("PreToolUse", "PostToolUse")


def load_json(path: Path, default):
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return v if isinstance(v, type(default)) else default


# --------------------------------------------------------------------------- keyword table


def load_table(path: Path = TABLE_PATH) -> list[dict]:
    """[{slug, name, patterns}] in file order; malformed entries are skipped."""
    data = load_json(path, {})
    out = []
    for app in data.get("apps") if isinstance(data.get("apps"), list) else []:
        if not isinstance(app, dict) or not isinstance(app.get("slug"), str):
            continue
        words = [k for k in app.get("keywords") or [] if isinstance(k, str) and k.strip()]
        patterns = [re.compile(r"(?<![a-z0-9])" + re.escape(k.strip().lower()).replace(r"\ ", r"\s+") + r"(?![a-z0-9])")
                    for k in words]
        out.append({"slug": app["slug"], "name": app.get("name") or app["slug"], "patterns": patterns,
                    "keywords": words})
    return out


def match_apps(text: str, table: list[dict]) -> list[dict]:
    """The apps the text names, in table order, each with the keyword that matched."""
    low = " ".join(text.lower().split())
    hits = []
    for app in table:
        for kw, pat in zip(app["keywords"], app["patterns"]):
            if pat.search(low):
                hits.append({"slug": app["slug"], "name": app["name"], "keyword": kw})
                break
    return hits


# --------------------------------------------------------------------------- the task


def task_refs(blob: str) -> list[tuple[str, str]]:
    """(path-as-written, task id) per task file the tool input names, first occurrence each."""
    seen: dict[str, str] = {}
    for path, tid in TASK_FILE.findall(blob):
        seen.setdefault(tid, path)
    return [(p, t) for t, p in seen.items()]


def task_fields(text: str) -> dict:
    """The task's headers (first value wins, above or below the body) and its message text: the
    `task:` line plus the body lines that are neither a writer field nor an instruction block."""
    fields: dict = {}
    body: list[str] = []
    in_body = False
    for ln in text.splitlines():
        if ln.startswith("==="):
            break
        m = HEADER_LINE.match(ln)
        if m and m.group(1) == "task" and not in_body:
            in_body = True
            fields.setdefault("task", m.group(2).strip())
            body.append(m.group(2))
            continue
        if m and (not in_body or BELOW_TASK_FIELD.match(ln)):
            fields.setdefault(m.group(1), m.group(2).strip())
            continue
        if in_body:
            body.append(ln)
    fields["text"] = " ".join(" ".join(body).split())[:MAX_TEXT]
    return fields


def room_kind(fields: dict) -> str:
    """dm | room | unknown: channel_kind first, then room_member_count; nothing else is guessed."""
    kind = (fields.get("channel_kind") or "").strip().lower()
    if kind in ("dm", "room"):
        return kind
    count = (fields.get("room_member_count") or "").strip()
    if count.isdigit():
        return "dm" if int(count) == 2 else "room"
    return "unknown"


def card_eligible(fields: dict) -> bool:
    """The skill's Step 0: only the owner's own AG2 Space message task gets a card."""
    return (fields.get("source") == "ag2space" and fields.get("access_tier") == "owner"
            and "collaborator" not in fields and bool(fields.get("source_message_id")))


# --------------------------------------------------------------------------- the cache (read-only)


def _finite(x):
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def connected_set(ws: Path, now: float, ttl: float = CACHE_TTL_S) -> set[str] | None:
    """The active toolkits from a fresh cache entry, or None when the cache is cold, stale, torn
    or skewed into the future. Never a network read: a hook runs before every tool call."""
    entry = load_json(ws / "state" / CACHE_NAME, {}).get("connectors")
    if not isinstance(entry, dict):
        return None
    ts = _finite(entry.get("value_ts"))
    if ts is None or not -ttl <= now - ts < ttl:
        return None
    active = entry.get("active")
    return {str(s).lower() for s in active if isinstance(s, str)} if isinstance(active, list) else None


# --------------------------------------------------------------------------- first touch


def first_touch(ws: Path, sid: str, tid: str, event: str, now: float) -> bool:
    """True once per (session, task, event). The file keeps the newest MAX_SESSIONS sessions."""
    path = ws / "state" / SESSIONS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        data = load_json(path, {})
        sessions = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
        entry = sessions.get(sid) if isinstance(sessions.get(sid), dict) else {"ts": now, "tasks": {}}
        tasks = entry.get("tasks") if isinstance(entry.get("tasks"), dict) else {}
        events = tasks.get(tid) if isinstance(tasks.get(tid), list) else []
        if event in events:
            return False
        tasks[tid] = events + [event]
        entry.update(ts=now, tasks=tasks)
        sessions[sid] = entry
        if len(sessions) > MAX_SESSIONS:
            for old in sorted(sessions, key=lambda s: _finite(sessions[s].get("ts")) or 0)[: len(sessions) - MAX_SESSIONS]:
                sessions.pop(old, None)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
        tmp.write_text(json.dumps({"version": 1, "sessions": sessions}), encoding="utf-8")
        os.replace(tmp, path)
        return True


# --------------------------------------------------------------------------- the line


def context_line(hits: list[dict], connected: set[str] | None, fields: dict, tid: str) -> str:
    kind = room_kind(fields)
    if connected is None:
        parts = ["mentions=" + ",".join(f"{h['slug']} ({h['name']})" for h in hits),
                 "connected=unknown (cache cold; connectors.py card or status reads the cloud)"]
        missing = [h["slug"] for h in hits]
    else:
        missing = [h["slug"] for h in hits if h["slug"] not in connected]
        parts = ["needs_connect=" + (",".join(f"{h['slug']} ({h['name']})" for h in hits if h["slug"] in missing) or "none"),
                 "connected=" + (",".join(sorted(connected)[:MAX_CONNECTED]) or "none")]
    parts.append(f"room_kind={kind}")
    if missing and card_eligible(fields):
        room = fields.get("source_room_id") or fields.get("channel_id") or "<room>"
        private = " --private" if kind == "room" else ("" if kind == "dm" else " [--private unless the room is your DM]")
        parts.append(
            f"run: python3 {SCRIPT} card {' '.join(missing)} --room '{room}' --reply-to "
            f"'{fields.get('source_message_id')}' --task {tid} --owner-from-task{private} --request-file - "
            "(heredoc: the owner request, verbatim); then for mode dm post its message with room.message.send")
    elif missing:
        parts.append("run: no card (not the owner's own AG2 Space message): say the app isn't connected, text only")
    return "connect-apps precheck: " + "; ".join(parts)


def workspace_for(path_as_written: str) -> Path | None:
    """The workspace from an absolute tasks/… path; else the configured one, resolved lazily."""
    if path_as_written.startswith("/"):
        return Path(path_as_written).parent.parent
    try:
        sys.path.insert(0, str(SKILL_DIR.parents[1] / "src"))
        from workspace_default import resolve_workspace  # noqa: PLC0415
        return Path(resolve_workspace())
    except Exception:  # noqa: BLE001 — no workspace, no verdict
        return None


def handle(payload: dict, now: float | None = None, table: list[dict] | None = None) -> str | None:
    """The additionalContext line this payload earns, or None."""
    now = time.time() if now is None else now
    sid = payload.get("session_id")
    event = payload.get("hook_event_name")
    inp = payload.get("tool_input")
    if not isinstance(sid, str) or not sid or event not in EVENTS or not isinstance(inp, dict):
        return None
    refs = task_refs(json.dumps(inp, ensure_ascii=False))
    if not refs:
        return None
    path_as_written, tid = refs[0]
    ws = workspace_for(path_as_written)
    if ws is None:
        return None
    try:
        text = (ws / "tasks" / f"{tid}.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not first_touch(ws, sid, tid, event, now):
        return None
    fields = task_fields(text)
    hits = match_apps(fields.get("text") or "", load_table() if table is None else table)
    if not hits:
        return None
    return context_line(hits, connected_set(ws, now), fields, tid)


def main(stdin=sys.stdin, stdout=sys.stdout) -> int:
    try:
        payload = json.loads(stdin.read() or "{}")
        line = handle(payload) if isinstance(payload, dict) else None
        if line:
            stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": payload.get("hook_event_name"), "additionalContext": line}}))
            stdout.write("\n")
    except Exception:  # noqa: BLE001 — a hook must never block the tool it observed
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
