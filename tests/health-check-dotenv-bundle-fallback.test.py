#!/usr/bin/env python3
"""
Tests for the _resolve_dotenv() helper and its consumers in health-check.py
(PR #1973 / issue #1951).

When REPO_DIR/.env is absent (e.g. Sutando.app bundle path wiped on update),
_resolve_dotenv() falls back to the durable user-clone .env under the install
home.  This ensures all callers — check_memory_sync(), the critical-file probe,
and env_path reading in run_all_checks() — see that .env automatically.

Run: python3 tests/health-check-dotenv-bundle-fallback.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

_passed = 0
_failed = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {name}")
    else:
        _failed += 1
        print(f"  FAIL: {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# _resolve_dotenv() unit tests
# ---------------------------------------------------------------------------

def test_resolve_returns_primary_when_exists() -> None:
    """_resolve_dotenv() returns REPO_DIR/.env when it exists."""
    with tempfile.TemporaryDirectory() as td:
        fake_repo = Path(td) / "repo"
        fake_repo.mkdir()
        (fake_repo / ".env").write_text("KEY=value\n")

        fake_home = Path(td) / "home"
        fake_home.mkdir()

        with unittest.mock.patch.object(hc, "REPO_DIR", fake_repo), \
             unittest.mock.patch.object(Path, "home", staticmethod(lambda: fake_home)):
            result = hc._resolve_dotenv()

        ok("primary exists → returns primary", result == fake_repo / ".env",
           f"got {result!r}")


def test_resolve_returns_fallback_when_primary_absent() -> None:
    """_resolve_dotenv() falls back to the durable user-clone .env when primary is absent."""
    with tempfile.TemporaryDirectory() as td:
        fake_repo = Path(td) / "bundle" / "repo"
        fake_repo.mkdir(parents=True)
        # No .env at fake_repo

        fake_home = Path(td) / "home"
        fallback_dir = fake_home / ".sutando" / "repo"
        fallback_dir.mkdir(parents=True)
        (fallback_dir / ".env").write_text("KEY=fallback\n")

        with unittest.mock.patch.object(hc, "REPO_DIR", fake_repo), \
             unittest.mock.patch.object(Path, "home", staticmethod(lambda: fake_home)):
            result = hc._resolve_dotenv()

        ok("primary absent + fallback exists → returns fallback",
           result == fallback_dir / ".env",
           f"got {result!r}")


def test_resolve_returns_primary_when_both_absent() -> None:
    """_resolve_dotenv() returns primary (even absent) when both locations are missing."""
    with tempfile.TemporaryDirectory() as td:
        fake_repo = Path(td) / "bundle" / "repo"
        fake_repo.mkdir(parents=True)
        # No .env anywhere

        fake_home = Path(td) / "home"
        fake_home.mkdir(parents=True)

        with unittest.mock.patch.object(hc, "REPO_DIR", fake_repo), \
             unittest.mock.patch.object(Path, "home", staticmethod(lambda: fake_home)):
            result = hc._resolve_dotenv()

        ok("both absent → returns primary (absent)",
           result == fake_repo / ".env",
           f"got {result!r}")
        ok("both absent → returned path does not exist", not result.exists())


# ---------------------------------------------------------------------------
# check_memory_sync() integration with _resolve_dotenv()
# ---------------------------------------------------------------------------

def _run_memory_sync(fake_repo_dir: Path, fake_home: Path) -> dict:
    with unittest.mock.patch.object(hc, "REPO_DIR", fake_repo_dir), \
         unittest.mock.patch.object(Path, "home", staticmethod(lambda: fake_home)):
        return hc.check_memory_sync()


def test_memory_sync_fallback_used_when_repo_env_missing() -> None:
    """check_memory_sync reads SUTANDO_MEMORY_REPO from the fallback .env."""
    with tempfile.TemporaryDirectory() as td:
        fake_repo = Path(td) / "bundle" / "repo"
        fake_repo.mkdir(parents=True)

        fake_home = Path(td) / "home"
        fallback_dir = fake_home / ".sutando" / "repo"
        fallback_dir.mkdir(parents=True)
        (fallback_dir / ".env").write_text('SUTANDO_MEMORY_REPO="git@github.com:user/mem.git"\n')

        result = _run_memory_sync(fake_repo, fake_home)

        ok("fallback read: status is not 'warn about missing MEMORY_REPO'",
           result.get("detail", "") != "SUTANDO_MEMORY_REPO not set — cross-machine sync disabled",
           f"got detail={result.get('detail')!r}")
        ok("fallback read: returns dict", isinstance(result, dict), str(result))


def test_memory_sync_no_fallback_when_both_missing() -> None:
    """check_memory_sync → ok single-machine when neither .env nor config exists.

    RECONCILED with #2069: originally asserted a warn "SUTANDO_MEMORY_REPO not
    set". #2069 changed the not-configured verdict to an informational ok
    ("single-machine mode") to kill the recurring nag. The point of THIS test
    is that the bundle-fallback still finds nothing — verified by the ok being
    the not-configured branch, not a stale-sync warn.
    """
    with tempfile.TemporaryDirectory() as td:
        fake_repo = Path(td) / "bundle" / "repo"
        fake_repo.mkdir(parents=True)

        fake_home = Path(td) / "home"
        fake_home.mkdir(parents=True)

        result = _run_memory_sync(fake_repo, fake_home)

        ok("no fallback: ok single-machine (not a nag)",
           result.get("status") == "ok" and "single-machine" in result.get("detail", ""),
           f"got detail={result.get('detail')!r}")


def test_memory_sync_repo_env_takes_precedence() -> None:
    """check_memory_sync prefers REPO_DIR/.env over the fallback."""
    with tempfile.TemporaryDirectory() as td:
        fake_repo = Path(td) / "repo"
        fake_repo.mkdir(parents=True)
        (fake_repo / ".env").write_text('SUTANDO_MEMORY_REPO="git@github.com:user/primary.git"\n')

        fake_home = Path(td) / "home"
        fallback_dir = fake_home / ".sutando" / "repo"
        fallback_dir.mkdir(parents=True)
        (fallback_dir / ".env").write_text('SUTANDO_MEMORY_REPO="git@github.com:user/fallback.git"\n')

        result = _run_memory_sync(fake_repo, fake_home)

        ok("primary .env takes precedence",
           "SUTANDO_MEMORY_REPO not set" not in result.get("detail", ""),
           f"got detail={result.get('detail')!r}")


# ---------------------------------------------------------------------------
# check_file() + _resolve_dotenv(): critical-file probe path
# ---------------------------------------------------------------------------

def test_critical_file_probe_ok_via_fallback() -> None:
    """The .env critical-file check returns ok when the fallback path contains .env."""
    with tempfile.TemporaryDirectory() as td:
        fake_repo = Path(td) / "bundle" / "repo"
        fake_repo.mkdir(parents=True)
        # No REPO_DIR/.env — simulates wiped bundle

        fake_home = Path(td) / "home"
        fallback_dir = fake_home / ".sutando" / "repo"
        fallback_dir.mkdir(parents=True)
        (fallback_dir / ".env").write_text("GEMINI_API_KEY=test\n")

        with unittest.mock.patch.object(hc, "REPO_DIR", fake_repo), \
             unittest.mock.patch.object(Path, "home", staticmethod(lambda: fake_home)):
            dotenv_path = hc._resolve_dotenv()
            result = hc.check_file(dotenv_path, ".env")

        ok("critical-file probe: status is ok via fallback",
           result.get("status") == "ok",
           f"got {result!r}")


test_resolve_returns_primary_when_exists()
test_resolve_returns_fallback_when_primary_absent()
test_resolve_returns_primary_when_both_absent()
test_memory_sync_fallback_used_when_repo_env_missing()
test_memory_sync_no_fallback_when_both_missing()
test_memory_sync_repo_env_takes_precedence()
test_critical_file_probe_ok_via_fallback()

total = _passed + _failed
print(f"\n{_passed}/{total} passed" + (f", {_failed} failed" if _failed else " ✓"))
sys.exit(0 if _failed == 0 else 1)
