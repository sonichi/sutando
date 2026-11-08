#!/usr/bin/env python3
"""hooks/github-evidence-guard.py --merge-gate: a merge command is denied on HOLD or UNKNOWN,
allowed on CLEAR, and every other command passes with no gh call and no file read; the verdict
comes from hooks/_merge_hold.py, and the core launcher registers the gate. No network.

Run:  python3 tests/github-evidence-guard-merge-gate.test.py
"""
import builtins
import importlib.util
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
import _merge_hold as mh  # noqa: E402

_spec = importlib.util.spec_from_file_location("github_evidence_guard_merge_gate", HOOK)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

ARGV = ["--merge-gate", "--repo", str(REPO)]
NOW = mh._ts("2026-11-07T12:30:00Z")


def bash(cmd, **extra):
    return dict({"tool_name": "Bash", "hook_event_name": "PreToolUse", "tool_input": {"command": cmd}}, **extra)


def pull(comments=(), number=5, repo="o/r"):
    return {"number": number, "repository": {"nameWithOwner": repo}, "author": {"__typename": "User", "login": "pat"},
            "commits": {"nodes": [{"commit": {"committedDate": "2026-11-07T10:00:00Z"}}]},
            "reviewRequests": {"nodes": [{"requestedReviewer": {"login": "alice"}}]},
            "reviews": {"pageInfo": {"hasPreviousPage": False}, "nodes": []},
            "comments": {"pageInfo": {"hasPreviousPage": False}, "nodes": list(comments)}}


HELD = [{"author": {"__typename": "User", "login": "alice"}, "authorAssociation": "NONE",
         "createdAt": "2026-11-07T11:00:00Z", "body": "re-reading the migration",
         "url": "https://github.com/o/r/pull/5#issuecomment-1"}]


class Gh:
    """Stands in for subprocess.run inside _merge_hold; counts invocations."""

    def __init__(self, comments=(), fail=False, by_node=None):
        self.comments, self.fail, self.by_node, self.calls = comments, fail, by_node or {}, []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        if self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 502")
        node = next((a[3:] for a in cmd if a.startswith("id=")), None)
        if node is not None:
            if node not in self.by_node:
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"data": {"node": {}}}), "")
            repo, number = self.by_node[node]
            return subprocess.CompletedProcess(cmd, 0, json.dumps(
                {"data": {"node": pull(self.comments, number, repo)}}), "")
        number = int(next(a[3:] for a in cmd if a.startswith("pr=")))
        return subprocess.CompletedProcess(cmd, 0, json.dumps(
            {"data": {"repository": {"pullRequest": pull(self.comments, number)}}}), "")


def fake_checkout(d, mode):
    """A checkout whose config pins a temp workspace and the gate mode, so a subprocess stays hermetic."""
    root = Path(d, "checkout")
    (root / "src").mkdir(parents=True)
    for name in ("sutando_config.py", "workspace_default.py"):
        (root / "src" / name).write_text((REPO / "src" / name).read_text())
    (root / "sutando.config.json").write_text(json.dumps({"workspace": {"path": str(Path(d, "ws"))},
                                                          "env": {"MERGE_HOLD_MODE": mode}}))
    return root


