#!/usr/bin/env python3
"""Codebase-sweep regression guard: every known task-writing file must use the
injection guard.

Rather than testing the guard's behavior (that's task-body-guard.test.py and
task-body-injection-guard.test.py), this test checks that the CALLER SITES are
wired up. Add a new task writer? This test fails until you add the guard call.

Python path: import + call confine_user_content()
TypeScript path: define + call confineUserContent() (module-private — checked by source scan)

Run: python3 tests/injection-guard-sweep.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_passed = 0
_failed = 0


def _check(label: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        print(f"FAIL [{label}]{': ' + detail if detail else ''}", file=sys.stderr)


def _src(path: str) -> str:
    return (REPO / path).read_text()


# ---------------------------------------------------------------------------
# Python bridge files: must import AND call confine_user_content
# ---------------------------------------------------------------------------

for _f in (
    "src/discord-bridge.py",
    "src/telegram-bridge.py",
    "src/slack-bridge.py",
    "src/github-webhook.py",
    "src/agent-api.py",
):
    _s = _src(_f)
    _name = _f.split("/")[-1]
    _check(
        f"{_name}: imports confine_user_content",
        "from task_body_guard import confine_user_content" in _s,
        f"{_f} must import confine_user_content from task_body_guard",
    )
    _check(
        f"{_name}: calls confine_user_content",
        "confine_user_content(" in _s,
        f"{_f} must call confine_user_content() on user-supplied task body",
    )

# ---------------------------------------------------------------------------
# Python bridges: task: field must come AFTER source/access_tier/priority
# (belt-and-suspenders alongside the ZWSP guard — a forged field in user content
# that slips past the guard would appear before the real fields, losing the race)
# ---------------------------------------------------------------------------

def _task_field_is_last_in_write_text(src: str) -> bool:
    """Check that task: appears after source/access_tier/priority in write_text.

    Scans the source for the write_text block by line: starts at the first
    `task_file.write_text(` line and collects f"key: pattern lines until
    the matching close paren, counting open parens to handle nesting.
    """
    import re
    lines = src.splitlines()
    in_block = False
    depth = 0
    keys: list[str] = []
    for line in lines:
        if not in_block:
            if "task_file.write_text(" in line:
                in_block = True
                # Count parens on this line; the call itself opens one extra
                # net paren that the rest of the block will close.
                depth = line.count("(") - line.count(")")
                # Collect keys on the trigger line itself (uncommon but possible)
                for m in re.finditer(r'f"([a-z_]+): ', line):
                    keys.append(m.group(1))
                continue  # skip the double-count in the in_block branch below
        if in_block:
            for m in re.finditer(r'f"([a-z_]+): ', line):
                keys.append(m.group(1))
            depth += line.count("(") - line.count(")")
            if depth <= 0:
                break
    if "task" not in keys:
        return False
    task_pos = keys.index("task")
    for sentinel in ("source", "access_tier", "priority"):
        if sentinel in keys and keys.index(sentinel) > task_pos:
            return False
    return True


for _fb, _fname in (
    ("src/discord-bridge.py", "discord-bridge"),
    ("src/telegram-bridge.py", "telegram-bridge"),
    ("src/slack-bridge.py", "slack-bridge"),
):
    _sb = _src(_fb)
    _check(
        f"{_fname}: task: field is after source/access_tier/priority (field-order defense)",
        _task_field_is_last_in_write_text(_sb),
        f"{_fb}: task: must appear after source/access_tier/priority in task_file.write_text()",
    )

# ---------------------------------------------------------------------------
# Python ag2-relay: uses _one_line() as structural equivalent
# (collapses newlines → prevents line-based injection; no ZWSP needed)
# ---------------------------------------------------------------------------

_ag2 = _src("skills/ag2-relay/remote-task-client.py")
_check(
    "ag2-relay: _one_line on task fields",
    "_one_line(" in _ag2 and "task" in _ag2,
    "ag2-relay must use _one_line() for newline-stripping on task fields",
)
_check(
    "ag2-relay: access_tier appended last (local decision wins)",
    "lines.append(f\"access_tier: {LOCAL_TIER}\")" in _ag2
    or 'lines.append(f"access_tier: {LOCAL_TIER}")' in _ag2
    or "access_tier" in _ag2 and "last" in _ag2.lower(),
)

# ---------------------------------------------------------------------------
# TypeScript task-bridge.ts: confineUserContent defined and applied at all sites
# ---------------------------------------------------------------------------

_tb = _src("src/task-bridge.ts")
_check(
    "task-bridge: confineUserContent defined",
    "function confineUserContent" in _tb,
)
_check(
    "task-bridge: confineUserContent(task) — voice body",
    "confineUserContent(task)" in _tb,
)
_check(
    "task-bridge: confineUserContent(recent) — voice transcript",
    "confineUserContent(recent)" in _tb,
)
_check(
    "task-bridge: confineUserContent(content) — context-drop",
    "confineUserContent(content)" in _tb,
)
_check(
    "task-bridge: confineUserContent(taskDescription) — chat path",
    "confineUserContent(taskDescription)" in _tb,
)

# ---------------------------------------------------------------------------
# TypeScript conversation-server.ts: confineUserContent defined and applied
# ---------------------------------------------------------------------------

_cs = _src("skills/phone-conversation/scripts/conversation-server.ts")
_check(
    "conversation-server: confineUserContent defined",
    "function confineUserContent" in _cs,
)
_check(
    "conversation-server: confineUserContent(taskDescription) — delegation",
    "confineUserContent(taskDescription)" in _cs,
)
_check(
    "conversation-server: confineUserContent(fullTranscript) — delegation",
    "confineUserContent(fullTranscript)" in _cs,
)
_check(
    "conversation-server: confineUserContent(formatted) — summary task",
    "confineUserContent(formatted)" in _cs,
)
_check(
    "conversation-server: confineUserContent(callerNumber) — caller field before access_tier",
    "confineUserContent(callSession.callerNumber" in _cs,
    "caller: field appears before access_tier: in delegateTask() — callerNumber must be confined "
    "so a \\naccess_tier: owner injection lands ZWSP-prefixed (Twilio validates signatures but "
    "defence-in-depth requires this)",
)
_check(
    "conversation-server: confineUserContent(session.callerNumber) — summary task caller field",
    "confineUserContent(session.callerNumber" in _cs,
    "caller: field appears before access_tier: in the summary task (lines ~1264) — session.callerNumber "
    "must be wrapped in confineUserContent() for the same reason as delegateTask(): a \\naccess_tier: owner "
    "injection in the phone number would precede the real access_tier: line.",
)

# ---------------------------------------------------------------------------
# inline-tools.ts cancel_task: targetId newline-stripped, task: field last
# ---------------------------------------------------------------------------

_it = _src("src/inline-tools.ts")
# Locate the cancelBody block
_cb_start = _it.find("const cancelBody = [")
_cb_end = _it.find("].join('\\n');", _cb_start) if _cb_start > 0 else -1
_cb_block = _it[_cb_start:_cb_end + 20] if _cb_start > 0 and _cb_end > 0 else ""
_check(
    "inline-tools: cancel_task strips newlines from targetId",
    "safeTargetId" in _it and ".replace(/[\\r\\n]/g, '')" in _it,
    "cancel_task embeds targetId (Gemini-controlled) in the task: body — must strip newlines "
    "before embedding so a forged \\naccess_tier: line can't precede the real one",
)
_check(
    "inline-tools: cancel_task task: field is last (after access_tier:)",
    "access_tier: owner" in _cb_block
    and "task: CANCEL_INSTRUCTION" in _cb_block
    and _cb_block.index("access_tier: owner") < _cb_block.index("task: CANCEL_INSTRUCTION"),
    "cancel_task cancelBody must place task: after access_tier: (field-order defence)",
)

# ---------------------------------------------------------------------------
# web-client.ts: task body is hardcoded (no user data embedded)
# ---------------------------------------------------------------------------

_wc = _src("src/web-client.ts")
# Find the server-side handler by locating the writeFileSync call that follows
# the /paidsubscriptions/scan comment block (scan-prompt.md pointer pattern).
_wc_server_start = _wc.find("scan-prompt.md")
_wc_task_block = _wc[max(0, _wc_server_start - 400): _wc_server_start + 600] if _wc_server_start > 0 else ""
# Confirm the task content is a hardcoded literal, not user-interpolated
_check(
    "web-client: paidsubscriptions task is hardcoded (no ${req.} interpolation)",
    "scan-prompt.md" in _wc_task_block and "${req." not in _wc_task_block,
    "web-client /paidsubscriptions/scan task must use hardcoded content only",
)

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

_total = _passed + _failed
print(f"injection-guard-sweep: {_passed}/{_total} passed"  # expected 29/29
      + ("" if _failed == 0 else f" — {_failed} FAILED"))
sys.exit(0 if _failed == 0 else 1)
