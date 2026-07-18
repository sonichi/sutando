#!/usr/bin/env python3
"""skip-ask-user-question — PreToolUse hook that blocks the interactive
`AskUserQuestion` tool in Sutando's headless core session.

Why: the core agent runs NON-INTERACTIVELY — src/agent/claude/cli/start-cli.sh
launches it with `--dangerously-skip-permissions` inside a tmux pane, driven
over `--remote-control`, with no human watching the terminal. When the model
calls the built-in `AskUserQuestion` tool there, no UI ever renders to answer
it, so the tool BLOCKS THE SESSION INDEFINITELY (observed in the obs collector:
a `tool.call` with tool_name=AskUserQuestion, permission_mode=bypassPermissions,
that never returns).

Denying the call in a PreToolUse hook short-circuits it BEFORE it can render:
Claude Code skips the tool and feeds `permissionDecisionReason` back to the
model, which then continues on its own judgement instead of hanging. Confirmed
against the hooks contract — a PreToolUse `deny` does not render an
AskUserQuestion prompt, it blocks the call outright.

The hook is a NO-OP (exit 0 = allow) for every other tool, so it is safe to
register under any matcher; the `permissionDecision: deny` output is only ever
emitted for AskUserQuestion. Fail-OPEN on any error — a crashing hook must never
wedge the core.

Registration: Sutando's core launcher wires this automatically for every core
session via `--settings` (see src/agent/claude/cli/build-core-settings.mjs,
invoked from start-cli.sh). For manual/other deployments, register it under
PreToolUse with matcher "AskUserQuestion" — see hooks/README.md.
"""
import sys
import json

TOOL = "AskUserQuestion"

REASON = (
    "AskUserQuestion is disabled in this session: Sutando's core agent runs headless "
    "(no interactive user to answer), so the tool would block the session forever. "
    "Do NOT ask — decide autonomously: pick the option you judge best, or state a clear "
    "assumption and proceed. If a choice is genuinely blocking AND irreversible, surface "
    "the question through a normal channel instead (write it to the per-host "
    "pending-questions.md or send an owner notification) and keep working on other things. "
    "[skip-ask-user-question]"
)


def main() -> None:
    data = json.loads(sys.stdin.read())
    if data.get("tool_name") == TOOL:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": REASON,
        }}))
    # Every other tool (and the deny above) exits 0: PreToolUse only blocks when
    # a deny decision is present in the JSON payload.
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail-open: never wedge the core on a hook error
        print(f"[skip-ask-user-question] non-fatal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
