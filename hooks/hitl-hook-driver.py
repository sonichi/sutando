#!/usr/bin/env python3
"""hitl-hook-driver — permission hook that routes a tool-permission decision
through the HumanRequirement Manager instead of the terminal.

Layer 1 of the no-TUI stack: structured, no screen parsing. Claude Code feeds
this hook JSON on stdin; the hook blocks until a human answers the card (or a
policy answers for them), then prints the decision in the shape the firing
event expects. Two events are served, told apart by `hook_event_name`:

  PermissionRequest (preferred) — fires only when Claude Code would render a
    permission dialog, so every requirement it creates is a real block. Output:
    `{"hookSpecificOutput": {"hookEventName": "PermissionRequest",
      "decision": {"behavior": "allow"} | {"behavior": "deny", "message": ...}}}`
  PreToolUse (legacy registration) — fires before EVERY tool call, dialog or
    not. Output: `{"hookSpecificOutput": {"hookEventName": "PreToolUse",
      "permissionDecision": "allow" | "deny", "permissionDecisionReason": ...}}`

A payload with no `hook_event_name` is treated as PreToolUse (the shape every
existing registration produces). The card itself is posted by the supervisor's
projector from the same requirement record — this hook never talks to Matrix.

Invariants (same as hooks/human-action-bridge.py, which owns AskUserQuestion):
  - a timeout NEVER approves: no decision => deny with reason
  - fail-OPEN for the session: any hook error => exit 0 with no decision,
    so Claude Code falls back to its own permission flow
  - policy first: an allowlisted tool never creates a requirement

Registration (settings.json) — PermissionRequest, so the hook runs only for
real dialogs; keep a PreToolUse entry only on installs that predate the event:
  "PermissionRequest": [{"matcher": "*", "hooks": [{"type": "command",
      "command": "python3 <repo>/hooks/hitl-hook-driver.py", "timeout": 900}]}]
The hook's own wait (SUTANDO_HITL_TIMEOUT, default 600s) must stay below the
registered hook timeout, or Claude Code kills it before it can deny.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

# Locates the CODE tree (this hook is registered by absolute path), never the workspace.
REPO = Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(REPO / "src"))

from hitl.manager import (  # noqa: E402
    POLICY_DECIDER,
    HitlManager,
    HitlStore,
    default_store,
)
from hitl.policy import policy_from_env  # noqa: E402
from hitl.schema import Action, HumanRequirement  # noqa: E402

# AskUserQuestion has its own bridge. The allowlist lives in hitl.policy: the
# Manager answers it, so the record exists even when no human is needed.
OWNED_ELSEWHERE = {"AskUserQuestion"}
TIMEOUT_REASON = (
    "No decision arrived from the owner within the wait window (requirement {rid}). "
    "Denied by default; the owner can re-run the tool once they answer."
)


def _workspace() -> Path:
    override = os.environ.get("SUTANDO_HITL_WORKSPACE")
    if override:
        return Path(override)
    from workspace_default import resolve_workspace  # noqa: E402

    return Path(resolve_workspace())


SUMMARY_CAP = 200


def _clip(text: str) -> str:
    # A clipped command must SAY it was clipped: the owner allows what they read.
    if len(text) <= SUMMARY_CAP:
        return text
    return f"{text[:SUMMARY_CAP]}… (truncated; {len(text)} chars total)"


def _summary(tool: str, tool_input: dict) -> str:
    if tool == "Bash":
        return _clip(str(tool_input.get("command") or ""))
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if tool == "ExitPlanMode":
        return "exit plan mode and start executing"
    return _clip(json.dumps(tool_input, sort_keys=True))


def _guard(tool: str, tool_input: dict) -> str:
    basis = tool + "\n" + json.dumps(tool_input, sort_keys=True)
    return "hook:" + hashlib.sha256(basis.encode()).hexdigest()[:16]


def _requirement(data: dict) -> HumanRequirement:
    tool = str(data.get("tool_name") or "")
    tool_input = data.get("tool_input") or {}
    session = os.environ.get("SUTANDO_TMUX_SESSION") or os.environ.get("SUTANDO_CORE_ID") or "sutando-core"
    kind = "confirmation" if tool == "ExitPlanMode" else "permission"
    verb = "wants to" if tool == "ExitPlanMode" else "wants to run"
    return HumanRequirement(
        kind=kind,
        runtime="claude",
        message=f"Claude {verb} {tool}: {_summary(tool, tool_input)}",
        title=f"claude · {session}",
        guard=_guard(tool, tool_input),
        subject={"tool": tool, "input": _summary(tool, tool_input)},
        device={"id": session, "name": session},
        actions=[
            Action(id="allow", kind="allow_once", label="Allow"),
            Action(id="deny", kind="reject_once", label="Deny"),
            Action(id="open_terminal", kind="open_terminal", label=f"Open terminal ({session})"),
        ],
    )


PERMISSION_REQUEST = "PermissionRequest"
PRE_TOOL_USE = "PreToolUse"


def _event(data: dict) -> str:
    # Absent = PreToolUse: every registration that predates PermissionRequest sends no name.
    return PERMISSION_REQUEST if data.get("hook_event_name") == PERMISSION_REQUEST else PRE_TOOL_USE


def _emit(event: str, behavior: str, reason: str) -> None:
    if event == PERMISSION_REQUEST:
        decision = {"behavior": "allow"} if behavior == "allow" else {"behavior": "deny", "message": reason}
        out = {"hookEventName": PERMISSION_REQUEST, "decision": decision}
    else:
        out = {"hookEventName": PRE_TOOL_USE, "permissionDecision": behavior, "permissionDecisionReason": reason}
    print(json.dumps({"hookSpecificOutput": out}))


def _wait(manager: HitlManager, rid: str, timeout_s: float, poll_s: float):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        req = manager.get(rid)
        if req is None:
            return None
        if req.chosen_action:
            return req.chosen_action
        if req.terminal:
            return None
        time.sleep(poll_s)
    manager.expire(rid)
    return None


def main() -> None:
    data = json.loads(sys.stdin.read() or "{}")
    tool = str(data.get("tool_name") or "")
    if not tool or tool in OWNED_ELSEWHERE:
        sys.exit(0)  # not ours — no decision, Claude's own flow continues
    event = _event(data)
    manager = HitlManager(HitlStore(default_store(_workspace())), policy=policy_from_env())
    req = manager.create(_requirement(data))
    if req.decided_by == POLICY_DECIDER and req.chosen_action == "allow":
        manager.resolve(req.id)
        _emit(event, "allow", f"hitl policy: allowlisted tool ({req.id})")
        sys.exit(0)
    timeout_s = float(os.environ.get("SUTANDO_HITL_TIMEOUT", "600"))
    poll_s = float(os.environ.get("SUTANDO_HITL_POLL", "1"))
    chosen = _wait(manager, req.id, timeout_s, poll_s)
    if chosen == "allow":
        manager.resolve(req.id)
        _emit(event, "allow", f"owner allowed via card {req.id}")
    elif chosen == "deny":
        manager.resolve(req.id)
        _emit(event, "deny", f"owner denied via card {req.id}")
    else:
        _emit(event, "deny", TIMEOUT_REASON.format(rid=req.id))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail-open: never wedge the core on a hook error
        print(f"[hitl-hook-driver] non-fatal error, no decision: {e}", file=sys.stderr)
        sys.exit(0)
