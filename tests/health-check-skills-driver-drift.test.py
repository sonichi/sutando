#!/usr/bin/env python3
"""Tests for `check_skills_driver_code_drift` in src/health-check.py.

`live-tree-drift` covers the repo side. Nothing covered the skills side, and a
skill pulled while its driver is running does not reach that driver: the process
froze its code at launch. Measured 2026-09-10 — three pulls in one day left the
content driver executing superseded code, and the only evidence was the
`v=<sha>@<git>` stamp the driver writes into its own log, compared by hand.

Covers:
  a) stamp == skills HEAD            -> ok, naming the sha
  b) stamp != skills HEAD            -> warn, naming BOTH shas and the remedy
  c) NEWEST stamp decides            -> warn when an older matching stamp
     precedes a newer mismatching one (a `stamps[0]` read would report ok
     on a driver that has since drifted — the exact failure being probed)
  d) no content-driver log           -> ok (a driver that never ran has no drift)
  e) skills checkout absent          -> ok (nothing to compare against)
  f) log present, no stamp in it     -> ok (driver logged before stamping)
  g) unreadable skills HEAD          -> ok (degrade; never invent an alarm)
  i) git not runnable (OSError)     -> ok (degrade, never a false alarm)
  j) real-Git control: the probe's git argv is never a bare "git"
  j2) absent-CLT control: no runnable git -> ok degrade
  h) POSITIVE CONTROL + MUTATION: the equality test is exercised, not just
     read. Inverting the `same` predicate in the source must break arm (a) —
     without this, deleting the comparison passes every other arm.

Run: python3 tests/health-check-skills-driver-drift.test.py
Exit code: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HC_SRC = REPO / "src" / "health-check.py"
spec = importlib.util.spec_from_file_location("hc", HC_SRC)
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], check=check,
                       capture_output=True, text=True)
    return (r.stdout + r.stderr).strip() if not check else r.stdout.strip()


def _mk_ws(td: str, *, skills: bool = True, log_lines: list[str] | None = None) -> tuple[Path, str]:
    """A workspace with a sutando-skills checkout and a content-driver log.

    Returns (workspace, skills_head_short). head is "" when no checkout was made.
    """
    ws = Path(td) / "workspace"
    (ws / "state").mkdir(parents=True)
    head = ""
    if skills:
        sk = ws / "skill-repos" / "sutando-skills"
        sk.mkdir(parents=True)
        _git(sk, "init", "-q", "-b", "main")
        _git(sk, "config", "user.email", "t@example.com")
        _git(sk, "config", "user.name", "t")
        (sk / "f.txt").write_text("x\n")
        _git(sk, "add", "f.txt")
        _git(sk, "commit", "-q", "-m", "init")
        head = _git(sk, "rev-parse", "--short", "HEAD")
    if log_lines is not None:
        (ws / "state" / "content-driver.log").write_text("\n".join(log_lines) + "\n")
    return ws, head


def main() -> int:
    # a) the healthy case: the running stamp is the skills HEAD.
    with tempfile.TemporaryDirectory() as td:
        ws, head = _mk_ws(td, log_lines=["placeholder"])   # head is known only after init
        (ws / "state" / "content-driver.log").write_text(f"[v=e1e1f151715f@{head}] driver started\n")
        r = hc.check_skills_driver_code_drift(ws)
        check(r["status"] == "ok" and head in r["detail"],
              f"a) stamp matches skills HEAD -> ok naming it, got {r}")

    # a2) one commit, two abbreviation lengths: the driver writes its own prefix
    #     and `--short` grows, so `==` called this drift and demanded a re-arm.
    with tempfile.TemporaryDirectory() as td:
        ws, head = _mk_ws(td, log_lines=["placeholder"])
        sk = ws / "skill-repos" / "sutando-skills"
        stamped = _git(sk, "rev-parse", "--short=9", "HEAD")
        # The stamp must differ in LENGTH from `--short HEAD`, or the string
        # compare already agrees and this arm passes against the unfixed probe.
        check(stamped != head and stamped.startswith(head),
              f"a2) fixture precondition: stamp is a longer spelling of head, got {stamped!r} vs {head!r}")
        (ws / "state" / "content-driver.log").write_text(f"[v=e1e1f151715f@{stamped}] driver started\n")
        r = hc.check_skills_driver_code_drift(ws)
        check(r["status"] == "ok",
              f"a2) another spelling of HEAD is NOT drift, got {r}")
        check(stamped in r["detail"],
              f"a2) the ok still names what the driver stamped, got {r['detail']}")

    # a3) control for a2: a real other commit at the SAME short length must
    #     still warn, so a2 cannot be satisfied by never warning.
    with tempfile.TemporaryDirectory() as td:
        ws, head = _mk_ws(td, log_lines=["placeholder"])
        sk = ws / "skill-repos" / "sutando-skills"
        (sk / "f.txt").write_text("y\n")
        _git(sk, "add", "f.txt")
        _git(sk, "commit", "-q", "-m", "second")
        stale = _git(sk, "rev-parse", "--short=7", "HEAD~1")
        (ws / "state" / "content-driver.log").write_text(f"[v=e1e1f151715f@{stale}] driver started\n")
        r = hc.check_skills_driver_code_drift(ws)
        check(r["status"] == "warn",
              f"a3) a genuinely older commit still warns, got {r}")

    # b) the case the probe exists for.
    with tempfile.TemporaryDirectory() as td:
        ws, head = _mk_ws(td, log_lines=["[v=e1e1f151715f@0000000] driver started"])
        r = hc.check_skills_driver_code_drift(ws)
        check(r["status"] == "warn", f"b) stamp differs -> warn, got {r}")
        check("0000000" in r["detail"] and head in r["detail"],
              f"b) warn names BOTH the running sha and the disk HEAD, got {r['detail']}")
        check("Re-arm" in r["detail"],
              f"b) warn carries the remedy, got {r['detail']}")

    # c) the newest stamp decides. An older MATCHING stamp must not mask a
    #    newer mismatching one — a `stamps[0]` read reports ok here.
    with tempfile.TemporaryDirectory() as td:
        ws, head = _mk_ws(td, log_lines=["placeholder"])
        (ws / "state" / "content-driver.log").write_text(
            f"[v=aaaaaaaaaaaa@{head}] driver started\n"
            "[v=bbbbbbbbbbbb@0000000] driver re-armed on older code\n")
        r = hc.check_skills_driver_code_drift(ws)
        check(r["status"] == "warn" and "0000000" in r["detail"],
              f"c) NEWEST stamp decides (older match must not mask it), got {r}")

    # d) no log at all.
    with tempfile.TemporaryDirectory() as td:
        ws, _ = _mk_ws(td, log_lines=None)
        r = hc.check_skills_driver_code_drift(ws)
        check(r["status"] == "ok", f"d) no driver log -> ok, got {r}")

    # e) no skills checkout.
    with tempfile.TemporaryDirectory() as td:
        ws, _ = _mk_ws(td, skills=False, log_lines=["[v=e1e1f151715f@abc1234] x"])
        r = hc.check_skills_driver_code_drift(ws)
        check(r["status"] == "ok", f"e) no skills checkout -> ok, got {r}")

    # f) a log with no stamp in it.
    with tempfile.TemporaryDirectory() as td:
        ws, _ = _mk_ws(td, log_lines=["driver started", "CONTENT_ITEM_EXPIRED: 1"])
        r = hc.check_skills_driver_code_drift(ws)
        check(r["status"] == "ok" and "no stamp" in r["detail"],
              f"f) log without a stamp -> ok, got {r}")

    # g) skills dir has .git but HEAD is unreadable (no commit yet) -> degrade.
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / "workspace"
        (ws / "state").mkdir(parents=True)
        sk = ws / "skill-repos" / "sutando-skills"
        sk.mkdir(parents=True)
        _git(sk, "init", "-q", "-b", "main")          # no commit: rev-parse fails
        (ws / "state" / "content-driver.log").write_text("[v=e1e1f151715f@abc1234] x\n")
        r = hc.check_skills_driver_code_drift(ws)
        check(r["status"] == "ok" and "could not read" in r["detail"],
              f"g) unreadable skills HEAD -> ok degrade naming THAT cause, got {r}")

    # i) git is not runnable at all -> the except arm degrades to ok. A probe
    #    that cannot measure must not invent an alarm.
    with tempfile.TemporaryDirectory() as td:
        ws, head = _mk_ws(td, log_lines=["placeholder"])
        (ws / "state" / "content-driver.log").write_text(f"[v=e1e1f151715f@{head}] x\n")
        real_run = hc.subprocess.run

        def _boom(*a, **k):
            raise OSError("git: not found")

        hc.subprocess.run = _boom
        try:
            r = hc.check_skills_driver_code_drift(ws)
        finally:
            hc.subprocess.run = real_run
        check(r["status"] == "ok" and "not asserting drift" in r["detail"],
              f"i) git unrunnable -> ok degrade, not a false alarm, got {r}")

    # The macOS /usr/bin/git stub raises an install dialog no timeout can suppress.
    # Assert the ARGV built, not the verdict: a decision test cannot see the invoke.
    with tempfile.TemporaryDirectory() as td:
        ws, head = _mk_ws(td, log_lines=["placeholder"])
        (ws / "state" / "content-driver.log").write_text("[v=e1e1f151715f@" + head + "] x\n")
        real_run = hc.subprocess.run
        seen = []

        def _spy(argv, *a, **k):
            seen.append(argv)
            return real_run(argv, *a, **k)

        hc.subprocess.run = _spy
        try:
            hc.check_skills_driver_code_drift(ws)
        finally:
            hc.subprocess.run = real_run
        gitcalls = [c for c in seen if c and "rev-parse" in list(c)]
        check(bool(gitcalls), "j) the probe invokes git at all (guards the arm below)")
        check(all(list(c)[0] != "git" for c in gitcalls),
              "j) NO call is a bare 'git' - it goes through the resolver, got " + repr(gitcalls))

    # j2) resolver reports no runnable git -> ok degrade, naming that cause.
    with tempfile.TemporaryDirectory() as td:
        ws, head = _mk_ws(td, log_lines=["placeholder"])
        (ws / "state" / "content-driver.log").write_text("[v=e1e1f151715f@" + head + "] x\n")
        real_argv = hc.git_argv

        def _no_git(*a):
            raise hc.GitUnavailable("no runnable git on this host")

        hc.git_argv = _no_git
        try:
            r = hc.check_skills_driver_code_drift(ws)
        finally:
            hc.git_argv = real_argv
        check(r["status"] == "ok" and "no runnable git" in r["detail"],
              "j2) absent CLT -> ok degrade naming the cause, got " + repr(r))

    # h) POSITIVE CONTROL + MUTATION. Arm (a) must depend on the equality test.
    #    Invert it in the source, load THAT, and prove the healthy case breaks.
    src = HC_SRC.read_text()
    target = "    if same:"
    check(src.count(target) == 1,
          "h) the mutated predicate is present exactly once (update the arm if it moved)")
    if src.count(target) == 1:
        with tempfile.TemporaryDirectory() as td:
            mutated = src.replace(target, "    if not same:", 1)
            mpath = Path(td) / "hc_mutant.py"
            mpath.write_text(mutated)
            mspec = importlib.util.spec_from_file_location("hc_mut", mpath)
            mhc = importlib.util.module_from_spec(mspec)
            mspec.loader.exec_module(mhc)
            ws, head = _mk_ws(td, log_lines=["placeholder"])
            (ws / "state" / "content-driver.log").write_text(f"[v=e1e1f151715f@{head}] x\n")
            rm = mhc.check_skills_driver_code_drift(ws)
            check(rm["status"] != "ok",
                  "h) inverting the `same` predicate breaks the healthy case "
                  f"(the comparison is exercised, not merely present), got {rm}")

    # k) ambiguity is the property under test, not the prefix length — 4 chars
    #    needs a few hundred objects and takes the identical code path.
    with tempfile.TemporaryDirectory() as td:
        ws, _h = _mk_ws(td, log_lines=["placeholder"])
        sk = ws / "skill-repos" / "sutando-skills"
        seen, prefix = {}, None
        for i in range(4000):
            oid = subprocess.run(["git", "-C", str(sk), "hash-object", "-w", "--stdin"],
                                 input=f"blob{i}\n", capture_output=True, text=True,
                                 check=True).stdout.strip()
            if oid[:4] in seen and seen[oid[:4]] != oid:
                prefix = oid[:4]
                break
            seen[oid[:4]] = oid
        check(prefix is not None, "k) fixture precondition: two real objects share a prefix")
        if prefix:
            amb = subprocess.run(["git", "-C", str(sk), "rev-parse", "--verify", f"{prefix}^{{commit}}"],
                                 capture_output=True, text=True)
            check(amb.returncode != 0 and "ambiguous" in amb.stderr.lower(),
                  f"k) fixture precondition: git calls {prefix!r} ambiguous, got {amb.stderr.strip()[:70]!r}")
            (ws / "state" / "content-driver.log").write_text(f"[v=e1e1f151715f@{prefix}] driver started\n")
            r = hc.check_skills_driver_code_drift(ws)
            check(r["status"] == "ok" and "INCONCLUSIVE" in r["detail"],
                  f"k) an ambiguous stamp is INCONCLUSIVE, got {r}")
            check("Re-arm" not in r["detail"],
                  f"k) and it does not advise a re-arm, got {r['detail']}")

    # l) the stamp is the THIRD git call; killing an earlier one tests the
    #    HEAD branch instead, which is a different arm.
    with tempfile.TemporaryDirectory() as td:
        ws, head = _mk_ws(td, log_lines=["placeholder"])
        (ws / "state" / "content-driver.log").write_text(f"[v=e1e1f151715f@{head}] driver started\n")
        real_run, calls = subprocess.run, {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            argv = a[0] if a else k.get("args", [])
            if "--verify" in argv and head in " ".join(argv):
                raise subprocess.TimeoutExpired(["git"], 10)
            return real_run(*a, **k)

        hc.subprocess.run = flaky
        try:
            r = hc.check_skills_driver_code_drift(ws)
        finally:
            hc.subprocess.run = real_run
        check(r["status"] == "ok" and "INCONCLUSIVE" in r["detail"],
              f"l) an unrunnable stamp lookup is INCONCLUSIVE, got {r}")
        check("Re-arm" not in r["detail"],
              f"l) and it does not advise a re-arm, got {r['detail']}")

    # m) an UNREADABLE object is present-but-unresolvable, not absent. HEAD is a
    #    SECOND commit and stays readable, so only the stamp lookup fails.
    with tempfile.TemporaryDirectory() as td:
        ws, _h = _mk_ws(td, log_lines=["placeholder"])
        sk = ws / "skill-repos" / "sutando-skills"
        stamp_full = _git(sk, "rev-parse", "HEAD")
        (sk / "f.txt").write_text("second\n")
        _git(sk, "add", "f.txt"); _git(sk, "commit", "-q", "-m", "second")
        (ws / "state" / "content-driver.log").write_text(
            f"[v=e1e1f151715f@{stamp_full[:7]}] driver started\n")
        loose = sk / ".git" / "objects" / stamp_full[:2] / stamp_full[2:]
        check(loose.is_file(), "m) fixture precondition: the stamped commit is a loose object")
        if loose.is_file():
            r_before = hc.check_skills_driver_code_drift(ws)
            check(r_before["status"] == "warn",
                  f"m) control: a readable OLDER commit does warn, got {r_before}")
            loose.chmod(0o000)
            try:
                probe = _git(sk, "rev-parse", "--verify", f"{stamp_full[:7]}^{{commit}}", check=False)
                check("Permission denied" in probe,
                      f"m) fixture precondition: git cannot read it, got {probe[:60]!r}")
                r = hc.check_skills_driver_code_drift(ws)
            finally:
                loose.chmod(0o444)
            check(r["status"] == "ok" and "INCONCLUSIVE" in r["detail"],
                  f"m) an unreadable stamp is INCONCLUSIVE, not drift, got {r}")
            check("Re-arm" not in r["detail"],
                  f"m) and it does not advise a re-arm, got {r['detail']}")

    # n) HEAD that ABBREVIATES but will not RESOLVE: `--short` reads the ref,
    #    `--verify` needs the object, so an unreadable one separates the two.
    with tempfile.TemporaryDirectory() as td:
        ws, head = _mk_ws(td, log_lines=["placeholder"])
        sk = ws / "skill-repos" / "sutando-skills"
        full = _git(sk, "rev-parse", "HEAD")
        (ws / "state" / "content-driver.log").write_text(f"[v=e1e1f151715f@{head}] driver started\n")
        loose = sk / ".git" / "objects" / full[:2] / full[2:]
        check(loose.is_file(), "n) fixture precondition: HEAD's commit is a loose object")
        if loose.is_file():
            loose.chmod(0o000)
            try:
                r = hc.check_skills_driver_code_drift(ws)
            finally:
                loose.chmod(0o444)
            check(r["status"] == "ok" and "HEAD in the skills checkout is" in r["detail"],
                  f"n) an unresolvable HEAD is unanswerable, not drift, got {r}")
            check("Re-arm" not in r["detail"],
                  f"n) and it advises no re-arm, got {r['detail']}")

    # o) an unreadable OBJECT DIRECTORY: --verify fails and --disambiguate exits
    #    ZERO with empty stdout, which reads as "no such object" unless stderr is read.
    with tempfile.TemporaryDirectory() as td:
        ws, head = _mk_ws(td, log_lines=["placeholder"])
        sk = ws / "skill-repos" / "sutando-skills"
        full = _git(sk, "rev-parse", "HEAD")
        (sk / "f.txt").write_text("second\n")
        _git(sk, "add", "f.txt"); _git(sk, "commit", "-q", "-m", "second")
        (ws / "state" / "content-driver.log").write_text(
            f"[v=e1e1f151715f@{full[:7]}] driver started\n")
        objdir = sk / ".git" / "objects" / full[:2]
        check(objdir.is_dir(), "o) fixture precondition: the stamped object has its own dir")
        if objdir.is_dir():
            objdir.chmod(0o000)
            try:
                dis = _git(sk, "rev-parse", f"--disambiguate={full[:7]}", check=False)
                check("Permission denied" in dis or dis == "",
                      f"o) fixture precondition: git cannot read the object dir, got {dis[:60]!r}")
                r = hc.check_skills_driver_code_drift(ws)
            finally:
                objdir.chmod(0o755)
            check(r["status"] == "ok" and "INCONCLUSIVE" in r["detail"],
                  f"o) an unreadable object DIR is INCONCLUSIVE, not absent, got {r}")
            check("Re-arm" not in r["detail"],
                  f"o) and it advises no re-arm, got {r['detail']}")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("all ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
