#!/usr/bin/env python3
"""PreToolUse skip-ask-user-question: the headless core must never block on the
interactive AskUserQuestion tool (hooks/skip-ask-user-question.py).

Drives the real hook via stdin (the way Claude Code invokes it) and asserts:
  * AskUserQuestion  -> emits a PreToolUse `deny` decision (tool short-circuited).
  * any other tool   -> no output, exit 0 (allowed).
  * malformed stdin  -> fail-OPEN (exit 0, no deny) so a crash never wedges core.
Run:  python3 tests/skip-ask-user-question.test.py
"""
import json
import subprocess
import sys
from pathlib import Path

HOOK = str(Path(__file__).resolve().parent.parent / "hooks" / "skip-ask-user-question.py")


def run(payload):
    # payload may be a dict (JSON-encoded) or a raw string (malformed-input case).
    stdin = json.dumps(payload) if not isinstance(payload, str) else payload
    return subprocess.run([sys.executable, HOOK], input=stdin,
                          capture_output=True, text=True, timeout=20)


def decision(out):
    try:
        return json.loads(out).get("hookSpecificOutput", {}).get("permissionDecision")
    except Exception:
        return None


# 1) AskUserQuestion is denied — the whole point of the hook.
r = run({"tool_name": "AskUserQuestion", "tool_input": {"questions": [{"question": "?"}]}})
assert r.returncode == 0, f"AskUserQuestion: expected exit 0, got {r.returncode} / {r.stderr}"
out = json.loads(r.stdout)
hso = out["hookSpecificOutput"]
assert hso["hookEventName"] == "PreToolUse", f"wrong hookEventName: {hso}"
assert hso["permissionDecision"] == "deny", f"expected deny, got {hso}"
assert hso.get("permissionDecisionReason"), "deny must carry a reason so the model can proceed"

# 2) Every other tool is allowed (no output, exit 0). A stray deny here would
#    break normal operation for unrelated tools.
for tool in ("Bash", "Read", "Edit", "Task", "WebFetch", "AskUserQuestionX", "askuserquestion"):
    r = run({"tool_name": tool, "tool_input": {}})
    assert r.returncode == 0, f"{tool}: expected exit 0, got {r.returncode}"
    assert r.stdout.strip() == "", f"{tool}: expected no output, got {r.stdout!r}"
    assert decision(r.stdout) is None, f"{tool}: must not emit a permission decision"

# 3) Missing tool_name key -> treated as any-other-tool (allow), not a crash.
r = run({"tool_input": {}})
assert r.returncode == 0 and r.stdout.strip() == "", f"missing tool_name should allow: {r.stdout!r}"

# 4) Malformed stdin -> fail-OPEN: exit 0, no deny (a crashing hook must never
#    wedge the core by blocking every tool call).
r = run("this is not json")
assert r.returncode == 0, f"malformed stdin must fail-open (exit 0), got {r.returncode}"
assert decision(r.stdout) is None, "malformed stdin must not produce a deny"

print("PASS: skip-ask-user-question — denies AskUserQuestion, allows all else, fails open")
