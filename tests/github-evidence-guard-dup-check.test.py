#!/usr/bin/env python3
"""hooks/github-evidence-guard.py --dup-check: before a branch, `gh pr create` or `gh pr review`,
existing PRs that may cover the work are named as advisory context, never a denial; every other
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
RECENT = "2026-11-01T00:00:00Z"


def bash(cmd, **extra):
    return dict({"tool_name": "Bash", "hook_event_name": "PreToolUse", "tool_input": {"command": cmd},
                 "cwd": "/w"}, **extra)


def pr(number, title, branch, state="OPEN", files=(), closed=None):
    n = {"number": number, "title": title, "headRefName": branch, "state": state, "closedAt": closed,
         "url": f"https://github.com/o/r/pull/{number}"}
    if files:
        n["files"] = {"nodes": [{"path": f} for f in files]}
    return n


def diff(*hunks):
    """A unified diff: (file, enclosing-function) pairs."""
    return "".join(f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n@@ -1,2 +1,3 @@ {fn}\n+x\n" for f, fn in hunks)


class Fake:
    """Stands in for subprocess.run inside _dup_work: git answers locally, gh from fixtures."""

    def __init__(self, search=None, diffs=None, local=(), local_fns=(), branch="feat/x", fail=False,
                 origin="git@github.com:o/r.git"):
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
            if rest[:2] == ["diff", "-U0"]:
                return ok(diff(*self.local_fns))
            return subprocess.CompletedProcess(cmd, 1, "", "unknown git")
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 502: Bad Gateway")
        if cmd[1:3] == ["pr", "diff"]:
            return ok(self.diffs.get(int(cmd[3]), ""))
        self.query = cmd
        data = {a: {"issueCount": len(v), "nodes": v} for a, v in self.search.items() if a != "me"}
        if "me" in self.search:
            data["me"] = self.search["me"]
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


class Matching(Base):
    def test_ticket_id_match_is_flagged(self):
        fake = Fake({"tk": [pr(1808, "fix(settings): show a confirmation after Save", "fix/settings-save-feedback", "MERGED")]})
        text, r = self.run_hook("git checkout -b fix/rmaaaabbbbcccc-save-confirmation", fake)
        self.assertIn("Ticket-id hits", text)
        self.assertIn("#1808 [MERGED]", text)
        self.assertNotIn("permissionDecision", r["hookSpecificOutput"])
        self.assertEqual(self.rows()[0]["verdict"], "FLAG")
        self.assertEqual(self.rows()[0]["ticket"], [1808])

    def test_ticket_search_is_all_states_and_separate_from_keywords(self):
        fake = Fake({})
        self.run_hook("git switch -c fix/rmaaaabbbbcccc-save-confirmation", fake)
        q = dict(a.split("=", 1) for a in fake.query[4:] if "=" in a and not a.startswith("query="))
        self.assertEqual(q["tk"], "repo:o/r is:pr rmaaaabbbbcccc")
        self.assertNotIn("is:open", q["tk"])
        self.assertNotIn("closed:", q["tk"])
        self.assertIn("is:open", q["ko"])

    def test_ticket_hit_survives_many_keyword_hits(self):
        noise = [pr(100 + i, f"save confirmation tweak {i}", f"fix/save-confirmation-{i}") for i in range(20)]
        tk = [pr(1808, "fix(settings): show a confirmation after Save", "fix/settings-save-feedback", "MERGED")]
        tk += [pr(2000 + i, f"unrelated mention {i}", f"b{i}", "CLOSED") for i in range(29)]
        fake = Fake({"tk": tk, "ko": noise})
        text, _ = self.run_hook("git checkout -b fix/rmaaaabbbbcccc-save-confirmation", fake)
        lines = text.splitlines()
        self.assertEqual(lines[1], "Ticket-id hits (any state; every hit is listed):")
        self.assertIn("#1808", lines[2])
        self.assertEqual(sum("mentions the same ticket id" in x for x in lines), 30)
        self.assertLess(text.index("#1808"), text.index("Keyword overlap"))
        self.assertEqual(sum(" - keywords " in x for x in lines), dw.SHOWN)

    def test_keyword_match_is_flagged(self):
        fake = Fake({"ko": [pr(3877, "fix(gateway): flatten field values on the separator set", "fix/one-line-separator-parity")]})
        text, _ = self.run_hook("git checkout -b fix/one-line-folds-every-separator", fake)
        self.assertIn("Keyword overlap", text)
        self.assertIn("#3877", text)
        self.assertIn("keywords line, one, separator", text)
        self.assertIn("No ticket id in the branch name", text)

    def test_one_keyword_is_not_enough(self):
        fake = Fake({"ko": [pr(750, "fix(meter): say whether the meter fetch timed out", "fix/meter-transport-errors")]})
        text, _ = self.run_hook("git checkout -b fix/meter-request-failed", fake)
        self.assertEqual(text, "No ticket id in the branch name, so the duplicate check is weaker.")

    def test_function_overlap_is_flagged_and_file_only_is_a_count(self):
        f = "pkg/bridge.py"
        ro = [pr(3877, "flatten field values", "fix/a", files=[f]), pr(3952, "other bridge change", "fix/b", files=[f]),
              pr(3929, "third bridge change", "fix/c", files=[f])]
        fake = Fake({"ro": ro}, diffs={3877: diff((f, "def _one_line(value):")), 3952: diff((f, "def post():")),
                                       3929: diff((f, "class Bridge:"))},
                    local=[f], local_fns=[(f, "def _one_line(value):")], branch="fix/rmabcdefghij12-x")
        text, _ = self.run_hook("gh pr create --title 'fix: fold separators' --body x", fake)
        self.assertIn("Same function changed in an open PR:", text)
        self.assertIn("#3877", text)
        self.assertIn("`def _one_line(value):`", text)
        self.assertIn("(2 other open PR(s) touch the same files, no other signal.)", text)
        self.assertNotIn("#3952", text)
        self.assertEqual(self.rows()[0]["function"], [3877])
        self.assertEqual(self.rows()[0]["file_only"], [3952, 3929])

    def test_file_only_overlap_alone_prints_no_warning(self):
        f = "pkg/bridge.py"
        fake = Fake({"ro": [pr(3952, "other", "fix/b", files=[f])]}, diffs={3952: diff((f, "def post():"))},
                    local=[f], local_fns=[(f, "def _one_line():")], branch="fix/rmabcdefghij12-x")
        text, _ = self.run_hook("gh pr create -t 'fix: x' -b y", fake)
        self.assertEqual(text, "")
        self.assertEqual((self.rows()[0]["verdict"], self.rows()[0]["file_only"]), ("OK", [3952]))

    def test_no_match_prints_nothing(self):
        text, r = self.run_hook("git checkout -b fix/rmabcdefghij12-thing", Fake({"tk": [], "ko": [], "kc": []}))
        self.assertEqual((text, r), ("", {}))
        self.assertEqual(self.rows()[0]["verdict"], "OK")

    def test_current_pr_and_own_branch_are_excluded(self):
        f = "a.py"
        me = {"pullRequest": {"headRefName": "fix/mine", "files": {"nodes": [{"path": f}]}}}
        ro = [pr(3964, "this very PR", "fix/mine", files=[f]), pr(3965, "same branch elsewhere", "fix/mine", files=[f])]
        fake = Fake({"me": me, "ro": ro}, diffs={3964: diff((f, "def g():"))})
        text, _ = self.run_hook("gh pr review 3964 --repo o/r --approve -b ok", fake)
        self.assertEqual(text, "")
        fake = Fake({"tk": [pr(42, "my own PR", "fix/rmabcdefghij12-x")]}, branch="fix/rmabcdefghij12-x")
        text, _ = self.run_hook("gh pr create -t t -b b", fake)
        self.assertEqual(text, "")

    def test_closed_keyword_hits_older_than_30_days_are_dropped(self):
        old = pr(10, "separator one line fold", "fix/one-line", "CLOSED", closed="2020-01-01T00:00:00Z")
        new = pr(11, "separator one line fold", "fix/one-line-2", "CLOSED", closed="2099-01-01T00:00:00Z")
        text, _ = self.run_hook("git checkout -b fix/one-line-separator", Fake({"kc": [old, new]}))
        self.assertIn("#11", text)
        self.assertNotIn("#10 ", text)

    def test_gh_failure_is_unknown_and_one_line(self):
        text, r = self.run_hook("git checkout -b fix/rmabcdefghij12-thing", Fake(fail=True))
        self.assertEqual(len(text.splitlines()), 1)
        self.assertIn("could not run (HTTP 502: Bad Gateway)", text)
        self.assertNotIn("permissionDecision", r["hookSpecificOutput"])
        self.assertEqual(self.rows()[0]["verdict"], "UNKNOWN")

    def test_gh_timeout_is_unknown(self):
        def slow(cmd, **kw):
            if cmd[0] == "gh":
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return Fake()(cmd, **kw)
        text, _ = self.run_hook("git checkout -b fix/rmabcdefghij12-thing", slow)
        self.assertIn("did not answer within 5s", text)

    def test_no_github_origin_is_unknown(self):
        text, _ = self.run_hook("git checkout -b feat/a-b", Fake(origin="/local/bare.git"))
        self.assertIn("could not run (no GitHub repository", text)

    def test_heredoc_bodies_are_data_but_feed_ticket_ids(self):
        fake = Fake({})
        text, r = self.run_hook("cat > notes.txt <<'EOF'\ngit checkout -b fix/x-y\nEOF", fake)
        self.assertEqual((text, r, fake.calls), ("", {}, []))
        fake = Fake({}, branch="fix/rmabcdefghij12-x")
        self.run_hook("gh pr create -t 'fix: x' --body \"$(cat <<'EOF'\nRow rmzzzzzzzzzz99 and fixes #77\nEOF\n)\"", fake)
        q = [a for a in fake.query if a.startswith("tk=")][0]
        self.assertEqual(q, "tk=repo:o/r is:pr rmabcdefghij12 OR rmzzzzzzzzzz99 OR 77")

    def test_trigger_parsing(self):
        got = [(t["trigger"], t.get("branch") or t.get("pr"), t["cwd"]) for t in dw.triggers(
            "cd sub && git -C repo switch --create=feat/a-b; git worktree add -b feat/c ../wt; "
            "gh pr review https://github.com/o/r/pull/9 --comment -b hi; FOO=1 git checkout -B feat/d", "/w")]
        self.assertEqual(got, [("branch", "feat/a-b", "/w/sub/repo"), ("branch", "feat/c", "/w/sub"),
                               ("review", 9, "/w/sub"), ("branch", "feat/d", "/w/sub")])
        self.assertEqual(dw.triggers("git checkout main; git worktree add ../wt main; echo 'pr create'", "/w"), [])

    def test_one_search_per_branch_trigger(self):
        fake = Fake({})
        self.run_hook("git checkout -b fix/rmabcdefghij12-thing && git switch -c feat/other-thing", fake)
        self.assertEqual([c[:3] for c in fake.gh_calls()], [["gh", "api", "graphql"]] * 2)
        self.assertEqual([r["trigger"] for r in self.rows()], ["branch", "branch"])

    def test_off_switch_is_silent_and_logs_nothing(self):
        with mock.patch.object(dw, "enabled", return_value=False):
            fake = Fake({"tk": [pr(1, "x", "y")]})
            text, _ = self.run_hook("git checkout -b fix/rmabcdefghij12-thing", fake)
        self.assertEqual((text, fake.calls, self.rows()), ("", [], []))


class Edges(Base):
    """Parser corners and every failure path still end in one line or a clean pass, never a raise."""

    def test_parser_corners(self):
        with mock.patch.object(dw._shell_scan, "segments", side_effect=ValueError("unbalanced")):
            self.assertEqual(dw.triggers("git checkout -b feat/a", "/w"), [])
        self.assertEqual([t["branch"] for t in dw.triggers("FOO=1; git checkout -b feat/a", "/w")], ["feat/a"])
        self.assertEqual(dw.triggers("git push", "/w"), [])
        (t,) = dw.triggers("gh pr review -R o/r --approve 9", "/w")
        self.assertEqual((t["repo"], t["pr"]), ("o/r", 9))
        (t,) = dw.triggers("gh pr review --approve", "/w")
        self.assertIsNone(t["pr"])
        with tempfile.TemporaryDirectory() as d:
            Path(d, "body.md").write_text("Row rmqqqqrrrrssss here")
            (t,) = dw.triggers("gh pr create -t x -F body.md", d)
            self.assertIn("rmqqqqrrrrssss", t["body"])
            (t,) = dw.triggers("gh pr create -t x -F missing.md", d)
            self.assertEqual(t["body"].strip(), "")

    def test_review_without_a_number_is_unknown(self):
        text, _ = self.run_hook("gh pr review --approve -b ok", Fake({}))
        self.assertIn("could not run (the reviewed PR is not named", text)

    def test_review_of_a_missing_pr_is_unknown(self):
        text, _ = self.run_hook("gh pr review 5 -R o/r --approve", Fake({"ro": []}))
        self.assertIn("could not run (PR #5 not found in o/r)", text)

    def test_nothing_to_search_prints_only_the_weak_line(self):
        fake = Fake({})
        text, _ = self.run_hook("git checkout -b feat/a", fake)
        self.assertEqual(text, "No ticket id in the branch name, so the duplicate check is weaker.")
        self.assertEqual(fake.gh_calls(), [])

    def test_unreadable_local_diff_skips_file_overlap(self):
        def run(cmd, **kw):
            if cmd[:1] == ["git"] and "diff" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", "bad revision")
            return fake(cmd, **kw)
        fake = Fake({}, branch="fix/rmabcdefghij12-x")
        self.run_hook("gh pr create -t t -b b", run)
        self.assertNotIn("ro=", " ".join(fake.query))

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
            return Fake()(cmd, **kw)
        text, _ = self.run_hook("git checkout -b fix/rmabcdefghij12-x", no_gh)
        self.assertIn("could not run (gh could not run: no gh)", text)

    def test_unreadable_search_responses(self):
        for out in ("not json", json.dumps({"data": None}), json.dumps({"errors": []})):
            def run(cmd, out=out, **kw):
                if cmd[0] == "gh":
                    return subprocess.CompletedProcess(cmd, 0, out, "")
                return Fake()(cmd, **kw)
            text, _ = self.run_hook("git checkout -b fix/rmabcdefghij12-x", run)
            self.assertIn("could not run (unreadable search response)", text, out)

    def test_pr_diff_unavailable_or_out_of_time_is_no_function_signal(self):
        self.assertIsNone(dw.pr_diff(Fake(), "o/r", 5, 0.0))

        def broken(cmd, **kw):
            raise OSError("gone")
        self.assertIsNone(dw.pr_diff(broken, "o/r", 5, 2.0))
        fail = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "HTTP 404")
        self.assertIsNone(dw.pr_diff(fail, "o/r", 5, 2.0))

    def test_ticket_hits_beyond_the_first_page_name_the_total(self):
        nodes = [dict(pr(10 + i, f"t{i}", f"b{i}"), branch=f"b{i}") for i in range(3)]
        found = {"ticket": nodes, "function": [], "keyword": [], "file_only": [], "ticket_total": 140}
        text = dw.advice({"trigger": "branch", "branch": "x"}, "o/r", found, "repo:o/r is:pr rmabcdefghij12")
        self.assertIn("... 140 PRs in total: gh search prs 'repo:o/r is:pr rmabcdefghij12'", text)

    def test_no_repo_root_says_so(self):
        self.assertIn("repo root not configured", dw.check("git checkout -b feat/a", "/w", None))

    def test_unwritable_or_unresolvable_log_still_answers(self):
        with mock.patch.object(dw, "log_path", side_effect=RuntimeError("no workspace")):
            text, _ = self.run_hook("git checkout -b fix/rmabcdefghij12-x", Fake(fail=True))
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
    """The recorded incidents, as GitHub answered at the time (fixtures from the real PRs)."""

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
        text, _ = self.run_hook("gh pr review 3964 -R sonichi/sutando --approve",
                                Fake({"me": me, "ro": ro}, diffs=diffs))
        self.assertIn("Same function changed in an open PR:\n  #3877", text)
        self.assertIn("other open PR(s) touch the same files", text)

    def test_branch_for_3964_finds_3877_by_keywords(self):
        ko = [pr(3877, "fix(gateway): flatten field values on the separator set readers actually split on",
                 "fix/one-line-separator-parity")]
        text, _ = self.run_hook("git checkout -b fix/one-line-folds-every-separator", Fake({"ko": ko}))
        self.assertIn("#3877", text)

    def test_web_client_1808_by_ticket_and_by_title_keywords(self):
        n = pr(1808, "fix(settings): show a confirmation after Save",
               "fix/settings-save-feedback", "MERGED", closed=RECENT)
        text, _ = self.run_hook("git checkout -b fix/rmaaaabbbbcccc-save-confirmation", Fake({"tk": [n]}))
        self.assertIn("#1808", text)
        text, _ = self.run_hook("git checkout -b fix/save-confirmation", Fake({"kc": [n]}))
        self.assertIn("#1808", text)

    def test_desktop_app_750_caught_by_ticket_missed_by_title_with_a_weak_line(self):
        n = pr(750, "fix(meter): say whether the meter fetch timed out",
               "fix/meter-transport-errors", "MERGED", closed=RECENT)
        text, _ = self.run_hook("git checkout -b fix/rmddddeeeeffff-meter-request-failed", Fake({"tk": [n]}))
        self.assertIn("#750", text)
        text, _ = self.run_hook("git checkout -b fix/meter-request-failed", Fake({"kc": [n]}))
        self.assertEqual(text, "No ticket id in the branch name, so the duplicate check is weaker.")


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
        fake = Fake({})
        with mock.patch.object(dw, "log_path", return_value=d / "l.jsonl"), \
                mock.patch.object(dw, "enabled", return_value=True), mock.patch.object(dw.subprocess, "run", fake):
            start = time.perf_counter()
            guard.decide(bash("git checkout -b fix/rmabcdefghij12-thing"), ARGV)
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

    def test_only_before_the_tool_and_unchanged_without_the_flag(self):
        with mock.patch.object(dw, "check", side_effect=AssertionError("called")):
            self.assertEqual(guard.decide(bash("git checkout -b feat/a", hook_event_name="PostToolUse"), ARGV), {})
            self.assertEqual(guard.decide(bash("git checkout -b feat/a"), []), {})

    def test_subprocess_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            root = fake_checkout(d)
            work = Path(d, "work")
            work.mkdir()
            subprocess.run(["git", "init", "-q", str(work)], check=True)
            subprocess.run(["git", "-C", str(work), "remote", "add", "origin", "https://github.com/o/r.git"], check=True)
            gh = Path(d, "gh")
            gh.write_text("#!/bin/sh\necho 'HTTP 502' >&2\nexit 1\n")
            gh.chmod(0o755)
            env = isolated_child_env(PATH=f"{d}{os.pathsep}{os.environ['PATH']}")
            run = lambda cmd: subprocess.run([sys.executable, str(HOOK), "--dup-check", "--repo", str(root)],
                                             capture_output=True, text=True, env=env,
                                             input=json.dumps(bash(cmd, cwd=str(work))), timeout=60)
            out = json.loads(run("git checkout -b fix/rmabcdefghij12-x").stdout)["hookSpecificOutput"]
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
    def test_the_launcher_registers_the_check(self):
        launch = (REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh").read_text()
        self.assertIn('"$REPO/hooks/stale-head-post-guard.py" "$REPO/hooks/github-evidence-guard.py"', launch)

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
