#!/usr/bin/env python3
"""Tests for src/health-check.py check_stranded_channel_config().

Regression guard for the 2026-07-24 outage: channel config (bot tokens +
allowlists) lived only at the legacy ~/.claude/channels/ home while the active
CLAUDE_CONFIG_DIR had none, so the bridges silently exited "no token". This
check surfaces that un-migrated state as a warn with the exact copy-forward
command.

Run: python3 tests/health-check-stranded-channel-config.test.py
"""
from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HC = REPO / "src" / "health-check.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("health_check_mod", HC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed(base: Path, items):
    for rel in items or ():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")


def _call(mod, *, home: str, ccd: str | None, legacy=None, active=None):
    """Seed legacy (~/.claude/channels/) and active (CCD/channels/) files, set
    HOME/CLAUDE_CONFIG_DIR, then invoke the check. Restores env afterwards."""
    _seed(Path(home) / ".claude" / "channels", legacy)
    if ccd is not None:
        _seed(Path(ccd) / "channels", active)
    saved = {k: os.environ.get(k) for k in ("HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_HOME")}
    try:
        os.environ["HOME"] = home
        os.environ.pop("CLAUDE_HOME", None)
        if ccd is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = ccd
        return mod.check_stranded_channel_config()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}: {label}")
    return cond


def main() -> int:
    mod = _load_module()
    results = []

    # 1. Stranded: legacy has telegram/.env, active dir empty → warn + names it.
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as ccd:
        r = _call(mod, home=home, ccd=ccd, legacy=["telegram/.env"])
        results.append(check(r is not None and r["status"] == "warn", "case1 warns on stranded config"))
        results.append(check(r and "telegram/.env" in r["detail"], "case1 names the stranded file"))
        results.append(check(r and "cp -p" in r["detail"], "case1 includes copy-forward remedy"))

    # 2. Migrated: file present in BOTH legacy and active → ok (not stranded).
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as ccd:
        r = _call(mod, home=home, ccd=ccd, legacy=["telegram/.env"], active=["telegram/.env"])
        results.append(check(r and r["status"] == "ok", "case2 ok when config present under CCD"))

    # 3. CLAUDE_CONFIG_DIR unset → None (active home IS ~/.claude, nothing strands).
    with tempfile.TemporaryDirectory() as home:
        r = _call(mod, home=home, ccd=None, legacy=["telegram/.env"])
        results.append(check(r is None, "case3 returns None when CCD unset"))

    # 4. No legacy channels dir at all → ok.
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as ccd:
        r = _call(mod, home=home, ccd=ccd)
        results.append(check(r and r["status"] == "ok", "case4 ok when no legacy channels dir"))

    # 5. Partial: telegram migrated, discord still stranded → warn on discord only.
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as ccd:
        r = _call(mod, home=home, ccd=ccd,
                  legacy=["telegram/.env", "discord/access.json"],
                  active=["telegram/.env"])
        results.append(check(r and r["status"] == "warn", "case5 warns when one channel still stranded"))
        results.append(check(r and "discord/access.json" in r["detail"], "case5 names discord/access.json"))
        results.append(check(r and "telegram/.env" not in r["detail"], "case5 does NOT flag migrated telegram"))

    # 6. Legacy channels dir present but unreadable → iterdir raises OSError.
    #    The check cannot establish config is un-stranded, so it must WARN
    #    (not false-green ok), never propagate.
    import stat as _stat
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as ccd:
        chan = Path(home) / ".claude" / "channels"
        chan.mkdir(parents=True)
        (chan / "telegram").mkdir()
        chan.chmod(0)  # unreadable → iterdir() raises PermissionError (OSError)
        try:
            r = _call(mod, home=home, ccd=ccd)
            results.append(check(
                r and r["status"] == "warn" and "unreadable" in r["detail"],
                "case6 warns (not false-green) when legacy channels dir unreadable"))
        finally:
            chan.chmod(_stat.S_IRWXU)  # restore so TemporaryDirectory cleanup works

    passed = sum(1 for x in results if x)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
