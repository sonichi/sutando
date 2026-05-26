#!/usr/bin/env python3
"""Structural tests for health-check + dashboard Sutando-app pgrep broadening (#1069)
and discord-bridge DISCORD_BOT_TOKEN env-var-first loading (#1050).

## PR #1069 — broaden Sutando-app pgrep to match installed .app path

Before this fix, health-check.py and dashboard.py used `src/Sutando/Sutando`
(the dev-build binary path) as the pgrep pattern. On a normal install the
binary lives at `/Applications/Sutando.app/Contents/MacOS/Sutando` — so the
health check was silently skipped (gated on dev_bin.exists()) and the dashboard
always showed "✗ Sutando app not running" on end-user machines.

Fixes:
  - dashboard.py: broadens pattern to `(Sutando|MacOS)/Sutando`
  - health-check.py:
    - Exposes `app_bin` path (`/Applications/Sutando.app/...`)
    - Runs the check when EITHER `dev_bin` OR `app_bin` exists
    - Broadens pgrep to `(Sutando|MacOS)/Sutando`
    - Keeps staleness check gated on `dev_bin.exists()` only
      (bundled .app binary + bundled source have equal mtime — comparison is meaningless)

## PR #1050 — discord-bridge: check DISCORD_BOT_TOKEN env var before .env file

Before this fix, discord-bridge.py loaded `DISCORD_BOT_TOKEN` exclusively from
`~/.claude/channels/discord/.env` at module-load time. Tests that injected a stub
token via `os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token-not-real")`
would still fail because the bridge never checked the env var, only the .env file.

Fix: check `os.environ.get("DISCORD_BOT_TOKEN")` first; fall back to .env.
Matches the pattern already used by telegram-bridge.py.

Run: python3 tests/health-check-app-pgrep.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HEALTH_CHECK = REPO / "src" / "health-check.py"
DASHBOARD = REPO / "src" / "dashboard.py"
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
    missing = [f for f in [HEALTH_CHECK, DASHBOARD, DISCORD_BRIDGE] if not f.exists()]
    for m in missing:
        fail(f"file not found: {m}")
    if missing:
        sys.exit(1)

    hc = HEALTH_CHECK.read_text()
    db_py = DASHBOARD.read_text()
    bridge = DISCORD_BRIDGE.read_text()

    # ── PR #1069: health-check.py Sutando-app pgrep broadening ──────────────

    # 1. app_bin path defined (the installed .app binary)
    expect(
        "/Applications/Sutando.app/Contents/MacOS/Sutando" in hc,
        "health-check.py: app_bin must point to /Applications/Sutando.app/Contents/MacOS/Sutando",
    )

    # 2. dev_bin still defined (dev-workflow path preserved)
    expect(
        "dev_bin" in hc,
        "health-check.py: dev_bin must still be defined (dev-workflow path)",
    )

    # 3. check gated on EITHER dev_bin OR app_bin (not just dev_bin)
    expect(
        "dev_bin.exists() or app_bin.exists()" in hc
        or "app_bin.exists() or dev_bin.exists()" in hc,
        "health-check.py: Sutando check must run when EITHER dev_bin OR app_bin exists",
    )

    # 4. pgrep broadened to match both dev and installed-app binary paths
    expect(
        "(Sutando|MacOS)/Sutando" in hc,
        "health-check.py: pgrep pattern must be '(Sutando|MacOS)/Sutando' to match installed .app",
    )
    # Old narrow pattern must NOT be the sole pattern
    expect(
        "src/Sutando/Sutando" not in hc or "(Sutando|MacOS)/Sutando" in hc,
        "health-check.py: old narrow pattern 'src/Sutando/Sutando' must be replaced by the broader one",
    )

    # 5. staleness check still gated only on dev_bin (not app_bin)
    # The staleness comparison makes no sense for a bundled .app whose source
    # mtime equals the binary mtime. Verify the guard remains dev_bin-only.
    # Anchor: "Skip when dev_bin missing" comment from the PR.
    stale_anchor = hc.find("Skip when dev_bin")
    if stale_anchor == -1:
        stale_anchor = hc.find("mark_stale_if_outdated")
    if stale_anchor != -1:
        stale_region = hc[max(0, stale_anchor - 100):stale_anchor + 500]
        expect(
            "dev_bin.exists()" in stale_region
            and "app_bin.exists()" not in stale_region,
            "health-check.py: staleness check must be gated on dev_bin.exists() ONLY (not app_bin)",
            ctx=stale_region[:400],
        )

    # ── PR #1069: dashboard.py pgrep broadening ──────────────────────────────

    # 6. dashboard.py uses the broadened pgrep pattern
    expect(
        "(Sutando|MacOS)/Sutando" in db_py,
        "dashboard.py: pgrep pattern must be '(Sutando|MacOS)/Sutando' to match installed .app",
    )

    # 7. dashboard.py no longer uses the old narrow pattern exclusively
    expect(
        "src/Sutando/Sutando" not in db_py or "(Sutando|MacOS)/Sutando" in db_py,
        "dashboard.py: old narrow 'src/Sutando/Sutando' pattern must be replaced by the broader one",
    )

    # ── PR #1050: discord-bridge DISCORD_BOT_TOKEN env-var-first ────────────

    # 8. TOKEN loaded from os.environ.get() first
    expect(
        'os.environ.get("DISCORD_BOT_TOKEN"' in bridge
        or "os.environ.get('DISCORD_BOT_TOKEN'" in bridge,
        "discord-bridge.py: TOKEN must be loaded from os.environ.get() first (env-var-first pattern)",
    )

    # 9. .env file is still the fallback (not removed entirely)
    expect(
        'channels_env' in bridge or 'discord/.env' in bridge or 'DISCORD_BOT_TOKEN=' in bridge,
        "discord-bridge.py: .env file fallback must still exist after env-var-first check",
    )

    # 10. env-var check comes BEFORE the .env file lookup in source order
    env_var_pos = bridge.find('os.environ.get("DISCORD_BOT_TOKEN"')
    if env_var_pos == -1:
        env_var_pos = bridge.find("os.environ.get('DISCORD_BOT_TOKEN'")
    channels_env_pos = bridge.find("channels_env")
    if channels_env_pos == -1:
        channels_env_pos = bridge.find("discord/.env")
    expect(
        env_var_pos != -1 and channels_env_pos != -1 and env_var_pos < channels_env_pos,
        "discord-bridge.py: os.environ.get() check must appear BEFORE the .env file lookup",
        ctx=f"env_var_pos={env_var_pos} channels_env_pos={channels_env_pos}",
    )

    # 11. .env fallback only runs when TOKEN is empty (i.e. env var wasn't set)
    # Verify the .env block is inside an `if not TOKEN:` guard
    token_guard_pos = bridge.find("if not TOKEN:")
    expect(
        token_guard_pos != -1 and token_guard_pos > env_var_pos,
        "discord-bridge.py: .env fallback must be inside 'if not TOKEN:' guard after env-var init",
    )

    # 12. Graceful exit when token is still not set after both checks
    expect(
        "DISCORD_BOT_TOKEN not set" in bridge,
        "discord-bridge.py: must still print error and exit when no token found from either source",
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        total = 17  # count of individual expect() calls
        print(f"All {total} tests passed.")


if __name__ == "__main__":
    main()
