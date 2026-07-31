#!/usr/bin/env python3
"""Behavioral coverage for the SHIPPED check_skill_symlinks/fix_skill_symlinks
(the structural suite exercises a local copy of the logic; diff-coverage needs
the real functions executed). Runs entirely in temp dirs via $HOME override +
REPO_DIR patch — never touches the real ~/.claude.

Run: python3 tests/health-check-skill-symlinks-live.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules["health_check"] = hc
spec.loader.exec_module(hc)

errors = 0


def check(cond: bool, msg: str) -> None:
    global errors
    if cond:
        print(f"ok: {msg}")
    else:
        errors += 1
        print(f"FAIL: {msg}", file=sys.stderr)


orig_home = os.environ.get("HOME")
# The probe resolves its destination via claude_home_path(), which prefers
# CLAUDE_CONFIG_DIR/CLAUDE_HOME over $HOME — on a dev machine running Sutando
# those are SET, and without clearing them the fix cases would write real
# symlinks into the live skills dir (happened 2026-07-25: dangling foo/bar/zap
# links landed in the workspace claude-home). Redirect ALL three.
orig_ccd = os.environ.pop("CLAUDE_CONFIG_DIR", None)
orig_chome = os.environ.pop("CLAUDE_HOME", None)
td = Path(tempfile.mkdtemp(prefix="skill-symlinks-live-"))
try:
    fake_repo = td / "repo"
    fake_home = td / "home"

    # Case A: skills/ dir missing -> ok/skipped
    fake_repo.mkdir()
    os.environ["HOME"] = str(fake_home)
    hc.REPO_DIR = fake_repo
    r = hc.check_skill_symlinks()
    check(r["status"] == "ok" and "skipped" in r["detail"], f"A: no skills/ dir -> skipped ({r['detail']})")

    # Case B: ~/.claude/skills missing -> ok/skipped
    # Fixture skills carry SKILL.md: the probe applies skills/install.sh's
    # filter (only SKILL.md dirs are slash-invocable and get linked).
    (fake_repo / "skills" / "foo").mkdir(parents=True)
    (fake_repo / "skills" / "foo" / "SKILL.md").write_text("# foo\n")
    r = hc.check_skill_symlinks()
    check(r["status"] == "ok" and "skipped" in r["detail"], f"B: no dst dir -> skipped ({r['detail']})")

    # Case C: unlinked skill detected -> warn with _unlinked payload
    dst = fake_home / ".claude" / "skills"
    dst.mkdir(parents=True)
    (fake_repo / "skills" / "bar").mkdir()
    (fake_repo / "skills" / "bar" / "SKILL.md").write_text("# bar\n")
    (dst / "foo").symlink_to(fake_repo / "skills" / "foo")
    r = hc.check_skill_symlinks()
    check(r["status"] == "warn" and "bar" in r["detail"], f"C: unlinked bar -> warn ({r['detail']})")
    check(r.get("_unlinked") == ["bar"], f"C2: _unlinked payload ({r.get('_unlinked')})")

    # Case D: fix creates the symlink and reports it
    f = hc.fix_skill_symlinks(r)
    check(f["status"] == "ok" and "bar" in f["detail"], f"D: fix linked bar ({f['detail']})")
    check((dst / "bar").is_symlink(), "D2: symlink actually created")

    # Case E: re-check all linked -> ok
    r = hc.check_skill_symlinks()
    check(r["status"] == "ok", f"E: recheck ok ({r['detail']})")

    # Case F: non-directory entries in skills/ are skipped (continue branch)
    (fake_repo / "skills" / "README.md").write_text("not a skill")
    r = hc.check_skill_symlinks()
    check(r["status"] == "ok" and "README" not in r["detail"], f"F: plain file skipped ({r['detail']})")

    # Case G: fix error branch — dst parent missing forces symlink_to to raise
    f = hc.fix_skill_symlinks({"name": "skill-symlinks", "status": "warn",
                               "_unlinked": ["nested/never-exists"]})
    check(f["status"] == "warn" and "errors" in f["detail"], f"G: raise collected as error ({f['detail']})")

    # Case H: the --fix dispatch helper routes only _unlinked skill-symlinks rows
    (fake_repo / "skills" / "zap").mkdir()
    (fake_repo / "skills" / "zap" / "SKILL.md").write_text("# zap\n")
    r = hc.check_skill_symlinks()
    hc.apply_skill_symlink_fixes([{"name": "other", "status": "ok"}, r])
    check((dst / "zap").is_symlink(), "H: dispatch helper linked zap")

finally:
    if orig_home is not None:
        os.environ["HOME"] = orig_home
    if orig_ccd is not None:
        os.environ["CLAUDE_CONFIG_DIR"] = orig_ccd
    if orig_chome is not None:
        os.environ["CLAUDE_HOME"] = orig_chome

if errors:
    print(f"FAILED: {errors} check(s) failed", file=sys.stderr)
    sys.exit(1)
print("PASSED: shipped skill-symlink check/fix behave correctly")
