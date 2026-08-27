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
  r2) JSON true      → falls back (bool is an int subclass: int(True) == 1, which
                       would warn on EVERY one-commit drift — the alert fatigue
                       the default of 10 exists to prevent)
  r3) JSON false     → falls back
  r4) float / numeric string → falls back (schema declares an integer)
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
import time
import types
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

    # l2) "0 behind" is a claim about the remote and this probe never fetches, so a
    #     clean verdict is only as current as the last fetch. Say when that was.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind(Path(td), 0)
        r = hc.check_live_checkout_branch(work)
        check("fetch" in r["detail"],
              f"l2) clean verdict names its fetch age, got {r['detail']}")

    # l3) Backdating FETCH_HEAD alone must move the age: `refs/remotes/*` moves only
    #     when the remote does, so a reading taken from it would call a quiet main stale.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind(Path(td), 0)
        common = Path(subprocess.run(
            ["git", "-C", str(work), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True).stdout.strip())
        if not common.is_absolute():
            common = work / common
        fh = common / "FETCH_HEAD"
        _origin = subprocess.run(["git", "-C", str(work), "remote", "get-url", "origin"],
                                 capture_output=True, text=True).stdout.strip()
        fh.write_text(f"deadbeef\t\tbranch 'main' of {_origin}\n")
        old = time.time() - (26 * 3600 + 5 * 60)
        os.utime(fh, (old, old))
        r = hc.check_live_checkout_branch(work)
        check("26h5m" in r["detail"],
              f"l3) age tracks FETCH_HEAD mtime, got {r['detail']}")

    # l4) With no fetch on record, say so. A fabricated "0h0m ago" reads as maximally
    #     fresh — this disclosure's own failure mode, inverted.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind(Path(td), 0)
        common = Path(subprocess.run(
            ["git", "-C", str(work), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True).stdout.strip())
        if not common.is_absolute():
            common = work / common
        (common / "FETCH_HEAD").unlink(missing_ok=True)
        r = hc.check_live_checkout_branch(work)
        check("no fetch of that branch recorded" in r["detail"] and "h0m" not in r["detail"],
              f"l4) absent FETCH_HEAD is named, not rendered as fresh, got {r['detail']}")

    # l5) A fetch of an UNRELATED ref must not date origin/<expected>. FETCH_HEAD's
    #     mtime is the last fetch of anything; only its content names the ref.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind(Path(td), 0)
        common = Path(subprocess.run(
            ["git", "-C", str(work), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True).stdout.strip())
        if not common.is_absolute():
            common = work / common
        (common / "FETCH_HEAD").write_text(
            "deadbeef\t\t'refs/pull/2270/head' of https://example.invalid/r\n")
        r = hc.check_live_checkout_branch(work)
        check("no fetch of that branch recorded" in r["detail"],
              f"l5) a PR-ref fetch must not read as freshly fetched, got {r['detail']}")

    # l5b) `main` fetched from ANOTHER remote must not date origin/main. FETCH_HEAD
    #      names the branch AND the URL; only the URL separates the two remotes.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind(Path(td), 0)
        common = Path(subprocess.run(
            ["git", "-C", str(work), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True).stdout.strip())
        if not common.is_absolute():
            common = work / common
        origin = subprocess.run(["git", "-C", str(work), "remote", "get-url", "origin"],
                                capture_output=True, text=True).stdout.strip()
        (common / "FETCH_HEAD").write_text(
            "deadbeef\t\tbranch 'main' of https://elsewhere.invalid/other-fork\n")
        r = hc.check_live_checkout_branch(work)
        check("no fetch of that branch recorded" in r["detail"],
              f"l5b) another remote's main must not date origin/main, got {r['detail']}")
        # ...and the origin record, written the same way, DOES date it. Without this the
        # check above passes for a function that always returns None.
        (common / "FETCH_HEAD").write_text(f"deadbeef\t\tbranch 'main' of {origin}\n")
        r = hc.check_live_checkout_branch(work)
        check("a fetch" in r["detail"],
              f"l5b-control) origin's own record must date it, got {r['detail']}")

    # l5c) `remote get-url` keeps a trailing `.git` that FETCH_HEAD drops. An exact
    #      compare would never match a real clone and silently report nothing ever.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind(Path(td), 0)
        common = Path(subprocess.run(
            ["git", "-C", str(work), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True).stdout.strip())
        if not common.is_absolute():
            common = work / common
        origin = subprocess.run(["git", "-C", str(work), "remote", "get-url", "origin"],
                                capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "-C", str(work), "remote", "set-url", "origin", origin + ".git"],
                       capture_output=True, text=True)
        (common / "FETCH_HEAD").write_text(f"deadbeef\t\tbranch 'main' of {origin}\n")
        r = hc.check_live_checkout_branch(work)
        check("a fetch" in r["detail"],
              f"l5c) a .git suffix mismatch must still match, got {r['detail']}")

    # l6) A failed `rev-parse --git-common-dir` degrades to None, never to a number.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind(Path(td), 0)
        fake = Path(td) / "git-nocommon"
        fake.write_text("#!/bin/sh\ncase \"$*\" in *--git-common-dir*) exit 3;; esac\nexec git \"$@\"\n")
        fake.chmod(0o755)
        age = hc._last_fetch_age_s(work, str(fake), "main")
        check(age is None, f"l6) nonzero --git-common-dir must degrade to None, got {age}")
        check(hc._fetch_age_phrase(age) == "no fetch of that branch recorded",
              f"l6) and renders the named phrase, got {hc._fetch_age_phrase(age)}")

    # Behavioral staleness (added 2026-08-03). The count threshold above is
    # deliberately 10 and case k) pins that 1 behind stays ok — both correct for
    # alert fatigue. But a count cannot distinguish one commit that rewrites a
    # skill from nine that touch docs, and skills are the one case with no other
    # detector at all: the agent re-reads the markdown from this checkout on
    # every invocation. (`src/` is NOT covered either -- see the w-block.)
    #
    # Observed on this node: exactly ONE commit behind, this probe reporting ok,
    # while the live `context-reconstruct` still instructed writing the shared
    # flat `state/current-track.md`, which delivers one host's anchor onto
    # another host at the same local path (#2567/#2568). This used to say that
    # collision "had destroyed a peer's anchor"; nothing was destroyed, and the
    # observation does not depend on it — what the probe missed is that the
    # running skill and the merged skill disagreed with nothing to compare.
    def _mk_clone_behind_paths(td: Path, paths: "list[str]") -> Path:
        """A clone on `main`, one commit behind per entry in `paths`."""
        up = _mk_repo(td, "main")
        work = td / "work"
        subprocess.run(["git", "clone", "-q", str(up), str(work)],
                       check=True, capture_output=True)
        for i, rel in enumerate(paths):
            f = up / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"{i}\n")
            _git(up, "add", rel)
            _git(up, "commit", "-q", "-m", f"touch {rel}")
        _git(work, "fetch", "-q", "origin")
        return work

    # v1) THE GAP: one commit behind — under the nag threshold, so case k) says
    #     ok — but it changes a skill the agent reads every invocation.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(Path(td), ["skills/context-reconstruct/SKILL.md"])
        r = hc.check_live_checkout_branch(work)
        check(r["status"] == "warn",
              f"v1) 1 behind but it changes skills/ -> warn, got {r['status']}")
        check("skills/" in r["detail"],
              f"v1) must name skills/ as the reason, got {r['detail'][:110]}")
        check("touch skills/context-reconstruct/SKILL.md" in r["detail"],
              f"v1) must name the actual commit so it is actionable, got {r['detail'][:150]}")

    # v2) Over-trigger control: the same one-commit drift in a path the agent
    #     does NOT read live must stay ok, or this re-creates the alert fatigue
    #     the threshold exists to prevent.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(Path(td), ["docs/whatever.md"])
        r = hc.check_live_checkout_branch(work)
        check(r["status"] == "ok",
              f"v2) 1 behind touching docs only -> still ok, got {r['status']} / {r['detail'][:90]}")

    # v3) Only NOT-YET-PULLED commits count. A checkout that already contains
    #     the skill change is current — history is not drift.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(Path(td), ["skills/s/SKILL.md"])
        _git(work, "pull", "-q", "--ff-only")
        r = hc.check_live_checkout_branch(work)
        check(r["status"] == "ok",
              f"v3) skill change already pulled -> ok, got {r['status']} / {r['detail'][:90]}")

    # v4) A mixed drift still names only the skill commits, and the count in the
    #     message is the TOTAL — conflating the two would misstate how far behind
    #     the checkout actually is.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(
            Path(td), ["docs/a.md", "skills/one/SKILL.md", "docs/b.md"])
        r = hc.check_live_checkout_branch(work)
        check(r["status"] == "warn", f"v4) mixed drift with a skill -> warn, got {r['status']}")
        check("1 of them change" in r["detail"],
              f"v4) must count only the skill commits, got {r['detail'][:130]}")
        check("3 commit(s) behind" in r["detail"],
              f"v4) must still report the TOTAL behind count, got {r['detail'][:130]}")

    # v5) git unrunnable -> no skill warning, and no exception either. This is the
    #     branch that decides what happens when the MEASUREMENT fails, and an
    #     uncaught raise here would take down the whole health run from inside the
    #     probe that exists to report on it. Degrade closed: say nothing extra
    #     rather than invent a warning from a failed read.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(Path(td), ["skills/s/SKILL.md"])
        real_run = hc.subprocess.run

        def _boom_on_log(argv, *a, **kw):
            if "log" in argv:
                raise OSError("git vanished mid-run")
            return real_run(argv, *a, **kw)

        hc.subprocess.run = _boom_on_log
        try:
            got = hc._behind_commits_changing(work, "main", "skills/")
            r = hc.check_live_checkout_branch(work)
        finally:
            hc.subprocess.run = real_run
        check(got == [], f"v5) a failed measurement yields no commits, got {got}")
        check(r["status"] == "ok",
              f"v5) and must not invent a warning from it, got {r['status']}")

    # v6) git present but the log call FAILS (bad ref, renamed remote) -> same
    #     degrade. Distinct from v5: there the call raised, here it returns
    #     non-zero, and the two are different lines in the function.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(Path(td), ["skills/s/SKILL.md"])
        got = hc._behind_commits_changing(work, "no-such-branch-xyz", "skills/")
        check(got == [], f"v6) non-zero rc yields no commits, got {got}")

    # v7) NET-ZERO history must stay quiet. Upstream adds a skill and removes it
    #     in the next commit; the clone fetches and sits two commits behind.
    #     Commit-path history lists BOTH commits, but the tree diff is empty —
    #     pulling would change no skill bytes. Warning here is a false
    #     behavioral-staleness alarm, i.e. exactly the alert fatigue this check
    #     exists to argue against. Reproduced independently by qingyun-wu and
    #     john-the-dev on #2573; this pins the tree-diff gate that fixes it.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        up = _mk_repo(td, "main")
        work = td / "work"
        subprocess.run(["git", "clone", "-q", str(up), str(work)],
                       check=True, capture_output=True)
        (up / "skills" / "demo").mkdir(parents=True)
        (up / "skills" / "demo" / "SKILL.md").write_text("y\n")
        _git(up, "add", "-A"); _git(up, "commit", "-q", "-m", "add skills/demo")
        _git(up, "rm", "-q", "skills/demo/SKILL.md")
        _git(up, "commit", "-q", "-m", "remove skills/demo")
        _git(work, "fetch", "-q", "origin")

        # The two questions must genuinely disagree here, or this fixture proves
        # nothing — assert the disagreement before asserting the verdict.
        hist = subprocess.run(["git", "-C", str(work), "log", "--no-merges",
                               "--format=%s", "HEAD..origin/main", "--", "skills/"],
                              capture_output=True, text=True).stdout.split()
        tree = subprocess.run(["git", "-C", str(work), "diff", "--name-only",
                               "HEAD..origin/main", "--", "skills/"],
                              capture_output=True, text=True).stdout.strip()
        check(len(hist) > 0 and tree == "",
              f"v7) fixture must have history-yes/tree-no, got hist={len(hist)} tree={tree!r}")

        got = hc._behind_commits_changing(work, "main", "skills/")
        check(got == [], f"v7) net-zero skill history yields no drift, got {got}")
        r = hc.check_live_checkout_branch(work)
        check(r["status"] == "ok",
              f"v7) and must not warn on reversible history, got {r['status']} / {r['detail'][:100]}")

    # src/ behind for a RUNNING service: the *-stale probes compare a process to
    # the file ON DISK, which agree byte for byte while the checkout is behind.
    def _with_live(paths):
        """Pin the running-service set; the real one reads this host's pgrep."""
        real = hc._running_service_sources
        hc._running_service_sources = lambda: list(paths)
        return real

    # w1) THE GAP: one commit behind, changing a source whose service is live.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(Path(td), ["src/example-service.py"])
        real = _with_live(["src/example-service.py"])
        try:
            r = hc.check_live_checkout_branch(work)
        finally:
            hc._running_service_sources = real
        check(r["status"] == "warn",
              f"w1) 1 behind changing a running service's source -> warn, got {r['status']}")
        check("touch src/example-service.py" in r["detail"],
              f"w1) must name the commit so it is actionable, got {r['detail'][:150]}")
        check("ON DISK" in r["detail"],
              f"w1) must say WHY no stale probe caught it, got {r['detail'][:200]}")

    # w2) The gate is the design: src/ moves several times a day, so warning on
    #     every src/ commit re-creates the alert fatigue the threshold prevents.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(Path(td), ["src/example-service.py"])
        real = _with_live([])
        try:
            r = hc.check_live_checkout_branch(work)
        finally:
            hc._running_service_sources = real
        check(r["status"] == "ok",
              f"w2) same drift, service NOT running -> ok, got {r['status']} / {r['detail'][:90]}")

    # w3) Running, but the drift is elsewhere -> ok. With w2: BOTH halves are
    #     required, so neither alone can fire the warning.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(Path(td), ["docs/whatever.md"])
        real = _with_live(["src/example-service.py"])
        try:
            r = hc.check_live_checkout_branch(work)
        finally:
            hc._running_service_sources = real
        check(r["status"] == "ok",
              f"w3) live service but unrelated drift -> ok, got {r['status']} / {r['detail'][:90]}")

    # w4) The gateway runs from a package, so a directory entry must cover the
    #     files under it; a file-equality check would miss every one.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(
            Path(td), ["packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py"])
        real = _with_live(["packages/ag2-sparrow/ag2_sparrow/"])
        try:
            r = hc.check_live_checkout_branch(work)
        finally:
            hc._running_service_sources = real
        check(r["status"] == "warn",
              f"w4) a directory entry covers files beneath it, got {r['status']}")

    # w5) Both stale -> the skills message wins. One probe returns one warning,
    #     and a nondeterministic choice would make the detail untestable.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(Path(td), ["skills/s/SKILL.md", "src/example-service.py"])
        real = _with_live(["src/example-service.py"])
        try:
            r = hc.check_live_checkout_branch(work)
        finally:
            hc._running_service_sources = real
        check(r["status"] == "warn" and "skills/" in r["detail"],
              f"w5) both stale -> skills message, got {r['detail'][:110]}")
        check("2 commit(s) behind" in r["detail"],
              f"w5) and the TOTAL is still 2, got {r['detail'][:130]}")

    # w6) Measurement-failure branch: the running-service list is the gate, so a
    #     pgrep failure returning everything would warn on every src/ commit.
    real_run = hc.subprocess.run

    def _boom_on_pgrep(argv, *a, **kw):
        if argv and "pgrep" in str(argv[0]):
            raise OSError("pgrep vanished")
        return real_run(argv, *a, **kw)

    hc.subprocess.run = _boom_on_pgrep
    try:
        got = hc._running_service_sources()
    finally:
        hc.subprocess.run = real_run
    check(got == [], f"w6) a failed pgrep yields no live services, got {got}")

    # w7) POSITIVE CONTROL for w6: a function that always returned [] would pass
    #     w6. pgrep is stubbed so the control does not depend on the host.
    real_run2, real_filter = hc.subprocess.run, hc._filter_pids_this_checkout
    hc.subprocess.run = lambda argv, *a, **kw: (
        types.SimpleNamespace(stdout="4242\n", returncode=0)
        if argv and "pgrep" in str(argv[0]) else real_run2(argv, *a, **kw))
    hc._filter_pids_this_checkout = lambda pids: pids
    try:
        got = hc._running_service_sources()
    finally:
        hc.subprocess.run, hc._filter_pids_this_checkout = real_run2, real_filter
    check(got and "src/voice-agent.ts" in got,
          f"w7) control: a pgrep HIT yields the source path, got {got[:3]}")

    # w8) A hit from a DIFFERENT checkout is not evidence about this one --
    #     unfiltered, two coexisting clones give a perpetual false "stale".
    real_run3, real_filter3 = hc.subprocess.run, hc._filter_pids_this_checkout
    hc.subprocess.run = lambda argv, *a, **kw: (
        types.SimpleNamespace(stdout="4242\n", returncode=0)
        if argv and "pgrep" in str(argv[0]) else real_run3(argv, *a, **kw))
    hc._filter_pids_this_checkout = lambda pids: []
    try:
        got = hc._running_service_sources()
    finally:
        hc.subprocess.run, hc._filter_pids_this_checkout = real_run3, real_filter3
    check(got == [], f"w8) a foreign-clone pid is filtered out, got {got[:3]}")

    # w9) COST: an up-to-date checkout must not pay for the census at all. Ten
    #     sequential pgreps at a 5s timeout is ~50s worst case on the common path.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(Path(td), [])   # cloned, nothing added upstream
        _git(work, "fetch", "-q", "origin")

        def _must_not_run():
            raise AssertionError("_running_service_sources called with behind == 0")

        real = hc._running_service_sources
        hc._running_service_sources = _must_not_run
        try:
            r = hc.check_live_checkout_branch(work)
            reached = False
        except AssertionError:
            r, reached = None, True
        finally:
            hc._running_service_sources = real
        check(not reached, "w9) the census is NOT invoked for an up-to-date checkout")
        check(r and r["status"] == "ok", f"w9) and the verdict is still ok, got {r}")
        check(r and "commits behind" not in r["detail"],
              f"w9) detail unchanged from the pre-PR wording, got {r['detail'] if r else None!r}")

    # w10) The short-circuit keys on `behind == 0`, NOT on falsiness — `None`
    #      (shallow / no merge-base) is unanswerable and must still be probed.
    with tempfile.TemporaryDirectory() as td:
        repo = _mk_repo(Path(td))            # no origin -> _commits_behind returns None
        called = []
        real = hc._running_service_sources
        hc._running_service_sources = lambda: called.append(1) or []
        try:
            hc.check_live_checkout_branch(repo)
        finally:
            hc._running_service_sources = real
        check(called == [1], f"w10) behind is None still reaches the census, calls={len(called)}")

    # v7b) The TREE-DIFF call has its own failure branch, distinct from the log
    #      call's (v5). It runs FIRST and is the gate, so if it raises and the
    #      code fell through to history, the net-zero false positive would come
    #      straight back on any host where the diff happens to fail. Degrade
    #      closed: no diff answer, no drift claim.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(Path(td), ["skills/s/SKILL.md"])
        real_run = hc.subprocess.run

        def _boom_on_diff(argv, *a, **kw):
            if "diff" in argv:
                raise OSError("git vanished before the tree diff")
            return real_run(argv, *a, **kw)

        hc.subprocess.run = _boom_on_diff
        try:
            got = hc._behind_commits_changing(work, "main", "skills/")
        finally:
            hc.subprocess.run = real_run
        check(got == [], f"v7b) a failed TREE DIFF yields no drift claim, got {got}")

    # v7c) The LOG call's non-zero branch. Adding the tree-diff gate made this
    #      unreachable from v6: a bad ref now fails at the DIFF and returns
    #      early, so the log call is never issued. The branch is still live in
    #      production though — the diff can succeed while the log fails — and it
    #      must degrade the same way rather than return a half-answer. Reaching
    #      it needs the diff to SUCCEED with output and only the log to fail.
    with tempfile.TemporaryDirectory() as td:
        work = _mk_clone_behind_paths(Path(td), ["skills/s/SKILL.md"])
        real_run = hc.subprocess.run

        def _log_fails(argv, *a, **kw):
            if "log" in argv:
                class _Bad:
                    returncode, stdout, stderr = 128, "", "fatal"
                return _Bad()
            return real_run(argv, *a, **kw)

        hc.subprocess.run = _log_fails
        try:
            got = hc._behind_commits_changing(work, "main", "skills/")
        finally:
            hc.subprocess.run = real_run
        check(got == [], f"v7c) diff succeeds but log fails -> no drift claim, got {got}")

    # v8) Over-trigger control for v7: a skill change that is NOT reverted must
    #     still warn, or the tree-diff gate would have silenced the real case
    #     along with the false one.
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        up = _mk_repo(td, "main")
        work = td / "work"
        subprocess.run(["git", "clone", "-q", str(up), str(work)],
                       check=True, capture_output=True)
        (up / "skills" / "demo").mkdir(parents=True)
        (up / "skills" / "demo" / "SKILL.md").write_text("y\n")
        _git(up, "add", "-A"); _git(up, "commit", "-q", "-m", "add skills/demo")
        _git(work, "fetch", "-q", "origin")
        got = hc._behind_commits_changing(work, "main", "skills/")
        check(got == ["add skills/demo"], f"v8) a real skill change still reports, got {got}")
        r = hc.check_live_checkout_branch(work)
        check(r["status"] == "warn",
              f"v8) and still warns, got {r['status']}")

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
        ("r2) JSON true falls back (bool is an int subclass)", '{"core": {"checkout_behind_warn": true}}', hc._BEHIND_WARN_DEFAULT),
        ("r3) JSON false falls back", '{"core": {"checkout_behind_warn": false}}', hc._BEHIND_WARN_DEFAULT),
        ("r4) float falls back (schema says integer)", '{"core": {"checkout_behind_warn": 2.7}}', hc._BEHIND_WARN_DEFAULT),
        ("r5) numeric string falls back", '{"core": {"checkout_behind_warn": "3"}}', hc._BEHIND_WARN_DEFAULT),
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
    check("resolve_git()" in fn,
          "v) probe RESOLVES the git binary rather than shelling a literal")
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

    # z) COMPOSITION with #2469's resolver. The previous cut left a literal
    #    `git_bin = "git"` behind a comment promising a future swap; at the
    #    merged tree of both heads `resolve_git` was imported and used elsewhere
    #    while this probe still shelled the literal, so the cumulative state
    #    kept the CLT shim modal #2469 removes. Pin both directions.
    real_mod = sys.modules.get("git_binary")
    try:
        # z1) resolver present and returning a path -> BOTH calls use that path
        stub = types.ModuleType("git_binary")
        stub.resolve_git = lambda: "/opt/fake/git"
        sys.modules["git_binary"] = stub
        seen: list = []
        real_run = hc.subprocess.run

        def _spy(cmd, *a, **k):
            seen.append(cmd[0])
            raise OSError("stop after recording")

        with tempfile.TemporaryDirectory() as td:
            repo = _mk_repo(Path(td))
            hc.subprocess.run = _spy
            try:
                hc.check_live_checkout_branch(repo)
            finally:
                hc.subprocess.run = real_run
        check(seen == ["/opt/fake/git"],
              f"z1) resolved binary is what gets shelled, got {seen}")

        # z2) resolver saying there is NO runnable git -> degrade, shell nothing
        stub.resolve_git = lambda: None
        seen2: list = []

        def _spy2(cmd, *a, **k):
            seen2.append(cmd[0])
            raise OSError("should not be reached")

        with tempfile.TemporaryDirectory() as td:
            repo = _mk_repo(Path(td))
            hc.subprocess.run = _spy2
            try:
                r = hc.check_live_checkout_branch(repo)
            finally:
                hc.subprocess.run = real_run
        check(r["status"] == "ok" and "no runnable git" in r["detail"],
              f"z2) resolver None -> ok degrade, got {r}")
        check(seen2 == [], f"z2) nothing shelled when there is no git, got {seen2}")
    finally:
        if real_mod is None:
            sys.modules.pop("git_binary", None)
        else:
            sys.modules["git_binary"] = real_mod

    if FAILS:
        print(f"\n{len(FAILS)} failure(s)")
        return 1
    print("\nlive-checkout-branch probe invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