class Gate(unittest.TestCase):
    """Enforcing mode."""
    MODE = mh.ENFORCE

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.log = self.d / "guards" / "merge_hold_overrides.jsonl"
        self.shadow = self.d / "logs" / "merge-hold-shadow.jsonl"
        p = mock.patch.object(mh, "gate_mode", return_value=self.MODE)
        p.start()
        self.addCleanup(p.stop)
        for name, value in (("state_path", self.d / "merge-holds.json"), ("override_log_path", self.log),
                            ("shadow_log_path", self.shadow)):
            p = mock.patch.object(mh, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch.object(mh, "ttl_hours", return_value=4.0),
                  mock.patch.object(mh, "_now", return_value="2026-11-07T12:30:00Z"),
                  mock.patch.object(mh.dt, "datetime", wraps=mh.dt.datetime)):
            p.start()
            self.addCleanup(p.stop)
        mh.dt.datetime.now = lambda tz=None: NOW

    def run_gate(self, cmd, gh, **extra):
        with mock.patch.object(mh.subprocess, "run", gh):
            return guard.decide(bash(cmd, **extra), ARGV)

    def verdict(self, result):
        return (result.get("hookSpecificOutput") or {}).get("permissionDecision", "allow")

    def test_held_pr_merge_is_denied_naming_reviewer_url_and_expiry(self):
        r = self.run_gate("gh pr merge 5 --repo o/r --squash", Gh(HELD))
        self.assertEqual(self.verdict(r), "deny")
        reason = r["hookSpecificOutput"]["permissionDecisionReason"]
        for part in ("alice", "https://github.com/o/r/pull/5#issuecomment-1", "expires 2026-11-07T15:00:00Z",
                     "SUTANDO_MERGE_HOLD_OVERRIDE"):
            self.assertIn(part, reason)

    def test_clear_pr_merge_is_allowed(self):
        self.assertEqual(self.run_gate("gh pr merge -R o/r 5 --squash --match-head-commit abc", Gh()), {})
        self.assertEqual(self.run_gate("gh pr merge https://github.com/o/r/pull/5 --merge", Gh()), {})

    def test_enqueue_graphql_is_parsed_and_resolved(self):
        gh = Gh(HELD, by_node={"PR_kwDOabc": ("o/r", 9)})
        cmd = ("gh api graphql -f query='mutation { enqueuePullRequest(input: {pullRequestId: \"PR_kwDOabc\"}) "
               "{ mergeQueueEntry { id } } }'")
        r = self.run_gate(cmd, gh)
        self.assertEqual(self.verdict(r), "deny")
        self.assertIn("o/r#9", r["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(len(gh.calls), 1)

    def test_merge_mutation_with_variable_id_and_file_bodies(self):
        cmd = ("gh api graphql -F pr=PR_x -f query='mutation($pr: ID!) { mergePullRequest(input: "
               "{pullRequestId: $pr}) { clientMutationId } }'")
        self.assertEqual(self.run_gate(cmd, Gh(by_node={"PR_x": ("o/r", 3)})), {})
        Path(self.d, "body.json").write_text(json.dumps({
            "query": "mutation($id: ID!) { enqueuePullRequest(input: {pullRequestId: $id}) { x } }",
            "variables": {"id": "PR_f"}}))
        Path(self.d, "q.graphql").write_text('mutation { mergePullRequest(input: {pullRequestId: "PR_f"}) { x } }')
        Path(self.d, "read.graphql").write_text("query { viewer { login } }")
        for cmd in ("gh api graphql --input body.json", "gh api graphql -F query=@q.graphql"):
            r = self.run_gate(cmd, Gh(HELD, by_node={"PR_f": ("o/r", 11)}), cwd=str(self.d))
            self.assertEqual(self.verdict(r), "deny", cmd)
        self.assertEqual(self.run_gate("gh api graphql -F query=@read.graphql", Gh(HELD), cwd=str(self.d)), {})

    def test_unresolvable_targets_are_denied(self):
        for cmd in ("gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: \"I_no\"}) { x } }'",
                    "gh api graphql --input /nonexistent/body.json",
                    "gh api graphql -f query='mutation { mergePullRequest(input: {pullRequestId: $ID}) { x } }'",
                    "gh pr merge 5 --squash", "gh pr merge --squash", "gh pr merge 5 -R o/r 'unterminated"):
            self.assertEqual(self.verdict(self.run_gate(cmd, Gh())), "deny", cmd)

    def test_rest_merge_and_wrapped_merges_are_gated(self):
        for cmd in ("gh api -X PUT repos/o/r/pulls/5/merge", "bash -c 'gh pr merge 5 -R o/r'",
                    "echo hi && gh pr merge 5 -R o/r"):
            self.assertEqual(self.verdict(self.run_gate(cmd, Gh(HELD))), "deny", cmd)

    def test_unknown_is_denied_with_the_override_hint(self):
        r = self.run_gate("gh pr merge 5 --repo o/r", Gh(fail=True))
        self.assertEqual(self.verdict(r), "deny")
        reason = r["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("cannot verify merge holds", reason)
        self.assertIn("SUTANDO_MERGE_HOLD_OVERRIDE", reason)

    def test_timeout_is_unknown(self):
        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        r = self.run_gate("gh pr merge 5 --repo o/r", slow)
        self.assertIn("within 5s", r["hookSpecificOutput"]["permissionDecisionReason"])

    def test_override_on_unknown_or_hold_allows_and_is_logged(self):
        for gh, past in ((Gh(fail=True), "an unverifiable check"), (Gh(HELD), "a live hold")):
            r = self.run_gate("SUTANDO_MERGE_HOLD_OVERRIDE='GitHub outage, owner approved' gh pr merge 5 -R o/r", gh)
            self.assertEqual(self.verdict(r), "allow")
            self.assertIn(past, r["hookSpecificOutput"]["additionalContext"])
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([r["reason"] for r in rows], ["GitHub outage, owner approved"] * 2)
        self.assertEqual(rows[0]["targets"], [{"repo": "o/r", "pr": 5, "node": None}])
        self.assertIn("alice", rows[1]["holds"][0])

    def test_short_or_empty_override_is_refused(self):
        for cmd in ("SUTANDO_MERGE_HOLD_OVERRIDE=1 gh pr merge 5 -R o/r", "SUTANDO_MERGE_HOLD_OVERRIDE= gh pr merge 5 -R o/r"):
            self.assertEqual(self.verdict(self.run_gate(cmd, Gh(HELD))), "deny", cmd)
        self.assertFalse(self.log.exists())

    def test_override_that_cannot_be_logged_is_refused(self):
        with mock.patch.object(mh, "record_override", side_effect=OSError("read-only")):
            r = self.run_gate("SUTANDO_MERGE_HOLD_OVERRIDE='GitHub outage' gh pr merge 5 -R o/r", Gh(fail=True))
        self.assertIn("override not recorded", r["hookSpecificOutput"]["permissionDecisionReason"])

    def test_expired_hold_is_an_advisory_not_a_denial(self):
        mh.dt.datetime.now = lambda tz=None: mh._ts("2026-11-07T20:00:00Z")
        r = self.run_gate("gh pr merge 5 -R o/r", Gh(HELD))
        self.assertEqual(self.verdict(r), "allow")
        self.assertIn("EXPIRED hold by alice announced 2026-11-07T11:00:00Z (9h00m old); re-ask them",
                      r["hookSpecificOutput"]["additionalContext"])

    def test_non_merge_commands_pass_through(self):
        gh = Gh(HELD)
        for cmd in ("gh pr view 5 --repo o/r", "gh pr merge 5 -R o/r --disable-auto", "ls -la",
                    "git commit -m 'gate gh pr merge on holds'", "grep -n 'pr merge' hooks/README.md",
                    "git commit -F - <<'EOF'\ngh pr merge 5 --repo o/r\nEOF",
                    "gh api repos/o/r/pulls/4/merge", "gh api graphql -f query='query { viewer { login } }'"):
            self.assertEqual(self.run_gate(cmd, gh), {}, cmd)
        self.assertEqual(gh.calls, [])
        self.assertEqual(guard.decide({"tool_name": "Read", "tool_input": {"file_path": "x"}}, ARGV), {})

    def test_the_gate_only_runs_before_the_tool(self):
        gh = Gh(HELD)
        with mock.patch.object(mh.subprocess, "run", gh), mock.patch.object(guard, "decision_context", return_value=""):
            r = guard.decide(bash("gh pr merge 5 -R o/r", hook_event_name="PostToolUse"), ARGV)
        self.assertEqual((r, gh.calls), ({}, []))

    def test_without_merge_gate_the_hook_is_unchanged(self):
        gh = Gh(HELD)
        with mock.patch.object(mh.subprocess, "run", gh):
            self.assertEqual(guard.decide(bash("gh pr merge 5 -R o/r")), {})
        self.assertEqual(gh.calls, [])


class Shadow(Gate):
    """The trial default: every merge decision is logged, none is denied."""
    MODE = mh.SHADOW

    def rows(self):
        return [json.loads(line) for line in self.shadow.read_text().splitlines()]

    def test_shadow_never_denies_and_always_logs(self):
        cases = [("gh pr merge 5 --repo o/r", Gh(HELD), "HOLD", "alice", "pr-merge"),
                 ("gh pr merge 5 --repo o/r", Gh(), "CLEAR", "", "pr-merge"),
                 ("gh pr merge 5 --repo o/r", Gh(fail=True), "UNKNOWN", "", "pr-merge"),
                 ("gh pr merge 5 --squash", Gh(), "UNKNOWN", "", "pr-merge"),
                 ("gh pr merge 5 -R o/r 'unterminated", Gh(), "UNKNOWN", "", "unparsed"),
                 ("gh api -X PUT repos/o/r/pulls/5/merge", Gh(HELD), "HOLD", "alice", "rest-merge"),
                 ("gh api graphql -f query='mutation { enqueuePullRequest(input: {pullRequestId: \"PR_a\"}) { x } }'",
                  Gh(HELD, by_node={"PR_a": ("o/r", 5)}), "HOLD", "alice", "graphql-enqueuePullRequest")]
        for cmd, gh, *_ in cases:
            self.assertEqual(self.run_gate(cmd, gh), {}, cmd)
        rows = self.rows()
        self.assertEqual([(r["verdict"], r["holder"], r["kind"]) for r in rows], [c[2:] for c in cases])
        for r in rows:
            self.assertEqual(set(r) >= {"ts", "repo", "pr", "verdict", "holder", "elapsed_ms", "kind"}, True)
            self.assertEqual(r["mode"], "shadow")
        self.assertEqual((rows[0]["repo"], rows[0]["pr"]), ("o/r", 5))
        self.assertFalse(self.log.exists())

    def test_shadow_logs_expired_as_its_own_verdict(self):
        mh.dt.datetime.now = lambda tz=None: mh._ts("2026-11-07T20:00:00Z")
        self.assertEqual(self.run_gate("gh pr merge 5 -R o/r", Gh(HELD)), {})
        self.assertEqual((self.rows()[0]["verdict"], self.rows()[0]["holder"]), ("EXPIRED", "alice"))

    def test_shadow_survives_an_unwritable_log(self):
        with mock.patch.object(mh, "_log_rows", side_effect=OSError("disk full")):
            self.assertEqual(self.run_gate("gh pr merge 5 -R o/r", Gh(HELD)), {})

    def test_summary_counts_verdicts_and_percentiles(self):
        for gh in (Gh(HELD), Gh(), Gh(), Gh(fail=True)):
            self.run_gate("gh pr merge 5 -R o/r", gh)
        out = mh.summarize(self.shadow)
        self.assertIn("4 merge-command decision(s)", out)
        for line in ("HOLD     1", "CLEAR    2", "UNKNOWN  1", "elapsed_ms p50 "):
            self.assertIn(line, out)
        self.assertIn("p50 n/a", mh.summarize(self.d / "missing.jsonl"))


for _name in [n for n in dir(Gate) if n.startswith("test_")]:
    setattr(Shadow, _name, None)


class Cost(unittest.TestCase):
    """Only a merge pays: no subprocess and no file read otherwise; one gh call for a merge."""

    def test_non_merge_makes_no_subprocess_call_and_reads_no_file(self):
        opened = []
        real_open = builtins.open
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("gh called")) as run, \
                mock.patch.object(builtins, "open", side_effect=lambda *a, **k: opened.append(a) or real_open(*a, **k)):
            start = time.perf_counter()
            for _ in range(2000):
                for cmd in ("ls -la", "git status", "gh pr view 5 --repo o/r --json state"):
                    self.assertEqual(guard.decide(bash(cmd), ARGV), {})
            per_call_us = (time.perf_counter() - start) / 6000 * 1e6
        self.assertEqual((run.call_count, opened), (0, []))
        print(f"\n  non-merge pass-through: {per_call_us:.1f} us/command, 0 subprocess, 0 file reads", file=sys.stderr)

    def test_merge_makes_exactly_one_gh_call(self):
        gh, d = Gh(), Path(tempfile.mkdtemp())
        with mock.patch.object(mh.subprocess, "run", gh), \
                mock.patch.object(mh, "state_path", return_value=d / "none.json"), \
                mock.patch.object(mh, "shadow_log_path", return_value=d / "shadow.jsonl"), \
                mock.patch.object(mh, "gate_mode", return_value=mh.ENFORCE), \
                mock.patch.object(mh, "ttl_hours", return_value=4.0):
            start = time.perf_counter()
            self.assertEqual(guard.decide(bash("gh pr merge 5 --repo o/r --squash"), ARGV), {})
            ms = (time.perf_counter() - start) * 1e3
        self.assertEqual(len(gh.calls), 1)
        self.assertEqual(gh.calls[0][:3], ["gh", "api", "graphql"])
        print(f"\n  merge path, stubbed gh: {len(gh.calls)} gh invocation, {ms:.2f} ms excluding the network",
              file=sys.stderr)


class Delegation(unittest.TestCase):
    def test_the_hook_delegates_to_the_helper_gate(self):
        seen = []

        def fake_gate(command, cwd, repo_root, gh=None, now=None):
            seen.append((command, cwd, repo_root))
            return "deny", "stubbed verdict"

        with mock.patch.object(mh, "gate", fake_gate):
            r = guard.decide(bash("gh pr merge 8 -R o/r", cwd="/w"), ARGV)
        self.assertEqual(seen, [("gh pr merge 8 -R o/r", "/w", str(REPO))])
        self.assertEqual(r["hookSpecificOutput"]["permissionDecisionReason"], "stubbed verdict")

    def test_subprocess_end_to_end_with_failing_gh(self):
        for mode, want in (("enforce", "deny"), ("shadow", None)):
            with tempfile.TemporaryDirectory() as d:
                root = fake_checkout(d, mode)
                gh = Path(d, "gh")
                gh.write_text("#!/bin/sh\necho 'HTTP 502' >&2\nexit 1\n")
                gh.chmod(0o755)
                env = dict(os.environ, PATH=f"{d}{os.pathsep}{os.environ['PATH']}")
                run = lambda cmd: subprocess.run([sys.executable, str(HOOK), "--merge-gate", "--repo", str(root)],
                                                 capture_output=True, text=True, env=env,
                                                 input=json.dumps(bash(cmd)), timeout=60)
                out = run("gh pr merge 5 -R o/r").stdout
                got = json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out else None
                self.assertEqual(got, want, mode)
                self.assertEqual(run("ls").stdout, "")
                rows = Path(d, "ws", "logs", "merge-hold-shadow.jsonl").read_text().splitlines()
                self.assertEqual([json.loads(r)["verdict"] for r in rows], ["UNKNOWN"], mode)

    def test_mode_comes_from_sutando_config_and_a_typo_enforces(self):
        import sutando_config
        with tempfile.TemporaryDirectory() as d:
            for value, want in (("shadow", mh.SHADOW), ("enforce", mh.ENFORCE), ("shadwo", mh.ENFORCE)):
                root = fake_checkout(Path(d, value), value)
                sutando_config._reset_cache_for_tests()
                self.assertEqual(mh.gate_mode(root), want, value)
            root = fake_checkout(Path(d, "unset"), "x")
            (root / "sutando.config.json").write_text("{}")
            sutando_config._reset_cache_for_tests()
            self.assertEqual(mh.gate_mode(root), mh.SHADOW)
        sutando_config._reset_cache_for_tests()

    def test_no_repo_root_is_denied_for_a_merge(self):
        with mock.patch.dict(os.environ, {"SUTANDO_REPO_ROOT": ""}):
            r = guard.decide(bash("gh pr merge 5 -R o/r"), ["--merge-gate"])
        self.assertIn("repo root not configured", r["hookSpecificOutput"]["permissionDecisionReason"])

    def test_summary_cli(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d, "s.jsonl")
            log.write_text("".join(json.dumps({"verdict": v, "elapsed_ms": ms}) + "\n"
                                   for v, ms in (("CLEAR", 300), ("HOLD", 400), ("CLEAR", 500))))
            p = subprocess.run([sys.executable, str(REPO / "hooks" / "_merge_hold.py"), "--summary", "--log", str(log)],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0)
        self.assertIn("elapsed_ms p50 400.0  p95 500.0", p.stdout)

    def test_malformed_event_fails_closed_only_for_a_merge(self):
        run = lambda raw: subprocess.run([sys.executable, str(HOOK), *ARGV], capture_output=True, text=True,
                                         input=raw, timeout=60)
        self.assertEqual(run("{not json").stdout, "")
        self.assertIn('"deny"', run('{"tool_input": "gh pr merge 5"').stdout)


class Registration(unittest.TestCase):
    def test_the_launcher_registers_the_gate(self):
        launch = (REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh").read_text()
        self.assertIn('"$REPO/hooks/stale-head-post-guard.py" "$REPO/hooks/github-evidence-guard.py"', launch)

    def test_the_builder_registers_it_on_bash_with_merge_gate_and_its_checkout(self):
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
        self.assertEqual(words, ["python3", hook, "--merge-gate", "--repo", "/x/y z"])
        self.assertNotIn("PostToolUse", json.loads(out)["hooks"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
