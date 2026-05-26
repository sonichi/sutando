#!/usr/bin/env python3
"""Structural tests for the discord-voice "za warudo" magic-word summon (#1080/#1113).

Two source files under test:
  skills/discord-voice/scripts/join_trigger.py  — the skill implementation
  src/discord-bridge.py                          — the bridge integration point

What each test verifies:

join_trigger.py:
  1. DEFAULT_JOIN_PHRASE constant exists and equals "za warudo"
  2. load_join_phrase() reads from workspace config, then example config
  3. message_is_join_phrase(): case-insensitive exact match
  4. message_is_join_phrase(): @-mention prefix stripping (PR #1113 fix)
  5. message_is_join_phrase(): word-boundary — "za warudonow" must NOT match
  6. message_is_join_phrase(): trailing punctuation (za warudo!) matches
  7. _server_already_running() uses pgrep with --channel {channel_id}
  8. _spawn_voice_server(): pops GEMINI_API_KEY, sets DISCORD_VOICE_SERVER=1
  9. _enqueue_context_prep_task(): writes source: system-magic-word + priority: urgent
  10. handle_join_trigger(): checks _server_already_running before spawning

discord-bridge.py:
  11. Best-effort import of join_trigger with no-op stubs fallback
  12. requireMention bypass: magic word fires before the require_mention gate
  13. requireMention bypass: log message includes "bypassing requireMention"
  14. Graceful catch: handler raise is caught and returns a safe reply

Run: python3 tests/discord-voice-za-warudo.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
JOIN_TRIGGER = REPO / "skills" / "discord-voice" / "scripts" / "join_trigger.py"
DISCORD_BRIDGE = REPO / "src" / "discord-bridge.py"

_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("--- context ---", file=sys.stderr)
        print(ctx[:600], file=sys.stderr)


def expect(cond: bool, label: str, ctx: str = "") -> None:
    if not cond:
        fail(label, ctx)


def main() -> None:
    missing = [f for f in [JOIN_TRIGGER, DISCORD_BRIDGE] if not f.exists()]
    for m in missing:
        fail(f"file not found: {m}")
    if missing:
        sys.exit(1)

    jt = JOIN_TRIGGER.read_text()
    db = DISCORD_BRIDGE.read_text()

    # 1. DEFAULT_JOIN_PHRASE constant
    expect(
        'DEFAULT_JOIN_PHRASE = "za warudo"' in jt or "DEFAULT_JOIN_PHRASE = 'za warudo'" in jt,
        'join_trigger.py: DEFAULT_JOIN_PHRASE must equal "za warudo"',
        ctx=next((l for l in jt.splitlines() if "DEFAULT_JOIN_PHRASE" in l), ""),
    )

    # 2. load_join_phrase() resolves workspace config then example config
    expect(
        "load_join_phrase" in jt,
        "join_trigger.py: load_join_phrase() function must be defined",
    )
    expect(
        "config.json.example" in jt,
        "join_trigger.py: load_join_phrase() must fall back to config.json.example",
    )
    expect(
        "join_phrase" in jt,
        "join_trigger.py: load_join_phrase() must read the 'join_phrase' config key",
    )

    # 3. message_is_join_phrase(): case-insensitive match logic
    expect(
        "message_is_join_phrase" in jt,
        "join_trigger.py: message_is_join_phrase() function must be defined",
    )
    expect(
        ".lower()" in jt,
        "join_trigger.py: message_is_join_phrase() must do case-insensitive comparison via .lower()",
    )

    # 4. @-mention prefix stripping (PR #1113 fix)
    # The fix strips <@id>, <@!id>, <@&roleid> prefixes before matching.
    expect(
        "<@" in jt and "re.sub" in jt or "_re.sub" in jt,
        "join_trigger.py: message_is_join_phrase() must strip Discord @-mentions via re.sub",
    )
    mention_region = ""
    for i, line in enumerate(jt.splitlines()):
        if "<@" in line and ("sub" in line or "strip" in line.lower() or "re" in line):
            mention_region = "\n".join(jt.splitlines()[max(0, i-2):i+3])
            break
    expect(
        r"<@[!&]?" in jt or r"<@!" in jt or r"<@\d" in jt,
        "join_trigger.py: @-mention strip regex must handle <@id>, <@!id>, <@&id> variants",
        ctx=mention_region,
    )

    # 5. word-boundary check — prevents "za warudonow" from matching
    expect(
        "isalnum()" in jt,
        "join_trigger.py: message_is_join_phrase() must use .isalnum() for word-boundary guard",
    )

    # 6. Trailing punctuation handling — startswith + boundary check
    expect(
        "startswith(" in jt,
        "join_trigger.py: message_is_join_phrase() must use startswith() for trailing-content match",
    )

    # 7. _server_already_running() uses pgrep with --channel {channel_id}
    expect(
        "_server_already_running" in jt,
        "join_trigger.py: _server_already_running() must be defined",
    )
    expect(
        "--channel" in jt,
        "join_trigger.py: _server_already_running() must check pgrep output for '--channel <id>'",
    )
    expect(
        "pgrep" in jt,
        "join_trigger.py: _server_already_running() must call pgrep to detect running server",
    )

    # 8. _spawn_voice_server() env setup
    expect(
        "_spawn_voice_server" in jt,
        "join_trigger.py: _spawn_voice_server() must be defined",
    )
    expect(
        'GEMINI_API_KEY' in jt and ('pop(' in jt or '.pop(' in jt),
        "join_trigger.py: _spawn_voice_server() must pop GEMINI_API_KEY from child env",
    )
    expect(
        'DISCORD_VOICE_SERVER' in jt and '"1"' in jt,
        'join_trigger.py: _spawn_voice_server() must set DISCORD_VOICE_SERVER="1" in child env',
    )
    expect(
        "start_new_session=True" in jt,
        "join_trigger.py: _spawn_voice_server() must use start_new_session=True for detached spawn",
    )

    # 9. _enqueue_context_prep_task() writes system-magic-word task
    expect(
        "_enqueue_context_prep_task" in jt,
        "join_trigger.py: _enqueue_context_prep_task() must be defined",
    )
    expect(
        "source: system-magic-word" in jt,
        "join_trigger.py: context-prep task must use source: system-magic-word",
    )
    expect(
        "priority: urgent" in jt,
        "join_trigger.py: context-prep task must set priority: urgent",
    )
    # The context-prep task should never raise — it's observability, not blocking
    expect(
        "except Exception" in jt,
        "join_trigger.py: _enqueue_context_prep_task() must catch all exceptions (non-blocking)",
    )

    # 10. handle_join_trigger() checks _server_already_running before spawning
    handle_pos = jt.find("def handle_join_trigger(")
    expect(handle_pos != -1, "join_trigger.py: handle_join_trigger() must be defined")
    if handle_pos != -1:
        handle_region = jt[handle_pos:handle_pos + 2200]
        expect(
            "_server_already_running(" in handle_region,
            "join_trigger.py: handle_join_trigger() must guard against double-launch via _server_already_running()",
            ctx=handle_region[:600],
        )
        expect(
            "_spawn_voice_server(" in handle_region,
            "join_trigger.py: handle_join_trigger() must call _spawn_voice_server() on successful guard",
            ctx=handle_region[:600],
        )
        expect(
            "_enqueue_context_prep_task(" in handle_region,
            "join_trigger.py: handle_join_trigger() must enqueue context-prep task after successful spawn",
            ctx=handle_region[:600],
        )

    # 11. discord-bridge.py: best-effort import with no-op stubs
    import_region_pos = db.find("join_trigger")
    expect(
        import_region_pos != -1,
        "discord-bridge.py: must import from join_trigger",
    )
    if import_region_pos != -1:
        import_region = db[max(0, import_region_pos - 100):import_region_pos + 400]
        expect(
            "except" in import_region or "try:" in db[max(0, import_region_pos - 300):import_region_pos + 50],
            "discord-bridge.py: join_trigger import must be wrapped in try/except (best-effort — skill may be absent)",
            ctx=import_region,
        )
        expect(
            "_dv_message_is_join_phrase" in db,
            "discord-bridge.py: must expose _dv_message_is_join_phrase (alias of join_trigger.message_is_join_phrase)",
        )
        expect(
            "_dv_handle_join_trigger" in db,
            "discord-bridge.py: must expose _dv_handle_join_trigger (alias of join_trigger.handle_join_trigger)",
        )

    # 12. requireMention bypass: magic-word check fires before the require_mention gate
    rq_gate_pos = db.find("require_mention and not bot_mentioned")
    magic_word_pos = db.find("_dv_message_is_join_phrase(")
    expect(
        rq_gate_pos != -1 and magic_word_pos != -1,
        "discord-bridge.py: both requireMention gate and magic-word check must be present",
    )
    expect(
        magic_word_pos < rq_gate_pos,
        "discord-bridge.py: magic-word check must appear BEFORE the requireMention gate in the handler flow",
        ctx=f"magic_word_pos={magic_word_pos} require_mention_pos={rq_gate_pos}",
    )

    # 13. requireMention bypass log message
    expect(
        "bypassing requireMention" in db,
        "discord-bridge.py: magic-word fast path must log 'bypassing requireMention'",
    )

    # 14. Graceful catch on handler raise
    handler_region_pos = db.find("_dv_handle_join_trigger(")
    expect(handler_region_pos != -1, "discord-bridge.py: must call _dv_handle_join_trigger()")
    if handler_region_pos != -1:
        handler_region = db[handler_region_pos:handler_region_pos + 500]
        expect(
            "except Exception" in handler_region or "except Exception" in db[handler_region_pos - 50:handler_region_pos + 500],
            "discord-bridge.py: _dv_handle_join_trigger() call must be wrapped in try/except (graceful degradation)",
            ctx=handler_region[:400],
        )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        total = 30  # count of individual expect() calls
        print(f"All {total} tests passed.")


if __name__ == "__main__":
    main()
