#!/usr/bin/env python3
"""The gate must fire when the BASE moves under a PR's own files after approval.

Built against a labeled real case (#3823): two approvals predated #3818's merge
into the same two files, one re-approval followed it, and only that one covered
the tree that landed. Driven here on a synthetic repo so it needs no network.
"""
import importlib.util
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("acb", ROOT / "scripts" / "approval-covers-base.py")
acb = importlib.util.module_from_spec(spec); spec.loader.exec_module(acb)

fails, ran = [], 0
def check(name, cond, detail=""):
    global ran; ran += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail and not cond else ""))
    if not cond: fails.append(name)

def git(d, *a, when=None):
    # `--date=` sets the AUTHOR date; `git log --since` filters on the COMMITTER
    # date, so a fixture that sets only the former is filtered by wall clock.
    import os
    env = dict(os.environ)
    if when:
        env["GIT_COMMITTER_DATE"] = when
        env["GIT_AUTHOR_DATE"] = when
    r = subprocess.run(["git", "-C", d, *a], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise SystemExit(f"git {' '.join(a)}: {r.stderr[:200]}")
    return r.stdout.strip()

print("approval-covers-base")
with tempfile.TemporaryDirectory() as d:
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.email", "t@t.t"); git(d, "config", "user.name", "t")
    (pathlib.Path(d) / "shared.sh").write_text("v1\n")
    (pathlib.Path(d) / "other.sh").write_text("v1\n")
    git(d, "add", "shared.sh", "other.sh")
    # Dates are the discriminator, so they are set explicitly, not by wall clock.
    git(d, "commit", "-q", "-m", "base", when="2026-09-03T16:00:00Z")
    T_EARLY = "2026-09-03T16:30:00Z"
    (pathlib.Path(d) / "shared.sh").write_text("v2\n")
    git(d, "add", "shared.sh")
    git(d, "commit", "-q", "-m", "base moves shared.sh", when="2026-09-03T17:00:00Z")
    T_LATE = "2026-09-03T17:30:00Z"

    PR_FILES = ["shared.sh"]
    early = acb.base_touched_since(d, "main", T_EARLY, PR_FILES)
    late = acb.base_touched_since(d, "main", T_LATE, PR_FILES)
    check("an approval BEFORE the base change is UNCOVERED", early == ["shared.sh"], f"got {early}")
    check("an approval AFTER the base change is COVERED", late == [], f"got {late}")

    # The property that makes it a gate and not a mood: a base change to a file
    # the PR does NOT touch must not invalidate anybody's approval.
    untouched = acb.base_touched_since(d, "main", T_EARLY, ["other.sh"])
    check("a base change to an UNRELATED file does not fire", untouched == [], f"got {untouched}")
    check("no files -> no claim", acb.base_touched_since(d, "main", T_EARLY, []) == [])

# The fetch is the one call that fails toward "covered": a stale base has no
# commits since any approval, so every row reads as covering.
with tempfile.TemporaryDirectory() as d:
    git(d, "init", "-q", "-b", "main")
    ok = acb.refresh_base(d, "origin", "main")
    check("a fetch against a MISSING remote returns False (fails closed)", ok is False,
          f"got {ok!r} — a failed refresh must not read as a successful one")

src_main = (ROOT / "scripts" / "approval-covers-base.py").read_text()
check("main REFUSES on a failed refresh rather than comparing a stale base",
      "REFUSING: could not refresh" in src_main and "return 3" in src_main)
check("the refusal is reachable — main calls refresh_base",
      "refresh_base(a.repo_dir" in src_main)
check("check=False is gone from the fetch path", "check=False" not in src_main)

# Non-OPEN returns early ON PURPOSE: once merged, the PR's own merge commit is
# on the base and would match every approval, masking the covering one.
src = (ROOT / "scripts" / "approval-covers-base.py").read_text()
check("non-OPEN PRs are gated out before any base comparison",
      'if pr["state"] != "OPEN"' in src)
check("time-based, not commit_id: a re-stamped review keeps submitted_at",
      "commit_id" not in src.split('"""')[2])


# --- main(), driven in-process: a subprocess run is invisible to coverage, and
# these branches are the ones a caller actually reaches.
import contextlib
import io

def drive(argv, *, pr, reviews, perms, refresh=True, touched=None):
    """Run main(argv) with the network stubbed; returns (rc, stdout+stderr)."""
    def fake_gh(args):
        if args[0] == "pr":
            return pr
        if "/reviews" in args[1]:
            return reviews
        if "/permission" in args[1]:
            return {"permission": perms}
        raise AssertionError(f"unstubbed gh call: {args}")
    real = (acb.gh, acb.refresh_base, acb.base_touched_since)
    acb.gh = fake_gh
    acb.refresh_base = lambda *a, **k: refresh
    if touched is not None:
        acb.base_touched_since = lambda *a, **k: touched
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = acb.main(argv)
    finally:
        acb.gh, acb.refresh_base, acb.base_touched_since = real
    return rc, out.getvalue() + err.getvalue()

ARGV = ["1", "--repo", "o/r"]
APPROVED = [{"user": {"login": "alice"}, "state": "APPROVED",
             "submitted_at": "2026-09-03T10:00:00Z"}]
OPEN_PR = {"state": "OPEN", "baseRefName": "main", "files": [{"path": "a.py"}]}

rc, txt = drive(ARGV, pr={"state": "MERGED", "baseRefName": "main", "files": []},
                reviews=[], perms="write")
check("main: a MERGED PR is gated out with rc 0", rc == 0 and "nothing to gate" in txt, txt)

rc, txt = drive(ARGV, pr=OPEN_PR, reviews=APPROVED, perms="write", refresh=False)
check("main: a FAILED refresh refuses with rc 3", rc == 3 and "REFUSING" in txt, txt)

rc, txt = drive(ARGV, pr=OPEN_PR, reviews=APPROVED, perms="write", touched=[])
check("main: an approval covering the tree exits 0", rc == 0 and "covers the tree" in txt, txt)

rc, txt = drive(ARGV, pr=OPEN_PR, reviews=APPROVED, perms="write", touched=["a.py"])
check("main: an UNCOVERED approval exits 1 and says NOT READY",
      rc == 1 and "UNCOVERED" in txt and "NOT READY" in txt, txt)

rc, txt = drive(ARGV, pr=OPEN_PR, reviews=APPROVED, perms="read", touched=["a.py"])
check("main: a read-tier approval does not count toward the gate",
      rc == 0 and "does not count" in txt, txt)

rc, txt = drive(ARGV, pr=OPEN_PR,
                reviews=[{"user": {"login": "bob"}, "state": "CHANGES_REQUESTED",
                          "submitted_at": "2026-09-03T10:00:00Z"}],
                perms="write", touched=["a.py"])
check("main: a CHANGES_REQUESTED review is reported, not counted as an approval",
      rc == 0 and "not an approval" in txt, txt)


# --- the two IO helpers' failure paths, which the stubs above deliberately skip
def raises_exit(fn, *a):
    try:
        fn(*a); return False
    except SystemExit:
        return True

check("gh() RAISES on a failed call — it must not return a silent empty result",
      raises_exit(acb.gh, ["--no-such-flag-here"]))

with tempfile.TemporaryDirectory() as d:
    subprocess.run(["git", "-C", d, "init", "-q", "-b", "main"], check=True)
    check("base_touched_since RAISES when git log fails, rather than reporting no movement",
          raises_exit(acb.base_touched_since, d, "no/such/ref", "2026-01-01T00:00:00Z", ["a.py"]))

print(f"\napproval-covers-base: {ran - len(fails)}/{ran} passed")
if fails:
    print("FAILED: " + ", ".join(fails)); raise SystemExit(1)
print("all passed")
