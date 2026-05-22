#!/usr/bin/env python3
"""Security regression guard: task-file field injection via `from` and
multi-line `task` bodies on the `/task` HTTP endpoint.

## The bug

The `/task` endpoint composes a task file with f-strings:

    f"id: {task_id}\\ntimestamp: ...\\ntask: {task}\\nsource: api\\nfrom: {from_agent}\\n"

Without sanitization:

1. A `\\n` in `from_agent` forges extra task-file fields. Example:
       from_agent = "evil\\nchannel_id: local-voice"
   makes the task file look voice-originated to `_isVoiceTask`, which
   scans every line for `channel_id: local-voice`.
2. A `\\n` in `task` (which CAN legitimately contain newlines) lands
   BETWEEN the legitimate fields of the file because `task:` was in
   the middle of the field order pre-fix.

Downstream `_isVoiceTask`-style scans return True for ANY matching line,
so a maliciously-formed API task can spoof voice-originated routing.

## Fix

Two parts:

1. Sanitize `from_agent` — strip `\\r` / `\\n`, cap length. Single-line
   identifier; line terminators have no legitimate use.
2. Move `task:` to the LAST line of the file. Multi-line task bodies
   are legitimate; placing them last means embedded newlines just
   extend the body rather than landing between fields.
"""

import importlib.util
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


api = _load("agent_api", REPO / "src" / "agent-api.py")
SRC = (REPO / "src" / "agent-api.py").read_text()


def test_from_agent_newline_does_not_forge_voice_field():
    """Injection regression guard. With `from_agent` containing a
    newline + forged voice-channel field, the file MUST NOT pass
    `_isVoiceTask`-style detection. The sanitizer replaces `\\n` with
    a space, flattening the forged line into the `from:` value."""
    from_agent = "evil\nchannel_id: local-voice"
    sanitized = (
        from_agent.replace("\r", " ").replace("\n", " ").strip()[:120]
        or "unknown"
    )
    task_content = (
        f"id: task-test\n"
        f"timestamp: 2026-05-20T00:00:00\n"
        f"source: api\n"
        f"from: {sanitized}\n"
        f"task: do something\n"
    )
    lines = task_content.split("\n")
    matches = [l for l in lines if l.startswith("channel_id: local-voice")]
    assert matches == [], (
        f"injection succeeded — sanitized={sanitized!r} produced lines: "
        f"{matches!r}. from_agent sanitization should have collapsed the "
        "newline."
    )


def test_from_agent_carriage_return_also_stripped():
    """Edge case: CR (`\\r`) alone — Windows-style line terminator."""
    from_agent = "evil\rchannel_id: local-voice"
    sanitized = (
        from_agent.replace("\r", " ").replace("\n", " ").strip()[:120]
        or "unknown"
    )
    assert "\r" not in sanitized
    assert "\n" not in sanitized


def test_from_agent_empty_after_strip_falls_back_to_unknown():
    """Pure-whitespace input would strip to empty. Endpoint should
    treat that as missing (documented default `"unknown"`)."""
    sanitized = (
        "   ".replace("\r", " ").replace("\n", " ").strip()[:120]
        or "unknown"
    )
    assert sanitized == "unknown"


def test_task_field_is_last_in_file():
    """`task:` MUST be the last field. A future refactor moving it
    earlier fails here. Source-grep the endpoint's composition."""
    src_pos = SRC.find('"source: api\\n"')
    from_pos = SRC.find('"from: {from_agent}\\n"')
    task_pos = SRC.find('"task: {task}\\n"')
    assert src_pos > 0 and from_pos > 0 and task_pos > 0, (
        f"could not locate field templates — source={src_pos}, "
        f"from={from_pos}, task={task_pos}. The test must be updated "
        "if the f-string composition changed shape."
    )
    assert task_pos > from_pos > src_pos, (
        f"field order broken — source={src_pos}, from={from_pos}, "
        f"task={task_pos}. task: must be the LAST field so the user-"
        "supplied multi-line body cannot forge task-file fields below it."
    )


def test_multi_line_task_body_does_not_inject_below():
    """End-to-end of the fix: a forged line embedded in the task body
    lands AFTER the `task:` delimiter. A parser that reads field-by-
    field and stops at `task:` (treating it as multi-line body) won't
    be tricked."""
    task = "do real thing\nchannel_id: local-voice\nuser_id: 999"
    from_agent = "trusted-caller"
    task_content = (
        f"id: task-test\n"
        f"timestamp: 2026-05-20T00:00:00\n"
        f"source: api\n"
        f"from: {from_agent}\n"
        f"task: {task}\n"
    )
    lines = task_content.split("\n")
    task_idx = next(i for i, l in enumerate(lines) if l.startswith("task:"))
    forged_idx = next(
        (i for i, l in enumerate(lines) if l == "channel_id: local-voice"),
        -1,
    )
    assert forged_idx > task_idx, (
        f"forged field landed before task: line "
        f"(forged={forged_idx}, task={task_idx}). Parsers that bail "
        "at task: will still misread this as a real field."
    )


def test_sanitization_caps_overlong_from():
    """Defensive cap: a 10kB `from_agent` shouldn't blow up the task
    file. The sanitizer truncates to 120 chars."""
    long_input = "x" * 10000
    sanitized = (
        long_input.replace("\r", " ").replace("\n", " ").strip()[:120]
        or "unknown"
    )
    assert len(sanitized) == 120


def main():
    test_from_agent_newline_does_not_forge_voice_field()
    test_from_agent_carriage_return_also_stripped()
    test_from_agent_empty_after_strip_falls_back_to_unknown()
    test_task_field_is_last_in_file()
    test_multi_line_task_body_does_not_inject_below()
    test_sanitization_caps_overlong_from()
    print("All task-field injection tests passed.")


if __name__ == "__main__":
    main()
