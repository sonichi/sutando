#!/usr/bin/env python3
"""Tests for src/util_paths.py claude_home_path() legacy reader-fallback.

Regression guard for the 2026-07-24 Telegram + Discord outage: bot tokens +
allowlists lived at the legacy ~/.claude/channels/ home, but the active
CLAUDE_CONFIG_DIR had no channels/ dir. claude_home_path() resolved straight to
the (empty) active dir with no fallback, so the stranded config was invisible
and both bridges exited "no token".

Policy (CLAUDE.md "Migration transition window"): when CLAUDE_CONFIG_DIR points
somewhere other than ~/.claude and the requested file is ABSENT there but
PRESENT under legacy ~/.claude/, resolve to the legacy copy and warn once.

Run: python3 tests/util-paths-legacy-fallback.test.py
"""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"

SUB = ("channels", "discord", "access.json")


def _run_probe(*, home: str, ccd: str | None, seed_legacy: bool, seed_primary: bool,
               suppress: bool = False) -> tuple[str, str, int]:
    """Set up isolated HOME (+ optional CCD) dirs, optionally seed the file in
    the legacy ~/.claude/ tree and/or the CCD tree, then resolve SUB and print
    the result. Returns (stdout, stderr, returncode).
    """
    home_p = Path(home)
    if seed_legacy:
        legacy = home_p / ".claude" / Path(*SUB)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("legacy-token\n")
    if seed_primary and ccd:
        prim = Path(ccd) / Path(*SUB)
        prim.parent.mkdir(parents=True, exist_ok=True)
        prim.write_text("primary-token\n")

    env = os.environ.copy()
    for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME",
              "SUTANDO_SUPPRESS_CLAUDE_HOME_LEGACY_FALLBACK"):
        env.pop(k, None)
    env["HOME"] = home
    if ccd is not None:
        env["CLAUDE_CONFIG_DIR"] = ccd
    if suppress:
        env["SUTANDO_SUPPRESS_CLAUDE_HOME_LEGACY_FALLBACK"] = "1"

    probe = f"""
import sys
sys.path.insert(0, {str(SRC)!r})
from util_paths import claude_home_path
p = claude_home_path(*{SUB!r})
print('RESOLVED:', p, flush=True)
"""
    r = subprocess.run([sys.executable, "-c", probe], env=env,
                       capture_output=True, text=True, timeout=10)
    return r.stdout, r.stderr, r.returncode


def _resolved(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("RESOLVED:"):
            return line[len("RESOLVED:"):].strip()
    return ""


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}: {label}")
    return cond


def main() -> int:
    results = []
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as ccd:
        # 1. Primary missing, legacy present → fall back to legacy + warn once.
        out, err, rc = _run_probe(home=home, ccd=ccd, seed_legacy=True, seed_primary=False)
        legacy_path = str(Path(home) / ".claude" / Path(*SUB))
        results.append(check(rc == 0, "case1 exit 0"))
        results.append(check(_resolved(out) == legacy_path, "case1 resolves to legacy copy"))
        results.append(check("not found under $CLAUDE_CONFIG_DIR" in err, "case1 emits warning"))

    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as ccd:
        # 2. Primary present → use primary, never fall back, no warning.
        out, err, rc = _run_probe(home=home, ccd=ccd, seed_legacy=True, seed_primary=True)
        primary_path = str(Path(ccd) / Path(*SUB))
        results.append(check(_resolved(out) == primary_path, "case2 resolves to primary when it exists"))
        results.append(check("not found under" not in err, "case2 no warning when primary exists"))

    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as ccd:
        # 3. Neither present (fresh write target) → return primary, no warning.
        out, err, rc = _run_probe(home=home, ccd=ccd, seed_legacy=False, seed_primary=False)
        primary_path = str(Path(ccd) / Path(*SUB))
        results.append(check(_resolved(out) == primary_path, "case3 returns primary when legacy absent"))
        results.append(check("not found under" not in err, "case3 no warning when nothing to fall back to"))

    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as ccd:
        # 4. Suppression env silences the warning but STILL resolves to legacy.
        out, err, rc = _run_probe(home=home, ccd=ccd, seed_legacy=True, seed_primary=False, suppress=True)
        legacy_path = str(Path(home) / ".claude" / Path(*SUB))
        results.append(check(_resolved(out) == legacy_path, "case4 still resolves to legacy when suppressed"))
        results.append(check("not found under" not in err, "case4 warning suppressed"))

    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
