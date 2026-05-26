#!/usr/bin/env python3
"""
Structural test for the discord-voice "za warudo" bridge hook added in PR #1080.

The hook in discord-bridge.py is intentionally THIN (CLAUDE.md core/skill
split): the bridge only checks "is this the owner saying the join phrase" and
delegates everything else to the discord-voice skill. This test guards that
contract and several safety properties.

Guards:
  1. Best-effort import block is present (join_trigger imported from skill path)
  2. Fallback stubs return the right zero-values (False / "")
  3. Hook is owner-only: access_tier == "owner" gates the whole block
  4. Dedup-first ordering: seen_message_ids.add appears before the join-trigger
     check (prevents gateway replay from double-firing)
  5. Exception guard on match check: try/except wraps is_join = _dv_...phrase()
  6. Returns without task-file write when triggered (the lone `return` in hook)

Uses source-text structural inspection — no discord.py or event loop required.

Run: python3 tests/discord-bridge-join-trigger-hook.test.py
Exit code: 0 on pass, 1 on fail.
"""

from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"


def fail(msg: str) -> int:
    print(f"FAIL: {msg}", file=sys.stderr)
    return 1


def main() -> int:
    if not BRIDGE.exists():
        return fail(f"{BRIDGE} not found")

    src = BRIDGE.read_text(encoding="utf-8")
    errors = 0

    # ── 1. Best-effort import block ──────────────────────────────────────────
    if "message_is_join_phrase as _dv_message_is_join_phrase" not in src:
        print("FAIL: join_trigger import alias _dv_message_is_join_phrase missing", file=sys.stderr)
        errors += 1
    if "handle_join_trigger as _dv_handle_join_trigger" not in src:
        print("FAIL: join_trigger import alias _dv_handle_join_trigger missing", file=sys.stderr)
        errors += 1
    # The import must be inside a try block (best-effort — skill is optional)
    import_try_m = re.search(
        r"try:\s*\n[\s\S]{0,400}?from join_trigger import",
        src,
    )
    if not import_try_m:
        print("FAIL: join_trigger import is not inside a try block (best-effort guard missing)", file=sys.stderr)
        errors += 1

    # ── 2. Fallback stubs ────────────────────────────────────────────────────
    # _dv_message_is_join_phrase stub must return False
    false_stub = re.search(
        r"def _dv_message_is_join_phrase\(.*?\).*?return\s+False",
        src,
        re.DOTALL,
    )
    if not false_stub:
        print("FAIL: _dv_message_is_join_phrase fallback stub missing or does not return False", file=sys.stderr)
        errors += 1
    # _dv_handle_join_trigger stub must return "" (empty string)
    empty_stub = re.search(
        r'def _dv_handle_join_trigger\(.*?\).*?return\s+""',
        src,
        re.DOTALL,
    )
    if not empty_stub:
        print('FAIL: _dv_handle_join_trigger fallback stub missing or does not return ""', file=sys.stderr)
        errors += 1

    # ── 3. Owner-only gate ───────────────────────────────────────────────────
    # The hook block must be inside `if access_tier == "owner":`.
    owner_gate = re.search(
        r'if access_tier == "owner":\s*\n[\s\S]{0,800}?_dv_message_is_join_phrase',
        src,
    )
    if not owner_gate:
        print('FAIL: join-trigger hook not guarded by `if access_tier == "owner":`', file=sys.stderr)
        errors += 1

    # ── 4. Dedup-first ordering ──────────────────────────────────────────────
    # seen_message_ids.add() must appear before _dv_message_is_join_phrase
    dedup_pos = src.find("seen_message_ids.add(message.id)")
    # Search for the CALL site, not the stub definition — use "is_join = " prefix
    hook_pos = src.find("is_join = _dv_message_is_join_phrase(")
    if dedup_pos == -1:
        print("FAIL: seen_message_ids.add(message.id) not found", file=sys.stderr)
        errors += 1
    elif hook_pos == -1:
        print("FAIL: is_join = _dv_message_is_join_phrase(...) call not found in hook", file=sys.stderr)
        errors += 1
    elif dedup_pos > hook_pos:
        print("FAIL: dedup (seen_message_ids.add) appears AFTER join-trigger check — ordering regression", file=sys.stderr)
        errors += 1

    # ── 5. Exception guard on match check ───────────────────────────────────
    match_guard = re.search(
        r"try:\s*\n\s+is_join\s*=\s*_dv_message_is_join_phrase\(",
        src,
    )
    if not match_guard:
        print("FAIL: is_join = _dv_message_is_join_phrase() not wrapped in try/except", file=sys.stderr)
        errors += 1

    # ── 6. Returns without task-file write when triggered ────────────────────
    # The hook ends with a bare `return` after sending the reply — ensuring no
    # task file is written for a join-phrase message. Find the hook's return.
    hook_block = re.search(
        r"if access_tier == \"owner\":\s*\n([\s\S]{0,1200}?)(?=\n\s{4}#|\n\s{4}if|\Z)",
        src,
    )
    if hook_block:
        block_text = hook_block.group(1)
        if not re.search(r"\breturn\b", block_text):
            print("FAIL: join-trigger hook block missing `return` (would fall through to task-file write)", file=sys.stderr)
            errors += 1
    else:
        # Looser check: find any bare `return` near the join-trigger handler call
        handler_section = src[hook_pos : hook_pos + 600] if hook_pos != -1 else ""
        if "\n            return" not in handler_section and "\n        return" not in handler_section:
            print("FAIL: could not confirm bare `return` in join-trigger block", file=sys.stderr)
            errors += 1

    if errors == 0:
        print("PASS: discord-bridge join-trigger hook — all 6 structural checks OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
