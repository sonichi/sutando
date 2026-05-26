#!/usr/bin/env python3
"""Structural tests for outbox_log recipient_label (#1078)
and catchup-after-startup submodule/worktree detection (#1076).

## PR #1078 — outbox_log recipient_label (follow-up to #1077)

Before this fix, `outbox_log.append()` was called with `recipient_label=None`
in all three delivery paths in `poll_results`. PR #1078 populates it with
human-readable labels:
  - DM to a specific user: `"<username> DM"` (or `"DM"` if name unavailable)
  - Public/private channel post: `"#<channel_name>"` (or `None` if name unavailable)
  - Channel-redirect target: `"#<channel_name>"` (same pattern)

The label is derived via `getattr(..., "name", None)` to avoid AttributeError on
unexpected channel/user object types.

## PR #1076 — catchup-after-startup: submodule/worktree checkout fix

Before this fix, the repo-discovery loop and the recent-commits section both
checked `[ -d "$REPO/.git" ]`. In a git submodule or worktree checkout, `.git`
is a file (a "gitdir pointer"), not a directory — so `-d` would fail and the
script would either fall back to the wrong repo or skip the commits section
entirely. Fix: use `-e` (exists) not `-d` (is-directory) so both regular
checkouts (`/.git` is a directory) and submodule/worktree checkouts (`/.git`
is a file) are detected correctly.

Run: python3 tests/outbox-label-catchup-submodule.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"
CATCHUP = REPO / "skills" / "catchup-after-startup" / "scripts" / "catchup-after-startup.sh"

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
    missing = [f for f in [BRIDGE, CATCHUP] if not f.exists()]
    for m in missing:
        fail(f"file not found: {m}")
    if missing:
        sys.exit(1)

    bridge = BRIDGE.read_text()
    catchup = CATCHUP.read_text()

    # ── PR #1078: outbox_log recipient_label in discord-bridge.py ────────────

    # 1. outbox_log imported in bridge
    expect(
        "import outbox_log" in bridge or "outbox_log" in bridge,
        "discord-bridge.py: must use outbox_log module for delivery logging",
    )

    # 2. recipient_label keyword argument passed to outbox_log.append
    expect(
        "recipient_label=" in bridge,
        "discord-bridge.py: outbox_log.append must receive recipient_label= keyword argument",
    )

    # 3. DM path: label is "<username> DM" format
    expect(
        '"DM"' in bridge and "_label" in bridge,
        "discord-bridge.py: DM delivery path must set recipient_label to '<username> DM' format",
    )

    # 4. Channel path: label uses '#' prefix for channel name
    expect(
        '"#' in bridge and "_ch_name" in bridge,
        "discord-bridge.py: channel delivery path must prefix label with '#'",
    )

    # 5. Name derived via getattr(..., "name", None) — safe against AttributeError
    expect(
        'getattr(' in bridge and '"name"' in bridge and 'None' in bridge,
        "discord-bridge.py: recipient/channel name must be extracted via getattr(..., 'name', None)",
    )

    # 6. Fallback when name unavailable: DM path uses "DM" literal
    expect(
        'else "DM"' in bridge or "else 'DM'" in bridge,
        "discord-bridge.py: DM label must fall back to 'DM' when recipient name is unavailable",
    )

    # 7. Channel fallback: None when channel name unavailable (not a blank string)
    # Pattern: `f"#{_ch_name}" if _ch_name else None`
    expect(
        "if _ch_name else None" in bridge,
        "discord-bridge.py: channel label must fall back to None (not empty string) when name unavailable",
    )

    # 8. recipient_label used in all three delivery paths (poll_results, poll_proactive, channel-redirect)
    # Count occurrences — should be 3 (one per delivery path)
    label_count = bridge.count("recipient_label=_label")
    expect(
        label_count >= 3,
        f"discord-bridge.py: recipient_label=_label must appear in all 3 delivery paths (found {label_count})",
    )

    # ── PR #1076: catchup-after-startup: .git -e check (not -d) ──────────────

    # 9. repo discovery uses [ -e ... .git ] not [ -d ... .git ]
    # -e works for both regular checkouts (.git dir) and submodule/worktree (.git file)
    expect(
        '[ -e "$_cand/.git" ]' in catchup or "[ -e \"$_cand/.git\" ]" in catchup,
        "catchup-after-startup.sh: repo discovery must use [ -e .git ] not [ -d .git ] (submodule/worktree support)",
    )

    # 10. commits section uses [ -e "$REPO/.git" ] not [ -d "$REPO/.git" ]
    expect(
        '[ -e "$REPO/.git" ]' in catchup or "[ -e \"$REPO/.git\" ]" in catchup,
        "catchup-after-startup.sh: commits section must use [ -e $REPO/.git ] not [ -d ] (submodule/worktree support)",
    )

    # 11. No bare [ -d ... .git ] check remaining (the bug pattern)
    d_git_checks = re.findall(r'\[\s*-d\s+["\']?\S*\.git["\']?\s*\]', catchup)
    expect(
        len(d_git_checks) == 0,
        f"catchup-after-startup.sh: must NOT have any [ -d ... .git ] checks (use -e); found: {d_git_checks}",
        ctx="\n".join(d_git_checks[:5]),
    )

    # 12. Comment explains why -e is used (documents the submodule/worktree case)
    expect(
        "submodule" in catchup and "worktree" in catchup,
        "catchup-after-startup.sh: must have comment explaining submodule/worktree reason for -e check",
    )

    # 13. CATCHUP_HOURS / positional arg both honored (de-flake fix from same PR)
    expect(
        "CATCHUP_HOURS" in catchup,
        "catchup-after-startup.sh: must support CATCHUP_HOURS env var for hours window",
    )
    expect(
        '"${1:-' in catchup or "${1:-" in catchup,
        "catchup-after-startup.sh: positional arg must be honored as hours override",
    )

    # 14. git log uses capture-then-head pattern (SIGPIPE safety)
    # Pattern: capture full git log output, then pipe to head
    expect(
        "git_rows=$(git" in catchup or "git_rows=$(" in catchup,
        "catchup-after-startup.sh: git log output must be captured before piping to head (SIGPIPE safety)",
    )

    # 15. SUTANDO_REPO_DIR respected for repo path
    expect(
        "SUTANDO_REPO_DIR" in catchup,
        "catchup-after-startup.sh: must respect SUTANDO_REPO_DIR env var for repo path",
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        total = 15
        print(f"All {total} tests passed.")


if __name__ == "__main__":
    main()
