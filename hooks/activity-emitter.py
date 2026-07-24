#!/usr/bin/env python3
"""activity-emitter — async Claude Code hook that journals the core's activity
as AWP activity objects (Activity outbox Phase 2, step 1 — owner's pick
2026-07-24, delivered via the human-action bridge's first live card).

The AWP roadmap's Phase 2 is a durable Agent Activity outbox: the owner (and
later the Workspace) should be able to see WHAT the agent is doing — session
lifecycle, tool activity, turn completions — without attaching to tmux. Claude
Code hooks are the structured source (NOT tmux scraping): each hook fires with
JSON on stdin; this emitter normalizes it to an AWP activity object and appends
it to a durable local journal. Upstream HTTP delivery is a later step (needs a
broker /v1/activities endpoint); the journal means nothing is lost meanwhile,
and the dashboard gets a local activity feed for free.

Design (workspace notes/tasks-events/human_action_bridge_design.md §2.6):
  hook JSON → normalize → append JSONL line to
  <workspace>/state/activity-journal/YYYY-MM-DD.jsonl

Hook → activity mapping (first ring, per the usecase doc):
  SessionStart        → agent.session.started
  UserPromptSubmit    → task.execution.started
  PreToolUse          → agent.tool.started
  PostToolUse         → agent.tool.completed
  PostToolUseFailure  → agent.tool.failed
  Notification        → agent.attention.required
  Stop                → task.turn.completed
  SessionEnd          → agent.session.ended
  (unknown hook)      → agent.activity (generic — forward-compatible)

Attribution: if the Execution Binding Registry file exists
(<workspace>/state/bindings/active-execution.json, written by the Core at task
pickup), its task_id/room_id ride on every activity so downstream consumers can
group by task. Absent registry → activities still journal, unattributed.

Invariants:
  - FAIL-OPEN and FAST: any error → exit 0 silently. An emitter crash or slow
    path must never wedge or lag the core (register with "async": true).
  - Append-only JSONL, one line per activity, day-rotated by filename. No
    fsync — activities are telemetry, not decisions (the human-action store is
    the durable-by-contract one); a crash losing the tail of a telemetry
    journal is acceptable, wedging the core is not.
  - No secrets: tool_input is reduced to a short display summary, never the
    full payload (commands can carry tokens).

Registration (NOT auto-registered yet — see hooks/README.md): add async
command-hook entries for the events above pointing at this file, argv[1] =
the hook name (SessionStart etc.) as a fallback when stdin lacks
hook_event_name. Test: tests/activity-emitter.test.py.
Test-only env override: SUTANDO_ACTIVITY_DIR (journal dir).
"""
import json
import os
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

HOOK_TO_TYPE = {
    "SessionStart": "agent.session.started",
    "UserPromptSubmit": "task.execution.started",
    "PreToolUse": "agent.tool.started",
    "PostToolUse": "agent.tool.completed",
    "PostToolUseFailure": "agent.tool.failed",
    "Notification": "agent.attention.required",
    "Stop": "task.turn.completed",
    "SessionEnd": "agent.session.ended",
}

_SUMMARY_LIMIT = 160


def _workspace() -> Path:
    """CLAUDE_CONFIG_DIR walk — same derivation as context-source-guard /
    human-action-bridge (no subprocess, no __file__ walk, deploy-safe)."""
    p = os.path.normpath(os.environ.get("CLAUDE_CONFIG_DIR")
                         or os.path.expanduser("~/.claude"))
    while True:
        if os.path.basename(p) == ".claude-sutando":
            return Path(os.path.dirname(p))
        parent = os.path.dirname(p)
        if parent == p:
            return Path(os.path.expanduser("~/sutando-workspace"))
        p = parent


def _journal_dir(ws: Path) -> Path:
    override = os.environ.get("SUTANDO_ACTIVITY_DIR")
    return Path(override) if override else ws / "state" / "activity-journal"


def _binding(ws: Path) -> dict:
    try:
        with open(ws / "state" / "bindings" / "active-execution.json") as f:
            b = json.load(f)
        return {k: b[k] for k in ("task_id", "room_id", "generation") if k in b}
    except (OSError, ValueError):
        return {}


def _tool_summary(data: dict) -> dict:
    """Reduce tool info to display-safe fields — never the raw input payload."""
    name = data.get("tool_name")
    if not name:
        return {}
    ti = data.get("tool_input") or {}
    # NOTE: deliberately NOT ti["command"] — raw command lines are the
    # likeliest secret carriers; Bash calls surface via their description.
    hint = (ti.get("description") or ti.get("file_path") or ti.get("path")
            or ti.get("pattern") or "")
    if not hint and ti.get("url"):
        # URLs carry secrets in query strings/fragments (presigned sigs, OAuth
        # codes) AND in userinfo (https://user:token@host — netloc includes it,
        # so reusing netloc leaked credentials; second review). Journal
        # scheme + hostname[:port] + path ONLY, authority REBUILT from parts.
        try:
            u = urllib.parse.urlsplit(str(ti["url"]))
            host = u.hostname or ""
            authority = host + (f":{u.port}" if u.port else "")
            hint = urllib.parse.urlunsplit(
                (u.scheme, authority, u.path, "", "")) if host else ""
        except ValueError:
            hint = ""
    hint = str(hint).splitlines()[0][:_SUMMARY_LIMIT] if hint else ""
    return {"tool": {"kind": name, **({"display": hint} if hint else {})}}


def build_activity(data: dict, argv_hook: "str | None" = None,
                   ws: "Path | None" = None) -> dict:
    ws = ws or _workspace()
    hook = data.get("hook_event_name") or argv_hook or "unknown"
    activity = {
        "activity_id": f"act_{uuid.uuid4().hex[:12]}",
        "type": HOOK_TO_TYPE.get(hook, "agent.activity"),
        "hook": hook,
        "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "execution": {"runtime": "claude-code",
                      "session_id": data.get("session_id")},
        **_binding(ws),
        **_tool_summary(data),
    }
    if hook == "Notification" and data.get("message"):
        activity["summary"] = str(data["message"])[:_SUMMARY_LIMIT]
    return activity


def append(activity: dict, ws: "Path | None" = None) -> Path:
    ws = ws or _workspace()
    d = _journal_dir(ws)
    d.mkdir(parents=True, exist_ok=True)
    path = d / (time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl")
    with open(path, "a") as f:
        f.write(json.dumps(activity, ensure_ascii=False) + "\n")
    return path


def main() -> int:
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    argv_hook = sys.argv[1] if len(sys.argv) > 1 else None
    append(build_activity(data, argv_hook))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — telemetry must NEVER wedge the core
        sys.exit(0)
