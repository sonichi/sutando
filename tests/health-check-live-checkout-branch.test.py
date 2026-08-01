#!/usr/bin/env python3
"""Tests for `check_live_checkout_branch` in src/health-check.py.

Bridges + core boot from the live checkout, and Sutando.app's 30-min health
check auto-restarts bridges onto whatever is checked out there. Observed
2026-07-29: a Jul-25 session left the live checkout on a PR branch for 4 days
— every bridge auto-restart booted 75-commits-stale feature code and nothing
surfaced it. This probe makes that drift loud.

Covers:
  a) checkout on main                  → ok
  b) checkout on a feature branch      → warn (names both branches)
  c) detached HEAD                     → warn (drift, unnamed branch)
  d) not a git repo                    → ok (degrade, no false alarm)
  e) SUTANDO_EXPECTED_BRANCH override  → ok on the pinned branch
  f) git not runnable (OSError)        → ok (degrade, no false alarm)
  g) core.expected_branch in sutando.config.local.json → ok on the pinned
     branch (durable pin for launchd/Sutando.app callers, no env needed)
  h) env override wins over config     → warn when they disagree
  i) malformed config JSON             → falls back to "main" (probe still
     runs; a broken config must not kill the health check)

Run: python3 tests/health-check-live-checkout-branch.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _reset_config_cache() -> None:
    """load_config memoizes per-process; clear it so each case reads its own
    temp-repo config (the exposed test seam — see sutando_config)."""
    sys.path.insert(0, str(REPO / "src"))
    import sutando_config  # noqa: PLC0415
    sutando_config._reset_cache_for_tests()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _mk_repo(tmp: Path, branch: str = "main") -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def main() -> int:
    os.environ.pop("SUTANDO_EXPECTED_BRANCH", None)

    with tempfile.TemporaryDirectory() as td:
        repo = _mk_repo(Path(td))
        r = hc.check_live_checkout_branch(repo)
        check(r["status"] == "ok" and "'main'" in r["detail"],
              f"a) on main -> ok, got {r}")

    with tempfile.TemporaryDirectory() as td:
        repo = _mk_repo(Path(td))
        _git(repo, "switch", "-q", "-c", "fix/some-pr-branch")
        r = hc.check_live_checkout_branch(repo)
        check(r["status"] == "warn" and "fix/some-pr-branch" in r["detail"]
              and "'main'" in r["detail"],
              f"b) on feature branch -> warn naming both, got {r}")

    with tempfile.TemporaryDirectory() as td:
        repo = _mk_repo(Path(td))
        _git(repo, "checkout", "-q", "--detach")
        r = hc.check_live_checkout_branch(repo)
        check(r["status"] == "warn" and "detached" in r["detail"],
              f"c) detached HEAD -> warn, got {r}")

    with tempfile.TemporaryDirectory() as td:
        plain = Path(td) / "not-a-repo"
        plain.mkdir()
        r = hc.check_live_checkout_branch(plain)
        check(r["status"] == "ok" and "skipping" in r["detail"],
              f"d) non-git dir -> ok degrade, got {r}")

    with tempfile.TemporaryDirectory() as td:
        repo = _mk_repo(Path(td), branch="pinned-branch")
        os.environ["SUTANDO_EXPECTED_BRANCH"] = "pinned-branch"
        try:
            r = hc.check_live_checkout_branch(repo)
        finally:
            os.environ.pop("SUTANDO_EXPECTED_BRANCH", None)
        check(r["status"] == "ok" and "pinned-branch" in r["detail"],
              f"e) SUTANDO_EXPECTED_BRANCH override honored, got {r}")

    with tempfile.TemporaryDirectory() as td:
        repo = _mk_repo(Path(td))
        real_run = hc.subprocess.run

        def _boom(*_a, **_k):
            raise OSError("git binary missing")

        hc.subprocess.run = _boom
        try:
            r = hc.check_live_checkout_branch(repo)
        finally:
            hc.subprocess.run = real_run
        check(r["status"] == "ok" and "not runnable" in r["detail"],
              f"f) git raising OSError -> ok degrade, got {r}")

    # g) durable config pin: core.expected_branch in sutando.config.local.json
    #    must be honored with NO env var set — this is the launchd/Sutando.app
    #    caller path, which never inherits an interactive shell's exports.
    with tempfile.TemporaryDirectory() as td:
        repo = _mk_repo(Path(td), branch="pinned-branch")
        (repo / "sutando.config.local.json").write_text(
            '{"core": {"expected_branch": "pinned-branch"}}\n')
        _reset_config_cache()
        try:
            r = hc.check_live_checkout_branch(repo)
        finally:
            _reset_config_cache()
        check(r["status"] == "ok" and "pinned-branch" in r["detail"],
              f"g) config core.expected_branch pin honored, got {r}")

    # h) precedence: env override beats the config pin when they disagree.
    with tempfile.TemporaryDirectory() as td:
        repo = _mk_repo(Path(td), branch="pinned-branch")
        (repo / "sutando.config.local.json").write_text(
            '{"core": {"expected_branch": "pinned-branch"}}\n')
        os.environ["SUTANDO_EXPECTED_BRANCH"] = "other-branch"
        _reset_config_cache()
        try:
            r = hc.check_live_checkout_branch(repo)
        finally:
            os.environ.pop("SUTANDO_EXPECTED_BRANCH", None)
            _reset_config_cache()
        check(r["status"] == "warn" and "'other-branch'" in r["detail"],
              f"h) env override wins over config pin, got {r}")

    # i) malformed config JSON: load_config raises → probe falls back to the
    #    "main" default instead of crashing the health check.
    with tempfile.TemporaryDirectory() as td:
        repo = _mk_repo(Path(td))
        (repo / "sutando.config.local.json").write_text('{not valid json')
        _reset_config_cache()
        try:
            r = hc.check_live_checkout_branch(repo)
        finally:
            _reset_config_cache()
        check(r["status"] == "ok" and "'main'" in r["detail"],
              f"i) malformed config -> default 'main', got {r}")

    if FAILS:
        print(f"\n{len(FAILS)} failure(s)")
        return 1
    print("\nlive-checkout-branch probe invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
