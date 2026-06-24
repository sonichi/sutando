#!/usr/bin/env python3
"""Tests for src/util_paths.py claude_home_path() fallback banner.

Owner directive #design 2026-06-07 (Option A+ for channels migration):
when CLAUDE_CONFIG_DIR is unset, claude_home_path() emits a stderr
deprecation banner ONCE per process. CLAUDE_HOME (legacy test override)
should NOT trigger the banner. CCD set should NOT trigger the banner.

Run: python3 tests/util-paths-ccd-banner.test.py
"""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"


def _run_probe(env_overrides: dict[str, str | None]) -> tuple[str, str, int]:
    """Run a minimal Python probe that imports util_paths and calls
    claude_home_path() three times, then prints the resolved base.
    Return (stdout, stderr, returncode). env_overrides=None unsets the key.
    """
    env = os.environ.copy()
    # Always start from a clean slate for these vars.
    for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME", "SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER"):
        env.pop(k, None)
    for k, v in env_overrides.items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    probe = f"""
import sys
sys.path.insert(0, {str(SRC)!r})
from util_paths import claude_home_path
# Call thrice to verify single-fire across calls.
for _ in range(3):
    p = claude_home_path('channels', 'discord', 'access.json')
print('RESOLVED:', p, flush=True)
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout, result.stderr, result.returncode


def assert_eq(actual, expected, label):
    if actual == expected:
        print(f"  PASS: {label}")
        return True
    print(f"  FAIL: {label}\n    expected: {expected!r}\n    actual:   {actual!r}")
    return False


def assert_in(needle, haystack, label):
    if needle in haystack:
        print(f"  PASS: {label}")
        return True
    print(f"  FAIL: {label}\n    needle: {needle!r}\n    haystack: {haystack!r}")
    return False


def assert_not_in(needle, haystack, label):
    if needle not in haystack:
        print(f"  PASS: {label}")
        return True
    print(f"  FAIL: {label}\n    unexpectedly found: {needle!r}\n    in: {haystack!r}")
    return False


def main() -> int:
    print("util_paths.py — CCD fallback banner tests")
    print("=" * 50)
    passed = []

    # Test 1: CCD set → no banner, resolved path uses CCD.
    print("\n[1] CLAUDE_CONFIG_DIR set → no banner, resolves under CCD")
    with tempfile.TemporaryDirectory() as ccd:
        out, err, rc = _run_probe({"CLAUDE_CONFIG_DIR": ccd})
        passed.append(assert_eq(rc, 0, "exit 0"))
        passed.append(assert_not_in(
            "CLAUDE_CONFIG_DIR not set", err,
            "no fallback banner on stderr"))
        passed.append(assert_in(
            f"RESOLVED: {ccd}/channels/discord/access.json", out,
            "resolved path uses CCD"))

    # Test 2: CLAUDE_HOME set (legacy test override) → no banner.
    print("\n[2] CLAUDE_HOME set (CCD unset) → no banner, resolves under CLAUDE_HOME")
    with tempfile.TemporaryDirectory() as home:
        out, err, rc = _run_probe({"CLAUDE_HOME": home})
        passed.append(assert_eq(rc, 0, "exit 0"))
        passed.append(assert_not_in(
            "CLAUDE_CONFIG_DIR not set", err,
            "no fallback banner on stderr (CLAUDE_HOME is a test override, not the deprecated path)"))
        passed.append(assert_in(
            f"RESOLVED: {home}/channels/discord/access.json", out,
            "resolved path uses CLAUDE_HOME"))

    # Test 3: Both unset → banner fires ONCE.
    print("\n[3] CCD + CLAUDE_HOME unset → banner fires ONCE, falls back to ~/.claude/")
    out, err, rc = _run_probe({})
    passed.append(assert_eq(rc, 0, "exit 0"))
    passed.append(assert_in(
        "CLAUDE_CONFIG_DIR not set", err,
        "fallback banner present on stderr"))
    # Count banner occurrences — should be exactly 1 even though we called the
    # helper three times in the probe.
    occurrences = err.count("CLAUDE_CONFIG_DIR not set")
    passed.append(assert_eq(
        occurrences, 1,
        f"banner fires exactly once across 3 helper calls (got {occurrences})"))
    home_default = str(Path.home() / ".claude")
    passed.append(assert_in(
        f"RESOLVED: {home_default}/channels/discord/access.json", out,
        "resolved path uses ~/.claude/ default"))

    # Test 4: Suppression env var set → no banner even when both unset.
    print("\n[4] SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER=1 → no banner, still falls back")
    out, err, rc = _run_probe({"SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER": "1"})
    passed.append(assert_eq(rc, 0, "exit 0"))
    passed.append(assert_not_in(
        "CLAUDE_CONFIG_DIR not set", err,
        "no banner with suppression env var set"))
    home_default = str(Path.home() / ".claude")
    passed.append(assert_in(
        f"RESOLVED: {home_default}/channels/discord/access.json", out,
        "resolved path still uses ~/.claude/ default (suppression only silences banner)"))

    print("\n" + "=" * 50)
    failed = passed.count(False)
    print(f"Pass: {passed.count(True)} / {len(passed)}")
    if failed:
        print(f"Fail: {failed}")
        return 1
    print("All claude_home_path() banner contract tests pass.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
