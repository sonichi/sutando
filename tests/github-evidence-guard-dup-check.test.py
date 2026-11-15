#!/usr/bin/env python3
"""hooks/github-evidence-guard.py --dup-check: before a branch, `gh pr create` or `gh pr review`,
another author's open PR changing the same function is named as advisory context, never a denial; every other
command passes with no subprocess and no file read; the matching lives in hooks/_dup_work.py,
and the core launcher registers the check. No network.

Run:  python3 tests/github-evidence-guard-dup-check.test.py
"""
from _helpers.hermetic_env import isolate_os_environ, isolated_child_env

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import builtins
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "github-evidence-guard.py"
sys.path.insert(0, str(REPO / "hooks"))
import _dup_work as dw  # noqa: E402

_spec = importlib.util.spec_from_file_location("github_evidence_guard_dup_check", HOOK)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

ARGV = ["--dup-check", "--repo", str(REPO)]
REVIEW = "gh pr review {n} --repo o/r --approve -b ok"
REVIEW_URL = "gh pr review https://github.com/o/r/pull/9 --comment -b hi"
REVIEW_R = "gh pr review -R o/r --approve 9"
REVIEW_BARE = "gh pr review --approve"


def bash(cmd, **extra):
    return dict({"tool_name": "Bash", "hook_event_name": "PreToolUse", "tool_input": {"command": cmd},
                 "cwd": "/w"}, **extra)


def pr(number, title, branch, state="OPEN", files=(), author=None, base=None):
    n = {"number": number, "title": title, "headRefName": branch, "state": state,
         "url": f"https://github.com/o/r/pull/{number}"}
    if author:
        n["author"] = {"login": author}
    if base:
        n["baseRefName"] = base
    if files:
        n["files"] = {"nodes": [{"path": f} for f in files]}
    return n


