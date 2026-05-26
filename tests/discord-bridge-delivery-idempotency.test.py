#!/usr/bin/env python3
"""Structural tests for discord-bridge delivery-idempotency (#1048)
and send-allowlist shared module (#1029).

## PR #1048 — delivery-idempotency sentinels in poll_results

Before this fix, `poll_results` could send a Discord reply, then crash before
archiving the result file. On restart the result file still existed in
`results/` and would be re-sent — producing a duplicate. Fix:

  1. Before the send block: `_is_delivered(task_id)` checks for a sentinel.
     If present → skip send, archive, clear sentinel, continue.
  2. After `channel.send` succeeds: `_mark_delivered(task_id)` touches the
     sentinel.
  3. After archive completes: `_clear_delivered(task_id)` removes the sentinel
     (bounds `discord-delivered/` directory growth).

## PR #1029 — send_allowlist shared module

Before this fix, the file-attachment delivery policy (`[file:]`, `[send:]`,
`[attach:]` markers) was duplicated between `discord-bridge.py` and
`dm-result.py`. The copies had already silently diverged — `dm-result.py` was
missing the Desktop, Documents, and personal-notes roots that discord-bridge
had. This creates a new `src/send_allowlist.py` as the single source of truth
imported by both bridges.

Run: python3 tests/discord-bridge-delivery-idempotency.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"
ALLOWLIST = REPO / "src" / "send_allowlist.py"

_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("--- context ---", file=sys.stderr)
        print(ctx[:500], file=sys.stderr)


def expect(cond: bool, label: str, ctx: str = "") -> None:
    if not cond:
        fail(label, ctx)


def main() -> None:
    missing = [f for f in [BRIDGE, ALLOWLIST] if not f.exists()]
    for m in missing:
        fail(f"file not found: {m}")
    if missing:
        sys.exit(1)

    bridge = BRIDGE.read_text()
    allowlist = ALLOWLIST.read_text()

    # ── PR #1048: delivery-idempotency sentinel definitions ──────────────────

    # 1. DELIVERED_DIR defined pointing at state/discord-delivered
    expect(
        "DELIVERED_DIR" in bridge and "discord-delivered" in bridge,
        "discord-bridge.py: must define DELIVERED_DIR pointing at state/discord-delivered",
    )

    # 2. Sentinel path helper defined
    expect(
        "_delivered_sentinel_path" in bridge,
        "discord-bridge.py: must define _delivered_sentinel_path(task_id) helper",
    )

    # 3. Sentinel filename is task_id + .sentinel
    sentinel_fn = re.search(r'_delivered_sentinel_path.*?return.*?\.sentinel', bridge, re.DOTALL)
    expect(
        sentinel_fn is not None and ".sentinel" in bridge,
        "discord-bridge.py: sentinel file must use '.sentinel' extension keyed by task_id",
    )

    # 4. _mark_delivered defined
    expect(
        "def _mark_delivered" in bridge,
        "discord-bridge.py: must define _mark_delivered(task_id) to touch sentinel after send",
    )

    # 5. _is_delivered defined
    expect(
        "def _is_delivered" in bridge,
        "discord-bridge.py: must define _is_delivered(task_id) to check sentinel before send",
    )

    # 6. _clear_delivered defined
    expect(
        "def _clear_delivered" in bridge,
        "discord-bridge.py: must define _clear_delivered(task_id) to remove sentinel after archive",
    )

    # 7. _mark_delivered uses mkdir(parents=True, exist_ok=True) to ensure dir exists
    mark_pos = bridge.find("def _mark_delivered")
    next_def = bridge.find("\ndef ", mark_pos + 10)
    mark_region = bridge[mark_pos:next_def] if next_def > 0 else bridge[mark_pos:mark_pos + 400]
    expect(
        "mkdir" in mark_region and "exist_ok=True" in mark_region,
        "discord-bridge.py: _mark_delivered must mkdir(parents=True, exist_ok=True) before touch",
        ctx=mark_region[:400],
    )

    # 8. _mark_delivered has try/except so a sentinel failure never kills the send path
    expect(
        "try:" in mark_region and "except" in mark_region,
        "discord-bridge.py: _mark_delivered must have try/except so sentinel failure doesn't block send",
        ctx=mark_region[:400],
    )

    # 9. _is_delivered returns False on exception (fail-open: treat as not-delivered)
    is_del_pos = bridge.find("def _is_delivered")
    next_def2 = bridge.find("\ndef ", is_del_pos + 10)
    is_del_region = bridge[is_del_pos:next_def2] if next_def2 > 0 else bridge[is_del_pos:is_del_pos + 300]
    expect(
        "return False" in is_del_region and "except" in is_del_region,
        "discord-bridge.py: _is_delivered must return False on exception (fail-open — treat as not-delivered)",
        ctx=is_del_region[:300],
    )

    # ── PR #1048: sentinel call ordering in poll_results ─────────────────────

    # 10. _is_delivered called BEFORE send (skip-if-already-delivered block)
    is_delivered_pos = bridge.find("_is_delivered(task_id)")
    mark_delivered_pos = bridge.find("_mark_delivered(task_id)")
    clear_delivered_pos = bridge.find("_clear_delivered(task_id)")
    expect(
        is_delivered_pos != -1,
        "discord-bridge.py: _is_delivered(task_id) must be called in poll_results",
    )
    expect(
        mark_delivered_pos != -1,
        "discord-bridge.py: _mark_delivered(task_id) must be called after channel.send",
    )
    expect(
        clear_delivered_pos != -1,
        "discord-bridge.py: _clear_delivered(task_id) must be called after archive completes",
    )

    # 11. Call order: _is_delivered → _mark_delivered → _clear_delivered
    expect(
        is_delivered_pos < mark_delivered_pos < clear_delivered_pos,
        "discord-bridge.py: call order must be _is_delivered → _mark_delivered → _clear_delivered",
        ctx=f"positions: is={is_delivered_pos} mark={mark_delivered_pos} clear={clear_delivered_pos}",
    )

    # 12. When sentinel present: skip send, still archive, then clear
    # Anchor: "Skipped (already delivered per sentinel)" or similar log line
    expect(
        "already delivered" in bridge or "Skipped.*sentinel" in bridge or "delivered per sentinel" in bridge,
        "discord-bridge.py: must log 'already delivered' when idempotency sentinel is present",
    )

    # 13. _mark_delivered is called AFTER channel.send (in the poll_results send path)
    # Anchor on the comment that precedes the _mark_delivered call in poll_results.
    # The comment reads "Mark delivered BEFORE the archive runs." or similar.
    mark_anchor = bridge.find("Mark delivered BEFORE")
    if mark_anchor == -1:
        mark_anchor = bridge.find("_mark_delivered(task_id)")
    if mark_anchor != -1:
        region = bridge[max(0, mark_anchor - 300):mark_anchor + 200]
        expect(
            "channel.send" in region or "send_chunk" in region or "send(" in region,
            "discord-bridge.py: _mark_delivered must appear after channel.send in poll_results",
            ctx=region[:400],
        )

    # ── PR #1029: send_allowlist.py module structure ──────────────────────────

    # 14. SEND_ALLOWED_ROOTS defined
    expect(
        "SEND_ALLOWED_ROOTS" in allowlist,
        "send_allowlist.py: must define SEND_ALLOWED_ROOTS tuple",
    )

    # 15. results/ and notes/ roots always allowed (core agent output)
    expect(
        '"results"' in allowlist and '"notes"' in allowlist,
        "send_allowlist.py: results/ and notes/ must be in SEND_ALLOWED_ROOTS",
    )

    # 16. SEND_ALLOWED_PREFIXES defined for /tmp paths
    expect(
        "SEND_ALLOWED_PREFIXES" in allowlist,
        "send_allowlist.py: must define SEND_ALLOWED_PREFIXES for /tmp/sutando- and /tmp/echo- prefixes",
    )

    # 17. /tmp/sutando- prefix included (agent-generated temp files)
    expect(
        '"/tmp/sutando-"' in allowlist or "'/tmp/sutando-'" in allowlist,
        "send_allowlist.py: must include '/tmp/sutando-' prefix for agent-generated temp files",
    )

    # 18. is_path_sendable function defined (the policy function)
    expect(
        "def is_path_sendable" in allowlist,
        "send_allowlist.py: must define is_path_sendable(fpath) as the single policy function",
    )

    # 19. Uses os.path.realpath to defeat symlink/traversal attacks
    expect(
        "os.path.realpath" in allowlist or "realpath" in allowlist,
        "send_allowlist.py: must use realpath() to defeat symlink and path-traversal attacks",
    )

    # 20. Checks os.path.isfile (not just existence) so non-files are rejected
    expect(
        "os.path.isfile" in allowlist,
        "send_allowlist.py: must use os.path.isfile() to reject directories and non-existent paths",
    )

    # 21. Trailing-slash suffix check (prevents prefix-pollution: /tmp/sutando-x ≠ /tmp/sutando)
    expect(
        "os.sep" in allowlist or '+ "/"' in allowlist or "+ os.sep" in allowlist,
        "send_allowlist.py: must check root + separator to prevent prefix-pollution attacks",
    )

    # 22. discord-bridge.py imports from send_allowlist
    expect(
        "from send_allowlist import" in bridge or "import send_allowlist" in bridge,
        "discord-bridge.py: must import is_path_sendable (or send_allowlist) from send_allowlist.py",
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        total = 22
        print(f"All {total} tests passed.")


if __name__ == "__main__":
    main()
