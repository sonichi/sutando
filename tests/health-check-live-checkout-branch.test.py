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

Stale-but-correct-branch (added 2026-08-01). Being on the right branch is only
half of "is this checkout current" — a checkout can sit ON main and still run
weeks-old code, and cases a-i all report ok for it. Observed on the 24/7 node:
on main, 0 ahead, 15 commits behind, four merged guards consequently not
running — including the MEMORY.md load-limit warning, so the memory index
truncated silently with nothing anywhere to report it.

  j) on main, >= threshold behind      → warn, naming the count
  k) on main, 1 behind                 → ok (main moves several times a day;
     warning on any delta trains the reader to ignore the check) but the
     count is still reported
  l) up to date                        → ok, no mention of drift
  m) no origin ref (fresh init)        → ok; a probe that cannot answer must
     not invent an alarm
  n) does NOT fetch                    → advancing upstream without fetching
     leaves the count unchanged; a network call here would hang the whole run,
     so this probe can only under-report, never cry wolf

Threshold + git-binary hardening (review of #2471). The first cut read
`int(os.environ["SUTANDO_CHECKOUT_BEHIND_WARN"])` at IMPORT time, so a
non-numeric value raised ValueError before the module finished loading and took
down the entire health check; a zero warned on an exactly-current checkout; and
it was an undocumented ad-hoc env var against the repo config rule. It also
hardcoded a second bare "git", so fixing only the first call would leave the new
path able to reopen the Xcode-CLT shim modal.

  o) valid positive threshold from core.checkout_behind_warn → honored
  p) zero            → falls back (would otherwise warn on a current checkout)
  q) negative        → falls back
  r) non-integer     → falls back, no ValueError
  s) malformed config→ falls back
  t) absent key      → falls back
  u) the removed env var stays removed — a poisoned value is not even read
  v) ONE git_bin swap point; no hardcoded "git" left in the probe; the
     stale-check receives the same binary as the branch call
  w) no-runnable-git control → ok degrade, and EXACTLY ONE git invocation is
     attempted (the branch call), so the added stale-check cannot invoke a shim
     the pre-existing call did not already hit

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

    # --- STALE-but-correct-branch (2026-08-01) ---------------------------------
    # The branch-name comparison above answers "am I on the right branch", which
    # is only half of "is this checkout current". A checkout can sit ON main and
    # still execute weeks-old code. Observed on the 24/7 node: on main, 0 ahead,
    # 15 commits behind, four merged guards not running as a result — including
    # the MEMORY.md load-limit warning, so the memory index truncated silently
    # with nothing to report it. Cases a-i all pass in that state.
    def _mk_clone_behind(td: Path, n: int) -> Path:
        """A clone that is on `main` and `n` commits behind its own origin."""
        up = _mk_repo(td, "main")
        work = td / "work"
        subprocess.run(["git", "clone", "-q", str(up), str(work)],
                       check=True, capture_output=True)
        for i in range(n):
            (up / "f.txt").write_text(f"{i}\n")
            _git(up, "add", "f.txt")
            _git(up, "commit", "-q", "-m", f"c{i}")
        _git(work, "fetch", "-q", "origin")
        return work

    # j) THE GAP: on the expected branch, but far behind it -> warn.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind(Path(td), hc._BEHIND_WARN_DEFAULT + 2)
        r = hc.check_live_checkout_branch(work)
        check(r["status"] == "warn", f"j) on main but behind -> warn, got {r['status']}")
        check("behind" in r["detail"] and str(hc._BEHIND_WARN_DEFAULT + 2) in r["detail"],
              f"j) warning names the count, got {r['detail'][:90]}")

    # k) A few commits behind is normal on a fast-moving main -> ok, but the
    #    count is still reported so it is visible before it becomes a problem.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind(Path(td), 1)
        r = hc.check_live_checkout_branch(work)
        check(r["status"] == "ok", f"k) 1 behind -> ok (no cry-wolf), got {r['status']}")
        check("1 commits behind" in r["detail"], f"k) ok detail still reports the count, got {r['detail']}")

    # l) An up-to-date clone must not mention drift at all.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind(Path(td), 0)
        r = hc.check_live_checkout_branch(work)
        check(r["status"] == "ok" and "behind" not in r["detail"],
              f"l) up-to-date -> clean ok, got {r}")

    # m) No remote ref at all (fresh init, renamed remote) -> degrade to ok.
    #    A probe that cannot answer must not invent an alarm.
    with tempfile.TemporaryDirectory() as td:
        repo = _mk_repo(Path(td))          # no origin
        check(hc._commits_behind(repo, "main") is None,
              "m) missing origin ref -> None, not a crash or a fake count")
        r = hc.check_live_checkout_branch(repo)
        check(r["status"] == "ok", f"m) no remote -> ok, got {r['status']}")

    # n) CONTRACT: the probe does NOT fetch. Advance upstream WITHOUT fetching;
    #    the reported count must not move. A health check that hits the network
    #    hangs the whole run on a flaky link, so this can only under-report —
    #    which is why the warning text says the count is against the last ref.
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        work = _mk_clone_behind(tdp, 1)
        before = hc._commits_behind(work, "main")
        up = tdp / "repo"
        for i in range(5):
            (up / "f.txt").write_text(f"extra{i}\n")
            _git(up, "add", "f.txt")
            _git(up, "commit", "-q", "-m", f"extra{i}")
        after = hc._commits_behind(work, "main")
        check(before == after == 1,
              f"n) no implicit fetch: count stayed {before} -> {after} (want 1 -> 1)")

    # --- threshold config + invalid classes (review of #2471) --------------
    # The first cut read `int(os.environ["SUTANDO_CHECKOUT_BEHIND_WARN"])` at
    # IMPORT time, so a non-numeric value raised ValueError before the module
    # finished loading and took down the whole health check — a probe meant to
    # reveal stale guards instead reporting no health at all. And a zero warned
    # on an exactly-current checkout. Threshold now comes from the same durable
    # `core` config as expected_branch, read lazily, with every invalid class
    # falling back instead of crashing or crying wolf.
    def _with_core(td: Path, cfg: str) -> Path:
        repo = _mk_repo(td)
        (repo / "sutando.config.local.json").write_text(cfg)
        _reset_config_cache()
        return repo

    for label, cfg, want in [
        ("o) valid positive threshold honored", '{"core": {"checkout_behind_warn": 3}}', 3),
        ("p) zero falls back (would warn on a current checkout)", '{"core": {"checkout_behind_warn": 0}}', hc._BEHIND_WARN_DEFAULT),
        ("q) negative falls back", '{"core": {"checkout_behind_warn": -5}}', hc._BEHIND_WARN_DEFAULT),
        ("r) non-integer falls back (no ValueError)", '{"core": {"checkout_behind_warn": "not-a-number"}}', hc._BEHIND_WARN_DEFAULT),
        ("s) malformed config falls back", '{not valid json', hc._BEHIND_WARN_DEFAULT),
        ("t) absent key falls back", '{"core": {}}', hc._BEHIND_WARN_DEFAULT),
    ]:
        with tempfile.TemporaryDirectory() as td:
            repo = _with_core(Path(td), cfg)
            try:
                got = hc._behind_warn_threshold(repo)
            finally:
                _reset_config_cache()
            check(got == want, f"{label}: got {got}, want {want}")

    # u) the removed env var must stay removed — a poisoned value must not be
    #    read at all, let alone crash the module.
    os.environ["SUTANDO_CHECKOUT_BEHIND_WARN"] = "not-a-number"
    try:
        with tempfile.TemporaryDirectory() as td:
            repo = _mk_repo(Path(td))
            _reset_config_cache()
            try:
                check(hc._behind_warn_threshold(repo) == hc._BEHIND_WARN_DEFAULT,
                      "u) poisoned env var is ignored, not parsed")
            finally:
                _reset_config_cache()
    finally:
        os.environ.pop("SUTANDO_CHECKOUT_BEHIND_WARN", None)

    # --- git-binary single swap point (review of #2471) --------------------
    # Both subprocesses in this probe must go through ONE binary value, so the
    # #2469 resolver swap fixes both together. Two hardcoded "git" strings meant
    # fixing one and leaving the other able to reopen the Xcode-CLT shim modal.
    src = (REPO / "src" / "health-check.py").read_text()
    fn = src[src.index("def check_live_checkout_branch"):]
    fn = fn[:fn.index("\ndef ", 1)]
    check('git_bin = "git"' in fn, "v) probe defines one git_bin swap point")
    check('["git", "-C"' not in fn, "v) no hardcoded \"git\" left in the probe body")
    check("_commits_behind(repo, expected, git_bin)" in fn,
          "v) the stale-check receives the same binary as the branch call")

    # w) no-runnable-git control: when the branch call cannot run git, the probe
    #    returns ok and NEVER reaches the second subprocess — so the added call
    #    cannot invoke a shim the first call did not already hit.
    calls: list = []
    with tempfile.TemporaryDirectory() as td:
        probe_repo = _mk_repo(Path(td))      # build the fixture BEFORE patching
        real_run = hc.subprocess.run

        def _boom(cmd, *a, **k):
            calls.append(cmd)
            raise OSError("no git")

        hc.subprocess.run = _boom
        try:
            r = hc.check_live_checkout_branch(probe_repo)
        finally:
            hc.subprocess.run = real_run
    check(r["status"] == "ok" and "git not runnable" in r["detail"],
          f"w) no runnable git -> ok degrade, got {r}")
    check(len(calls) == 1 and "rev-list" not in " ".join(map(str, calls[0])),
          f"w) exactly ONE git invocation attempted, and not the stale-check: {calls}")

    # x) `_commits_behind` own failure branch. Reached ONLY by calling it
    #    directly: w) proves the branch call short-circuits first, so no
    #    end-to-end path exercises this except clause — which is exactly why
    #    diff-cover flagged these two lines. The branch is real: git can vanish
    #    between the two calls, and rev-list can hit the 10s timeout on a large
    #    repo. Either way the probe must degrade to "unanswerable", not raise
    #    inside a health check.
    with tempfile.TemporaryDirectory() as td:
        repo = _mk_repo(Path(td))
        real_run = hc.subprocess.run
        for exc, label in ((OSError("git vanished"), "OSError"),
                           (subprocess.TimeoutExpired(cmd="git", timeout=10), "TimeoutExpired")):
            def _raise(*_a, _e=exc, **_k):
                raise _e
            hc.subprocess.run = _raise
            try:
                got = hc._commits_behind(repo, "main")
            finally:
                hc.subprocess.run = real_run
            check(got is None, f"x) rev-list raising {label} -> None, got {got!r}")

    # y) The config key must be DECLARED, not just read. The reviewer asked for
    #    the config route "with the documented precedence"; its sibling
    #    expected_branch is declared in three places, and a key the code reads
    #    but nothing documents is the same documented-vs-implemented drift this
    #    probe exists to catch — pointed at ourselves.
    for rel, needle in (("docs/sutando-config.schema.json", '"checkout_behind_warn"'),
                        ("sutando.config.local.json.example", '"checkout_behind_warn"'),
                        ("docs/workspace-config.md", "checkout_behind_warn")):
        f = REPO / rel
        check(f.exists() and needle in f.read_text(),
              f"y) core.checkout_behind_warn declared in {rel}")

    if FAILS:
        print(f"\n{len(FAILS)} failure(s)")
        return 1
    print("\nlive-checkout-branch probe invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