def diff(*hunks):
    """A unified diff: (file, enclosing-function) pairs."""
    return "".join(f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n@@ -1,2 +1,3 @@ {fn}\n+    x = 1\n" for f, fn in hunks)


class Fake:
    """Stands in for subprocess.run inside _dup_work: git answers locally, gh from fixtures."""

    def __init__(self, search=None, diffs=None, local=(), local_fns=(), branch="feat/x", fail=False,
                 origin="git@github.com:o/r.git", bad_diffs=()):
        self.bad_diffs = set(bad_diffs)
        self.search, self.diffs, self.fail = search or {}, diffs or {}, fail
        self.local, self.local_fns, self.branch, self.origin = list(local), list(local_fns), branch, origin
        self.calls = []

    def gh_calls(self):
        return [c for c in self.calls if c[0] == "gh"]

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        ok = lambda out: subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[0] == "git":
            rest = cmd[3:]
            if rest[:2] == ["remote", "get-url"]:
                return ok(self.origin + "\n")
            if rest[:1] == ["rev-parse"]:
                return ok(self.branch + "\n")
            if rest[:1] == ["symbolic-ref"]:
                return ok("origin/main\n")
            if rest[:2] == ["diff", "--name-only"]:
                return ok("\n".join(self.local) + "\n")
            if rest[:2] == ["diff", "-U3"]:
                return ok(diff(*self.local_fns))
            return subprocess.CompletedProcess(cmd, 1, "", "unknown git")
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 502: Bad Gateway")
        if cmd[1:3] == ["pr", "diff"]:
            if int(cmd[3]) in self.bad_diffs:
                return subprocess.CompletedProcess(cmd, 1, "", "HTTP 502")
            return ok(self.diffs.get(int(cmd[3]), ""))
        self.query = cmd
        data = {a: {"issueCount": len(v), "nodes": v} for a, v in self.search.items() if a not in ("me", "viewer")}
        for k in ("me", "viewer"):
            if k in self.search:
                data[k] = self.search[k]
        return ok(json.dumps({"data": data}))


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.log = self.d / "logs" / "dup-work.jsonl"
        for p in (mock.patch.object(dw, "log_path", return_value=self.log),
                  mock.patch.object(dw, "enabled", return_value=True)):
            p.start()
            self.addCleanup(p.stop)

    def run_hook(self, cmd, fake, **extra):
        with mock.patch.object(dw.subprocess, "run", fake):
            r = guard.decide(bash(cmd, **extra), ARGV)
        return (r.get("hookSpecificOutput") or {}).get("additionalContext", ""), r

    def rows(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []


F = "pkg/bridge.py"
WORK = {"local": [F], "local_fns": [(F, "def g():")]}


class Matching(Base):
    """The rule: flag only another author's OPEN PR that changes the same function."""

    def _overlap(self, other, files=(F,), fn="def _one_line(v):", title="fix: fold separators",
                 base="dev", viewer="me-bot", branch="fix/x"):
        fake = Fake({"ro": [other], "viewer": {"login": viewer}}, diffs={other["number"]: diff(*[(f, fn) for f in files])},
                    local=list(files), local_fns=[(f, fn) for f in files], branch=branch)
        return self.run_hook(f"gh pr create --title '{title}' --base {base} --body x", fake)

    # The four cases the narrowing is defined by.
    def test_case1_same_function_other_author_open_pr_is_flagged(self):
        text, r = self._overlap(pr(7, "other change", "fix/o", files=[F], author="someone", base="dev"))
        self.assertIn("Same function changed in an open PR:\n  #7 [OPEN]", text)
        self.assertIn("`def _one_line(v):`", text)
        self.assertNotIn("permissionDecision", r["hookSpecificOutput"])
        self.assertEqual((self.rows()[0]["verdict"], self.rows()[0]["function"]), ("FLAG", [7]))

    def test_case2_promote_pair_is_not_flagged(self):
        for other in (pr(7, "promote: fix: fold separators (#6)", "promote/x", files=[F], author="someone", base="main"),
                      pr(7, "chore(promote): fix: fold separators", "chore/promote-x", files=[F], author="someone", base="main"),
                      pr(7, "fix: fold separators", "fix/x", files=[F], author="someone", base="main")):
            self.assertEqual(self._overlap(other, base="dev")[0], "", other["title"])
            self.assertEqual(self.rows()[-1]["verdict"], "OK")
        same_base = pr(7, "fix: fold separators", "fix/o", files=[F], author="someone", base="dev")
        self.assertIn("#7", self._overlap(same_base, base="dev")[0])

    def test_case3_common_files_are_not_flagged(self):
        for f in ("docs/src-map.md", "docs/catalog.json", "uv.lock", "package-lock.json", "CHANGELOG.md",
                  "hooks/README.md", "ENGINE_MANIFEST.json"):
            other = pr(7, "other change", "fix/o", files=[f], author="someone", base="dev")
            fake = Fake({"ro": [other], "viewer": {"login": "me-bot"}}, diffs={7: diff((f, "## entry"))},
                        local=[f], local_fns=[(f, "## entry")])
            self.assertEqual(self.run_hook("gh pr create -t 'fix: x' -b y", fake)[0], "", f)
            self.assertEqual(fake.gh_calls(), [], f)

    def test_case3_common_files_neither_count_nor_crowd_out_the_real_collision(self):
        noisy = [pr(n, f"bump {n}", f"chore/b{n}", files=["CHANGELOG.md", "uv.lock"], author="someone", base="dev")
                 for n in (11, 12, 13)]
        real = pr(7, "other change", "fix/o", files=[F], author="someone", base="dev")
        fake = Fake({"ro": noisy + [real], "viewer": {"login": "me-bot"}},
                    diffs={n: diff(("CHANGELOG.md", "## 1.2"), ("uv.lock", "x")) for n in (11, 12, 13)}
                    | {7: diff((F, "def g():"))},
                    local=["CHANGELOG.md", "uv.lock", F], local_fns=[("CHANGELOG.md", "## 1.2"), (F, "def g():")])
        text, _ = self.run_hook("gh pr create -t 'fix: x' -b y", fake)
        self.assertIn("Same function changed in an open PR:\n  #7", text)
        self.assertEqual(self.rows()[0]["file_only"], [])

    def test_case4_the_authors_own_prs_are_not_flagged(self):
        other = pr(7, "other change", "fix/o", files=[F], author="Me-Bot", base="dev")
        self.assertEqual(self._overlap(other, viewer="me-bot")[0], "")

    def test_only_open_prs_count(self):
        for state in ("MERGED", "CLOSED"):
            other = pr(7, "other change", "fix/o", state, files=[F], author="someone", base="dev")
            self.assertEqual(self._overlap(other)[0], "", state)

    def test_same_file_different_function_is_not_flagged(self):
        other = pr(7, "other change", "fix/o", files=[F], author="someone", base="dev")
        fake = Fake({"ro": [other], "viewer": {"login": "me-bot"}}, diffs={7: diff((F, "def post():"))},
                    local=[F], local_fns=[(F, "def _one_line(v):")])
        self.assertEqual(self.run_hook("gh pr create -t 'fix: x' -b y", fake)[0], "")
        self.assertEqual(self.rows()[0]["file_only"], [7])

    def test_a_common_file_beside_a_code_file_does_not_widen_the_match(self):
        other = pr(7, "other", "fix/o", files=["CHANGELOG.md", F], author="someone", base="dev")
        fake = Fake({"ro": [other], "viewer": {"login": "me-bot"}},
                    diffs={7: diff(("CHANGELOG.md", "## 1.2"), (F, "def post():"))},
                    local=["CHANGELOG.md", F], local_fns=[("CHANGELOG.md", "## 1.2"), (F, "def g():")])
        self.assertEqual(self.run_hook("gh pr create -t 'fix: x' -b y", fake)[0], "")

    def test_function_overlap_is_flagged_and_file_only_is_a_count(self):
        ro = [pr(3877, "flatten field values", "fix/a", files=[F]), pr(3952, "other bridge change", "fix/b", files=[F]),
              pr(3929, "third bridge change", "fix/c", files=[F])]
        fake = Fake({"ro": ro}, diffs={3877: diff((F, "def _one_line(value):")), 3952: diff((F, "def post():")),
                                       3929: diff((F, "class Bridge:"))},
                    local=[F], local_fns=[(F, "def _one_line(value):")])
        text, _ = self.run_hook("gh pr create --title 'fix: fold separators' --body x", fake)
        self.assertIn("#3877", text)
        self.assertIn("(2 other open PR(s) touch the same files, no other signal.)", text)
        self.assertNotIn("#3952", text)
        self.assertEqual((self.rows()[0]["function"], self.rows()[0]["file_only"]), ([3877], [3952, 3929]))

    def test_review_uses_the_reviewed_prs_author_and_base(self):
        me = {"pullRequest": {"title": "fix: fold", "headRefName": "fix/m", "baseRefName": "dev", "author": {"login": "alice"},
                              "files": {"nodes": [{"path": F}]}}}
        ro = [pr(7, "x", "fix/a", files=[F], author="alice", base="dev"), pr(8, "y", "fix/b", files=[F], author="bob", base="dev"),
              pr(9, "fix: fold", "fix/c", files=[F], author="bob", base="main")]
        fake = Fake({"me": me, "ro": ro}, diffs={n: diff((F, "def g():")) for n in (7, 8, 9, 99)})
        text, _ = self.run_hook(REVIEW.format(n=99), fake)
        self.assertIn("#8", text)
        self.assertNotIn("#7 [", text)
        self.assertNotIn("#9 [", text)

    def test_branch_trigger_compares_the_working_tree_with_the_merge_base(self):
        other = pr(7, "other change", "fix/o", files=[F], author="someone", base="dev")
        fake = Fake({"ro": [other], "viewer": {"login": "me-bot"}}, diffs={7: diff((F, "def g():"))}, **WORK)
        text, _ = self.run_hook("git checkout -b fix/new-thing", fake)
        self.assertIn("#7", text)
        diffs = [c for c in fake.calls if c[0] == "git" and "diff" in c]
        self.assertTrue(diffs and all("--merge-base" in c for c in diffs), diffs)

    def test_a_branch_with_no_local_change_makes_no_search(self):
        fake = Fake({})
        text, r = self.run_hook("git checkout -b fix/new-thing", fake)
        self.assertEqual((text, r, fake.gh_calls()), ("", {}, []))
        self.assertEqual(self.rows()[0]["verdict"], "OK")

    def test_current_pr_and_own_branch_are_excluded(self):
        me = {"pullRequest": {"headRefName": "fix/mine", "files": {"nodes": [{"path": F}]}}}
        ro = [pr(3964, "this very PR", "fix/mine", files=[F]), pr(3965, "same branch elsewhere", "fix/mine", files=[F])]
        fake = Fake({"me": me, "ro": ro}, diffs={n: diff((F, "def g():")) for n in (3964, 3965)})
        self.assertEqual(self.run_hook(REVIEW.format(n=3964), fake)[0], "")

    def test_gh_failure_is_unknown_and_one_line(self):
        text, r = self.run_hook("git checkout -b fix/thing", Fake(fail=True, **WORK))
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("could not run (HTTP 502: Bad Gateway)", text)
        self.assertNotIn("permissionDecision", r["hookSpecificOutput"])
        self.assertEqual(self.rows()[0]["verdict"], "UNKNOWN")

    def test_gh_timeout_is_unknown(self):
        def slow(cmd, **kw):
            if cmd[0] == "gh":
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return Fake(**WORK)(cmd, **kw)
        text, _ = self.run_hook("git checkout -b fix/thing", slow)
        self.assertIn("did not answer within 5s", text)

    def test_no_github_origin_is_unknown(self):
        text, _ = self.run_hook("git checkout -b feat/a-b", Fake(origin="/local/bare.git"))
        self.assertIn("could not run (no GitHub repository", text)

    def test_heredoc_bodies_are_data(self):
        fake = Fake({})
        text, r = self.run_hook("cat > notes.txt <<'EOF'\ngit checkout -b fix/x-y\nEOF", fake)
        self.assertEqual((text, r, fake.calls), ("", {}, []))

    def test_trigger_parsing(self):
        got = [(t["trigger"], t.get("branch") or t.get("pr"), t["cwd"]) for t in dw.triggers(
            "cd sub && git -C repo switch --create=feat/a-b; git worktree add -b feat/c ../wt; "
            + REVIEW_URL + "; FOO=1 git checkout -B feat/d", "/w")]
        self.assertEqual(got, [("branch", "feat/a-b", "/w/sub/repo"), ("branch", "feat/c", "/w/sub"),
                               ("review", 9, "/w/sub"), ("branch", "feat/d", "/w/sub")])
        self.assertEqual(dw.triggers("git checkout main; git worktree add ../wt main; echo 'pr create'", "/w"), [])

    def test_one_search_per_trigger(self):
        fake = Fake({}, **WORK)
        self.run_hook("git checkout -b fix/thing && git switch -c feat/other-thing", fake)
        self.assertEqual([c[:3] for c in fake.gh_calls()], [["gh", "api", "graphql"]] * 2)
        self.assertEqual([r["trigger"] for r in self.rows()], ["branch", "branch"])
        searches = [a.split("=", 1)[0] for a in fake.query[4:] if "=" in a and not a.startswith("query=")]
        self.assertEqual(searches, ["ro"])  # no ticket or keyword search: those matched closed and own PRs
        self.assertIn("is:open", " ".join(fake.query))

    def test_off_switch_is_silent_and_logs_nothing(self):
        with mock.patch.object(dw, "enabled", return_value=False):
            fake = Fake({}, **WORK)
            text, _ = self.run_hook("git checkout -b fix/thing", fake)
        self.assertEqual((text, fake.calls, self.rows()), ("", [], []))


class Edges(Base):
    """Parser corners and every failure path still end in one line or a clean pass, never a raise."""

    def test_parser_corners(self):
        with mock.patch.object(dw._shell_scan, "segments", side_effect=ValueError("unbalanced")):
            self.assertEqual(dw.triggers("git checkout -b feat/a", "/w"), [])
        self.assertEqual([t["branch"] for t in dw.triggers("FOO=1; git checkout -b feat/a", "/w")], ["feat/a"])
        self.assertEqual(dw.triggers("git push", "/w"), [])
        (t,) = dw.triggers(REVIEW_R, "/w")
        self.assertEqual((t["repo"], t["pr"]), ("o/r", 9))
        (t,) = dw.triggers(REVIEW_BARE, "/w")
        self.assertIsNone(t["pr"])
        (t,) = dw.triggers("gh pr create -t x -H me:fix/y -B dev", "/w")
        self.assertEqual((t["branch"], t["base"], t["title"]), ("fix/y", "dev", "x"))

    def test_review_without_a_number_is_unknown(self):
        text, _ = self.run_hook(REVIEW_BARE + " -b ok", Fake({}))
        self.assertIn("could not run (the reviewed PR is not named", text)

    def test_review_of_a_missing_pr_is_unknown(self):
        text, _ = self.run_hook(REVIEW.format(n=5), Fake({"ro": []}))
        self.assertIn("could not run (PR #5 not found in o/r)", text)

    def test_unreadable_local_diff_is_unknown(self):
        def run(cmd, **kw):
            if cmd[:1] == ["git"] and "diff" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", "bad revision")
            return fake(cmd, **kw)
        fake = Fake({})
        text, _ = self.run_hook("gh pr create -t t -b b", run)
        self.assertIn("could not run (the local diff against the base could not be read)", text)
        self.assertEqual(fake.gh_calls(), [])

    def test_git_and_gh_that_cannot_start(self):
        def no_git(cmd, **kw):
            if cmd[0] == "git":
                raise OSError("no git")
            return Fake()(cmd, **kw)
        text, _ = self.run_hook("git checkout -b feat/a-b", no_git)
        self.assertIn("could not run (no GitHub repository", text)

        def no_gh(cmd, **kw):
            if cmd[0] == "gh":
                raise OSError("no gh")
            return Fake(**WORK)(cmd, **kw)
        text, _ = self.run_hook("git checkout -b fix/x", no_gh)
        self.assertIn("could not run (gh could not run: no gh)", text)

    def test_unreadable_search_responses(self):
        for out in ("not json", json.dumps({"data": None}), json.dumps({"errors": []})):
            def run(cmd, out=out, **kw):
                if cmd[0] == "gh":
                    return subprocess.CompletedProcess(cmd, 0, out, "")
                return Fake(**WORK)(cmd, **kw)
            text, _ = self.run_hook("git checkout -b fix/x", run)
            self.assertIn("could not run (unreadable search response)", text, out)

    def _review(self, bad):
        me = {"pullRequest": {"title": "fix: fold", "headRefName": "fix/m", "baseRefName": "dev", "author": {"login": "a"},
                              "files": {"nodes": [{"path": F}]}}}
        fake = Fake({"me": me, "ro": [pr(8, "y", "fix/b", files=[F], author="bob", base="dev")]},
                    diffs={n: diff((F, "def g():")) for n in (8, 99)}, bad_diffs=bad)
        return self.run_hook(REVIEW.format(n=99), fake)[0]

    def test_review_with_both_diffs_readable_flags_the_same_function(self):
        self.assertIn("Same function changed in an open PR:\n  #8", self._review(()))

    def test_review_with_an_unreadable_diff_is_unknown_not_ok(self):
        for bad in ((99,), (8,)):
            text = self._review(bad)
            self.assertIn("could not run (Function comparison incomplete", text, bad)
            self.assertEqual(self.rows()[-1]["verdict"], "UNKNOWN", bad)

    def test_a_flag_keeps_its_hit_and_names_the_unread_diffs(self):
        ro = [pr(7, "x", "fix/a", files=[F], author="al", base="dev"),
              pr(8, "y", "fix/b", files=[F], author="bob", base="dev")]
        fake = Fake({"ro": ro, "viewer": {"login": "me-bot"}}, diffs={7: diff((F, "def g():"))}, bad_diffs=(8,), **WORK)
        text, _ = self.run_hook("gh pr create --title t --base dev --body x", fake)
        self.assertIn("#7", text)
        self.assertIn("Function comparison incomplete: 1 diff read(s)", text)
        self.assertEqual(self.rows()[-1]["verdict"], "FLAG")

    def test_same_file_prs_beyond_the_diff_cap_are_named_not_silent(self):
        ro = [pr(n, f"t{n}", f"fix/{n}", files=[F], author="bob", base="dev") for n in (21, 22, 23, 24)]
        fake = Fake({"ro": ro, "viewer": {"login": "me-bot"}}, diffs={n: diff((F, "def other():")) for n in (21, 22, 23, 24)},
                    **WORK)
        text, _ = self.run_hook("gh pr create --title t --base dev --body x", fake)
        self.assertIn("could not run (Function comparison incomplete: 1 same-file open PR(s) beyond the top 3 "
                      "were not diffed", text)
        self.assertEqual((self.rows()[-1]["verdict"], self.rows()[-1]["undiffed"]), ("UNKNOWN", 1))
        fake = Fake({"ro": ro[:3], "viewer": {"login": "me-bot"}}, diffs={n: diff((F, "def other():")) for n in (21, 22, 23)},
                    **WORK)
        self.assertEqual(self.run_hook("gh pr create --title t --base dev --body x", fake)[0], "")

    def test_pr_diff_out_of_time_reads_nothing(self):
        fake = Fake()
        self.assertIsNone(dw.pr_diff(fake, "o/r", 5, 0.0))
        self.assertEqual(fake.calls, [])

    def test_pr_diff_that_cannot_finish_reads_nothing(self):
        def boom(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        self.assertIsNone(dw.pr_diff(boom, "o/r", 5, 2.0))

    def test_no_repo_root_says_so(self):
        self.assertIn("repo root not configured", dw.check("git checkout -b feat/a", "/w", None))

    def test_unwritable_or_unresolvable_log_still_answers(self):
        with mock.patch.object(dw, "log_path", side_effect=RuntimeError("no workspace")):
            text, _ = self.run_hook("git checkout -b fix/x", Fake(fail=True, **WORK))
        self.assertIn("could not run", text)
        self.assertEqual(self.rows(), [])

    def test_log_path_resolves_through_the_workspace_helper(self):
        sys.path.insert(0, str(REPO / "src"))
        import workspace_default
        with mock.patch.object(workspace_default, "resolve_workspace", return_value="/ws") as resolve:
            self.assertEqual(self.real_log_path(REPO), Path("/ws/logs/dup-work.jsonl"))
        resolve.assert_called_once_with(migrate=False)

    real_log_path = staticmethod(dw.log_path)

    def test_a_config_that_cannot_be_read_leaves_the_check_on(self):
        sys.path.insert(0, str(REPO / "src"))
        import sutando_config
        with mock.patch.object(sutando_config, "config_get", side_effect=RuntimeError("bad config")):
            self.assertTrue(self.real_enabled(REPO))

    real_enabled = staticmethod(dw.enabled)

    def test_summary_in_process(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "s.jsonl")
            log.write_text('{"verdict": "OK", "trigger": "branch", "elapsed_ms": 9}\nnot json\n')
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(dw.main(["--summary", "--log", str(log)]), 0)
            self.assertIn("dup-work: 1 checks", out.getvalue())
            self.assertIn("elapsed_ms p50 -  p95 -", dw.summarize(Path(d, "absent.jsonl")))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            dw.main([])

    def test_main_reraises_for_a_non_trigger(self):
        raw = json.dumps(bash("ls"))
        with mock.patch.object(guard, "decide", side_effect=RuntimeError("boom")), \
                mock.patch.object(sys, "argv", ["hook", *ARGV]), mock.patch.object(sys, "stdin", io.StringIO(raw)), \
                self.assertRaises(RuntimeError):
            guard.main()


class Replay(Base):
    """The recorded incident, as GitHub answered at the time (fixtures from the real PRs)."""

    BRIDGE = "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py"

    def test_review_of_3964_finds_3877_by_function_among_many_file_overlaps(self):
        me = {"pullRequest": {"headRefName": "fix/one-line-folds-every-separator",
                              "files": {"nodes": [{"path": self.BRIDGE}, {"path": "tests/gateway-one-line-folds.test.py"}]}}}
        others = [3952, 3929, 3853, 3408, 3326, 2436, 1789]
        ro = [pr(3877, "fix(gateway): flatten field values on the separator set readers actually split on",
                 "fix/one-line-separator-parity", files=[self.BRIDGE])]
        ro += [pr(n, f"gateway change {n}", f"fix/g{n}", files=[self.BRIDGE]) for n in others]
        diffs = {3964: diff((self.BRIDGE, "def _one_line(value: str) -> str:")),
                 3877: diff((self.BRIDGE, "def _one_line(value: str) -> str:"))}
        diffs.update({n: diff((self.BRIDGE, f"def other_{n}():")) for n in others})
        text, _ = self.run_hook(REVIEW.format(n=3964).replace("o/r", "sonichi/sutando"),
                                Fake({"me": me, "ro": ro}, diffs=diffs))
        self.assertIn("Same function changed in an open PR:\n  #3877", text)
        self.assertIn("other open PR(s) touch the same files", text)
        self.assertEqual(sum(line.startswith("  #") for line in text.splitlines()), 1)
        self.assertIn("5 same-file open PR(s) beyond the top 3 were not diffed", text)


class RealGit(Base):
    """Both sides from real git: the local diff from `git diff`, the other PR's in `gh pr diff` form (3 lines
    of context). An edit near the top of a function body must match even though git heads the
    3-context hunk with the previous function."""

    SRC = ("def alpha():\n    a = 1\n    b = 2\n    c = 3\n    return a + b + c\n\n\n"
           "def foo(x):\n    y = x + 1\n    z = y * 2\n    w = z - 3\n    v = w + 4\n    u = v * 5\n    return u\n")

    real_run = staticmethod(subprocess.run)  # the hook's subprocess.run is patched in run_hook

    def _repo(self, d):
        env = isolated_child_env(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                 GIT_COMMITTER_EMAIL="t@t")
        work = Path(d, "work")

        def g(*a):
            return subprocess.run(["git", "-C", str(work), *a], check=True, capture_output=True, text=True,
                                  env=env).stdout
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, env=env)
        g("remote", "add", "origin", "https://github.com/o/r.git")
        Path(work, "m.py").write_text(self.SRC)
        g("add", "m.py")
        g("commit", "-qm", "base")
        g("update-ref", "refs/remotes/origin/main", "HEAD")
        return work, g, env

    def _check(self, old, new):
        with tempfile.TemporaryDirectory() as d:
            work, g, env = self._repo(d)
            edited = self.SRC.replace(old, new)
            assert edited != self.SRC
            g("checkout", "-qb", "other")
            Path(work, "m.py").write_text(edited)
            g("commit", "-qam", "other edit")
            theirs = g("diff", "-U3", "main", "other")  # the shape `gh pr diff` returns
            self.assertIn("@@ def alpha():", theirs) if old == "    y = x + 1\n" else None
            g("checkout", "-q", "main")
            g("checkout", "-qb", "mine")
            Path(work, "m.py").write_text(edited)
            g("commit", "-qam", "my edit")
            other = pr(7, "other change", "other", files=["m.py"], author="someone", base="main")
            fake = Fake({"ro": [other], "viewer": {"login": "me-bot"}}, diffs={7: theirs})

            def run(cmd, **kw):
                if cmd[0] == "git":
                    return self.real_run(cmd, **dict(kw, env=env))
                return fake(cmd, **kw)
            return self.run_hook("gh pr create -t 'fix: y' -b z", run, cwd=str(work))[0]

    def test_same_edit_on_the_first_body_line_matches_across_real_git_and_gh_pr_diff(self):
        text = self._check("    y = x + 1\n", "    y = x + 10\n")
        self.assertIn("Same function changed in an open PR:\n  #7", text)
        self.assertIn("`def foo(x):`", text)

    def test_same_edit_mid_function_matches_across_real_git_and_gh_pr_diff(self):
        text = self._check("    u = v * 5\n", "    u = v * 50\n")
        self.assertIn("`def foo(x):`", text)

    def test_different_functions_still_do_not_match(self):
        self.assertEqual(dw.hunk_functions(
            "+++ b/m.py\n@@ -1,4 +1,4 @@ \n def alpha():\n-    a = 1\n+    a = 9\n     b = 2\n"),
            {"m.py": {"def alpha():"}})
        self.assertEqual(dw.hunk_functions(
            "+++ b/m.py\n@@ -6,6 +6,6 @@ def alpha():\n \n \n def foo(x):\n-    y = x + 1\n+    y = x + 10\n"),
            {"m.py": {"def foo(x):"}})


class Cost(unittest.TestCase):
    """Only a trigger pays: no subprocess and no file read otherwise."""

    def test_non_trigger_makes_no_subprocess_call_and_reads_no_file(self):
        opened = []
        real_open = builtins.open
        cmds = ("ls -la", "git status", "git checkout main", "gh pr view 5 --repo o/r --json state")
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess")) as run, \
                mock.patch.object(builtins, "open", side_effect=lambda *a, **k: opened.append(a) or real_open(*a, **k)):
            start = time.perf_counter()
            for _ in range(2000):
                for cmd in cmds:
                    self.assertEqual(guard.decide(bash(cmd), ARGV), {})
            per_call_us = (time.perf_counter() - start) / (2000 * len(cmds)) * 1e6
        self.assertEqual((run.call_count, opened), (0, []))
        print(f"\n  non-trigger pass-through: {per_call_us:.1f} us/command, 0 subprocess, 0 file reads", file=sys.stderr)

    def test_trigger_makes_one_gh_search(self):
        d = Path(tempfile.mkdtemp())
        fake = Fake({}, **WORK)
        with mock.patch.object(dw, "log_path", return_value=d / "l.jsonl"), \
                mock.patch.object(dw, "enabled", return_value=True), mock.patch.object(dw.subprocess, "run", fake):
            start = time.perf_counter()
            guard.decide(bash("git checkout -b fix/thing"), ARGV)
            ms = (time.perf_counter() - start) * 1e3
        self.assertEqual([c[:3] for c in fake.gh_calls()], [["gh", "api", "graphql"]])
        print(f"\n  branch trigger, stubbed gh: 1 gh call, {ms:.2f} ms excluding the network", file=sys.stderr)


def fake_checkout(d, switch="on"):
    """A checkout whose config pins a temp workspace and the switch, so a subprocess stays hermetic."""
    root = Path(d, "checkout")
    (root / "src").mkdir(parents=True)
    for name in ("sutando_config.py", "workspace_default.py"):
        (root / "src" / name).write_text((REPO / "src" / name).read_text())
    (root / "sutando.config.json").write_text(json.dumps({"workspace": {"path": str(Path(d, "ws"))},
                                                          "env": {dw.SWITCH_KEY: switch}}))
    return root


class Delegation(unittest.TestCase):
    def test_the_hook_delegates_to_the_helper_check(self):
        seen = []
        with mock.patch.object(dw, "check", lambda c, cwd, root: seen.append((c, cwd, root)) or "stubbed advice"):
            r = guard.decide(bash("git checkout -b feat/a", cwd="/w"), ARGV)
        self.assertEqual(seen, [("git checkout -b feat/a", "/w", str(REPO))])
        self.assertEqual(r, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "stubbed advice"}})

    def test_parser_supported_forms_reach_the_check_through_decide(self):
        for cmd in ("git checkout --quiet -b fix/test", "git checkout  -b fix/test",
                    "git  switch --quiet -c fix/test", "gh pr  review 9 -R o/r --comment -b x"):
            fake = Fake({"me": {"pullRequest": {"files": {"nodes": []}}}}, **WORK)
            with mock.patch.object(dw, "log_path", return_value=Path(tempfile.mkdtemp()) / "l"), \
                    mock.patch.object(dw, "enabled", return_value=True), mock.patch.object(dw.subprocess, "run", fake):
                guard.decide(bash(cmd), ARGV)
            self.assertTrue(fake.gh_calls(), cmd)

    def test_only_before_the_tool_and_unchanged_without_the_flag(self):
        with mock.patch.object(dw, "check", side_effect=AssertionError("called")):
            self.assertEqual(guard.decide(bash("git checkout -b feat/a", hook_event_name="PostToolUse"), ARGV), {})
            self.assertEqual(guard.decide(bash("git checkout -b feat/a"), []), {})

    def test_subprocess_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            root = fake_checkout(d)
            work = Path(d, "work")
            work.mkdir()
            env = isolated_child_env(PATH=f"{d}{os.pathsep}{os.environ['PATH']}", GIT_AUTHOR_NAME="t",
                                     GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

            def g(*a):
                subprocess.run(["git", "-C", str(work), *a], check=True, capture_output=True, env=env)
            subprocess.run(["git", "init", "-q", str(work)], check=True, env=env)
            g("remote", "add", "origin", "https://github.com/o/r.git")
            Path(work, "a.py").write_text("def g():\n    return 1\n")
            g("add", "a.py")
            g("commit", "-qm", "base")
            g("update-ref", "refs/remotes/origin/main", "HEAD")
            Path(work, "a.py").write_text("def g():\n    return 2\n")
            gh = Path(d, "gh")
            gh.write_text("#!/bin/sh\necho 'HTTP 502' >&2\nexit 1\n")
            gh.chmod(0o755)
            run = lambda cmd: subprocess.run([sys.executable, str(HOOK), "--dup-check", "--repo", str(root)],
                                             capture_output=True, text=True, env=env,
                                             input=json.dumps(bash(cmd, cwd=str(work))), timeout=60)
            out = json.loads(run("git checkout -b fix/x").stdout)["hookSpecificOutput"]
            self.assertNotIn("permissionDecision", out)
            self.assertIn("Duplicate-work check could not run (HTTP 502)", out["additionalContext"])
            self.assertEqual(run("ls").stdout, "")
            rows = Path(d, "ws", "logs", "dup-work.jsonl").read_text().splitlines()
            self.assertEqual([json.loads(r)["verdict"] for r in rows], ["UNKNOWN"])

    def test_switch_comes_from_sutando_config(self):
        sys.path.insert(0, str(REPO / "src"))
        import sutando_config
        with tempfile.TemporaryDirectory() as d:
            for i, (value, want) in enumerate((("on", True), ("off", False), ("OFF", False), ("typo", True))):
                root = fake_checkout(Path(d, str(i)), value)
                sutando_config._reset_cache_for_tests()
                self.assertEqual(dw.enabled(root), want, value)
        sutando_config._reset_cache_for_tests()

    def test_main_never_blocks_when_decide_raises(self):
        raw = json.dumps(bash("git checkout -b feat/a"))
        out = io.StringIO()
        with mock.patch.object(guard, "decide", side_effect=RuntimeError("boom")), \
                mock.patch.object(sys, "argv", ["hook", *ARGV]), mock.patch.object(sys, "stdin", io.StringIO(raw)), \
                contextlib.redirect_stdout(out):
            guard.main()
        o = json.loads(out.getvalue())["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", o)
        self.assertIn("could not run (RuntimeError)", o["additionalContext"])

    def test_summary_cli(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "s.jsonl")
            log.write_text("".join(json.dumps({"verdict": v, "trigger": t, "elapsed_ms": ms}) + "\n"
                                   for v, t, ms in (("OK", "branch", 300), ("FLAG", "pr-create", 400),
                                                    ("UNKNOWN", "branch", 500))))
            p = subprocess.run([sys.executable, str(REPO / "hooks" / "_dup_work.py"), "--summary", "--log", str(log)],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("verdicts FLAG 1  OK 1  UNKNOWN 1", p.stdout)
        self.assertIn("triggers branch 2  pr-create 1", p.stdout)
        self.assertIn("elapsed_ms p50 400.0  p95 500.0", p.stdout)


class Registration(unittest.TestCase):
    def _launcher_settings(self):
        script = ('REPO="$1"; . "$REPO/src/agent/claude/cli/session-launch.sh"; '
                  'resolve_claude_settings_args >/dev/null 2>&1; printf %s "${SETTINGS_ARGS[1]}"')
        out = subprocess.run(["/bin/bash", "-c", script, "x", str(REPO)], capture_output=True, text=True,
                             timeout=60, env=isolated_child_env()).stdout
        return json.loads(out)

    def test_the_launcher_registers_the_check_in_the_settings_it_builds(self):
        pre = self._launcher_settings()["hooks"]["PreToolUse"]
        cmds = [h["command"] for b in pre if b["matcher"] == "Bash" for h in b["hooks"]]
        mine = [c for c in cmds if "github-evidence-guard.py" in c]
        self.assertEqual(len(mine), 1, cmds)
        self.assertIn(f"--dup-check --repo {REPO}", mine[0].replace("'", ""))

    def test_the_builder_registers_it_on_bash_with_dup_check_and_its_checkout(self):
        builder = REPO / "src" / "agent" / "claude" / "cli" / "build-core-settings.mjs"
        hook = "/x/y z/hooks/github-evidence-guard.py"
        out = subprocess.run(["node", str(builder), "/x/hooks/skip-ask-user-question.py", "", "", "", "", "", "", "",
                              hook], capture_output=True, text=True, timeout=30, check=True).stdout
        pre = json.loads(out)["hooks"]["PreToolUse"]
        blk = [b for b in pre if any("github-evidence-guard" in h["command"] for h in b["hooks"])]
        self.assertEqual(len(blk), 1)
        self.assertEqual(blk[0]["matcher"], "Bash")
        words = subprocess.run(["/bin/bash", "-c", "printf '%s\\n' " + blk[0]["hooks"][0]["command"]],
                               capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(words, ["python3", hook, "--dup-check", "--repo", "/x/y z"])
        self.assertNotIn("PostToolUse", json.loads(out)["hooks"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
