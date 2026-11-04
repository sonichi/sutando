#!/usr/bin/env python3
"""hooks/stale-head-post-guard.py: a PR review or comment whose commit_id or sha claim
names a head the PR has moved past is blocked; everything else is allowed; the core
launcher registers the hook. No test touches the network: the head comes from a fake.

Run:  python3 tests/stale-head-post-guard.test.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "stale-head-post-guard.py"
PYBASE = [sys.executable]
if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") == "1":
    PYBASE += ["-m", "coverage", "run", f"--rcfile={REPO / '.coveragerc'}"]

_spec = importlib.util.spec_from_file_location("stale_head_post_guard", HOOK)
shg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shg)

HEAD = "c0ffee1234567890abcdef1234567890abcdef12"
OLD = "deadbe0987654321fedcba0987654321fedcba09"
REVIEWS = "repos/o/r/pulls/7/reviews"


def payload(cmd, cwd=None):
    data = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    if cwd:
        data["cwd"] = cwd
    return json.dumps(data)


class Decide(unittest.TestCase):
    def setUp(self):
        self.fetched = []

    def fetch(self, repo, number):
        self.fetched.append((repo, number))
        return HEAD

    def decide(self, cmd, cwd=None, fetch=None):
        return shg.decide(payload(cmd, cwd), fetch or self.fetch)

    def assertBlocked(self, cmd, cwd=None):
        code, _, err = self.decide(cmd, cwd)
        self.assertEqual(code, 2, cmd)
        self.assertIn(HEAD, err)
        self.assertIn("re-read the PR at the new head", err)
        return err

    def assertAllowed(self, cmd, cwd=None):
        code, out, err = self.decide(cmd, cwd)
        self.assertEqual((code, out, err), (0, "", ""), cmd)

    def test_review_post_with_matching_commit_id(self):
        self.assertAllowed("gh api -X POST %s -f commit_id=%s -f event=APPROVE" % (REVIEWS, HEAD))
        self.assertAllowed("gh api %s -f commit_id=%s -f body=ok" % (REVIEWS, HEAD[:12]))

    def test_review_post_with_moved_commit_id(self):
        err = self.assertBlocked("gh api -X POST %s -f commit_id=%s -f event=APPROVE" % (REVIEWS, OLD))
        self.assertIn(OLD, err)
        self.assertIn("o/r#7", err)
        self.assertBlocked("gh api --method=POST /%s -F commit_id=%s" % (REVIEWS, OLD))
        self.assertBlocked("gh api %s --raw-field commit_id=%s" % (REVIEWS, OLD))  # POST implied
        self.assertBlocked("cd /x && GH_HOST=github.com gh api -XPOST %s -fcommit_id=%s" % (REVIEWS, OLD))

    def test_input_json_file(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "rev.json").write_text(json.dumps({"commit_id": OLD, "event": "COMMENT", "body": "x"}))
            self.assertBlocked("gh api %s --input rev.json" % REVIEWS, cwd=d)
            self.assertBlocked("gh api %s --input %s/rev.json" % (REVIEWS, d))
            Path(d, "ok.json").write_text(json.dumps({"commit_id": HEAD, "body": "Reviewed at %s" % HEAD[:7]}))
            self.assertAllowed("gh api %s --input ok.json" % REVIEWS, cwd=d)
            self.assertAllowed("gh api %s --input missing.json" % REVIEWS, cwd=d)

    def test_input_from_a_heredoc(self):
        body = json.dumps({"commit_id": OLD, "event": "APPROVE"})
        self.assertBlocked("gh api %s --input - <<'EOF'\n%s\nEOF" % (REVIEWS, body))
        ok = json.dumps({"commit_id": HEAD, "event": "APPROVE"})
        self.assertAllowed("gh api %s --input - <<'EOF'\n%s\nEOF" % (REVIEWS, ok))

    def test_api_body_claim(self):
        self.assertBlocked("gh api %s -f event=COMMENT -f body='LGTM. Reviewed at %s.'" % (REVIEWS, OLD[:7]))
        self.assertAllowed("gh api %s -f event=COMMENT -f body='LGTM. Reviewed at %s.'" % (REVIEWS, HEAD[:7]))

    def test_get_and_other_endpoints_untouched(self):
        self.assertAllowed("gh api %s" % REVIEWS)
        self.assertAllowed("gh api -X GET %s -f per_page=100 -f commit_id=%s" % (REVIEWS, OLD))
        self.assertAllowed("gh api -X POST repos/o/r/issues/7/comments -f body='at %s'" % OLD)
        self.assertAllowed("gh api repos/{owner}/{repo}/pulls/7/reviews -f commit_id=%s" % OLD)
        self.assertEqual(self.fetched, [])

    def test_pr_review_body_claim(self):
        self.assertBlocked("gh pr review 7 --repo o/r --approve --body 'Reviewed at %s'" % OLD[:10])
        self.assertBlocked("gh pr review 7 -R o/r -c -b 'checked head %s'" % OLD[:7])
        self.assertBlocked("gh pr review https://github.com/o/r/pull/7 --approve --body='ok @ %s'" % OLD[:8])
        self.assertAllowed("gh pr review 7 --repo o/r --approve --body 'Reviewed at %s'" % HEAD[:10])
        self.assertAllowed("gh pr review 7 --repo o/r --approve --body 'Reviewed at `%s`'" % HEAD)

    def test_pr_comment_body_claim(self):
        self.assertBlocked("gh pr comment 7 --repo o/r --body 'Verified at %s'" % OLD[:7])
        self.assertAllowed("gh pr comment 7 --repo o/r --body 'Verified at %s'" % HEAD[:7])

    def test_body_file(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "b.md").write_text("Reviewed at %s\n" % OLD[:7])
            self.assertBlocked("gh pr comment 7 --repo o/r --body-file b.md", cwd=d)
            self.assertBlocked("gh pr review 7 --repo o/r --approve -F %s/b.md" % d)
        written = "cat > /tmp/nowhere-xyz/b.md <<'EOF'\nReviewed at %s\nEOF\n" % OLD[:7]
        self.assertBlocked(written + "gh pr comment 7 --repo o/r --body-file /tmp/nowhere-xyz/b.md")
        self.assertBlocked("gh pr comment 7 --repo o/r --body-file - <<'EOF'\nat %s\nEOF" % OLD[:7])
        self.assertBlocked("gh pr comment 7 --repo o/r --body \"$(cat <<'EOF'\nat %s\nEOF\n)\"" % OLD[:7])

    def test_body_naming_old_and_current_sha_is_allowed(self):
        body = "Re-reviewed at %s; the finding from %s is fixed." % (HEAD[:7], OLD[:7])
        self.assertAllowed("gh pr comment 7 --repo o/r --body '%s'" % body)
        self.assertAllowed("gh pr review 7 --repo o/r --approve --body 'was at %s, now head %s'"
                           % (OLD[:7], HEAD[:9]))

    def test_moved_commit_id_is_blocked_even_when_body_names_the_head(self):
        self.assertBlocked("gh api %s -f commit_id=%s -f body='at %s'" % (REVIEWS, OLD, HEAD[:7]))

    def test_no_repo_allows_without_fetching(self):
        self.assertAllowed("gh pr review 7 --approve --body 'Reviewed at %s'" % OLD[:7])
        self.assertAllowed("gh pr comment --body 'at %s'" % OLD[:7])
        self.assertEqual(self.fetched, [])

    def test_nothing_to_check_allows_without_fetching(self):
        self.assertAllowed("gh pr review 7 --repo o/r --approve --body 'LGTM'")
        self.assertAllowed("gh api %s -f event=APPROVE -f body='looks good at 1234567 rows'" % REVIEWS)
        self.assertAllowed("gh api %s -f commit_id=\"$(gh pr view 7 --json headRefOid -q .headRefOid)\""
                           % REVIEWS)
        self.assertAllowed("gh api %s -f commit_id=$SHA" % REVIEWS)
        self.assertEqual(self.fetched, [])

    def test_fetch_failure_allows_with_a_warning(self):
        code, out, err = self.decide("gh pr comment 7 --repo o/r --body 'at %s'" % OLD[:7],
                                     fetch=lambda r, n: None)
        self.assertEqual((code, out), (0, ""))
        self.assertIn("could not read the head of o/r#7", err)
        self.assertEqual(err.count("\n"), 1)

    def test_escape_hatch(self):
        self.assertAllowed("SUTANDO_ALLOW_STALE_POST=1 gh api %s -f commit_id=%s" % (REVIEWS, OLD))
        self.assertAllowed("export SUTANDO_ALLOW_STALE_POST=1; gh pr comment 7 -R o/r --body 'at %s'"
                           % OLD[:7])
        self.assertBlocked("gh pr comment 7 -R o/r --body 'SUTANDO_ALLOW_STALE_POST=1 at %s'" % OLD[:7])

    def test_non_pr_gh_commands_untouched(self):
        for cmd in ("gh pr view 7 --repo o/r --json body -q 'at %s'" % OLD[:7],
                    "gh pr list --repo o/r --search 'head %s'" % OLD[:7],
                    "gh issue comment 7 --repo o/r --body 'at %s'" % OLD[:7],
                    "git log --oneline | grep 'at %s'" % OLD[:7],
                    "echo \"gh pr comment 7 -R o/r --body 'at %s'\"" % OLD[:7]):
            self.assertAllowed(cmd)
        self.assertEqual(self.fetched, [])

    def test_one_fetch_per_pr(self):
        cmd = "gh pr comment 7 -R o/r --body 'at %s' && gh pr review 7 -R o/r -c -b 'at %s'" % (HEAD[:7], HEAD[:7])
        self.assertAllowed(cmd)
        self.assertEqual(self.fetched, [("o/r", "7")])

    def test_claims_are_conservative(self):
        self.assertEqual(shg.claims("Reviewed at abc1234 and head `def5678`; @ 0a1b2c3d"),
                         ["abc1234", "def5678", "0a1b2c3d"])
        self.assertEqual(shg.claims("at 1234567 rows, at defaced, sha abc1234, @abc1234, at abc12"), [])


class Hook(unittest.TestCase):
    """The hook as Claude Code runs it, with a fake `gh` on the head-fetch path."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.calls = self.tmp / "calls"
        self.gh = self.tmp / "gh"

    def tearDown(self):
        self._tmp.cleanup()

    def fake_gh(self, script):
        self.gh.write_text("#!/bin/sh\necho \"$@\" >> '%s'\n%s\n" % (self.calls, script))
        self.gh.chmod(0o755)

    def run_hook(self, raw, env_extra=None):
        env = dict(os.environ, STALE_HEAD_POST_GUARD_GH=str(self.gh))
        env.pop("STALE_HEAD_POST_GUARD_DISABLE", None)
        env.update(env_extra or {})
        return subprocess.run(PYBASE + [str(HOOK)], input=raw, capture_output=True, text=True,
                              env=env, timeout=30)

    def test_blocks_exit2_with_both_shas(self):
        self.fake_gh("echo %s" % HEAD)
        p = self.run_hook(payload("gh api %s -f commit_id=%s -f event=APPROVE" % (REVIEWS, OLD)))
        self.assertEqual(p.returncode, 2)
        self.assertIn(OLD, p.stderr)
        self.assertIn(HEAD, p.stderr)
        self.assertEqual(self.calls.read_text().split(), ["api", "repos/o/r/pulls/7", "--jq", ".head.sha"])

    def test_matching_head_is_silent(self):
        self.fake_gh("echo %s" % HEAD)
        p = self.run_hook(payload("gh api %s -f commit_id=%s" % (REVIEWS, HEAD)))
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))

    def test_fetch_failure_fails_open_with_a_warning(self):
        for script in ("echo 'HTTP 404' >&2; exit 1", "echo not-a-sha", "exit 0"):
            self.fake_gh(script)
            p = self.run_hook(payload("gh pr comment 7 -R o/r --body 'at %s'" % OLD[:7]))
            self.assertEqual((p.returncode, p.stdout), (0, ""), script)
            self.assertIn("could not read the head", p.stderr)
        p = self.run_hook(payload("gh pr comment 7 -R o/r --body 'at %s'" % OLD[:7]),
                          {"STALE_HEAD_POST_GUARD_GH": str(self.tmp / "absent")})
        self.assertEqual(p.returncode, 0)
        self.assertIn("could not read the head", p.stderr)

    def test_fetch_timeout_fails_open(self):
        self.fake_gh("sleep 30")
        orig = shg.FETCH_TIMEOUT
        shg.FETCH_TIMEOUT = 1
        try:
            os.environ["STALE_HEAD_POST_GUARD_GH"] = str(self.gh)
            self.assertIsNone(shg.fetch_head("o/r", "7"))
        finally:
            shg.FETCH_TIMEOUT = orig
            os.environ.pop("STALE_HEAD_POST_GUARD_GH", None)

    def test_disable(self):
        self.fake_gh("echo %s" % HEAD)
        p = self.run_hook(payload("gh api %s -f commit_id=%s" % (REVIEWS, OLD)),
                          {"STALE_HEAD_POST_GUARD_DISABLE": "1"})
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""))
        self.assertFalse(self.calls.exists())

    def test_garbage_and_other_tools_exit_0(self):
        self.fake_gh("echo %s" % HEAD)
        for bad in ["", "not json", "[]", '{"tool_input": 5}',
                    json.dumps({"tool_name": "Read", "tool_input": {"command": "gh api %s -f commit_id=%s"
                                                                    % (REVIEWS, OLD)}})]:
            p = self.run_hook(bad)
            self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""), bad)

    def test_missing_or_broken_scanner_fails_open(self):
        self.fake_gh("echo %s" % HEAD)
        for scanner in (None, "raise ImportError('boom')\n", "def (:\n"):
            with tempfile.TemporaryDirectory() as d:
                hook = Path(d, "stale-head-post-guard.py")
                hook.write_text(HOOK.read_text())
                if scanner is not None:
                    Path(d, "_shell_scan.py").write_text(scanner)
                env = dict(os.environ, STALE_HEAD_POST_GUARD_GH=str(self.gh))
                p = subprocess.run([sys.executable, str(hook)], capture_output=True, text=True, env=env,
                                   input=payload("gh api %s -f commit_id=%s" % (REVIEWS, OLD)), timeout=30)
                self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "", ""), scanner)


class Registration(unittest.TestCase):
    def test_the_launcher_registers_the_hook(self):
        launch = (REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh").read_text()
        self.assertIn('"$REPO/hooks/stale-head-post-guard.py"', launch)

    def test_the_builder_registers_it_on_bash(self):
        builder = REPO / "src" / "agent" / "claude" / "cli" / "build-core-settings.mjs"
        hook = "/x/y z/hooks/stale-head-post-guard.py"
        out = subprocess.run(["node", str(builder), "/x/hooks/skip-ask-user-question.py", "", "", "", "", "", "",
                              hook], capture_output=True, text=True, timeout=30, check=True).stdout
        blk = [b for b in json.loads(out)["hooks"]["PreToolUse"]
               if any("stale-head-post-guard" in h["command"] for h in b["hooks"])]
        self.assertEqual(len(blk), 1)
        self.assertEqual(blk[0]["matcher"], "Bash")
        words = subprocess.run(["/bin/bash", "-c", "printf '%s\\n' " + blk[0]["hooks"][0]["command"]],
                               capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(words, ["python3", hook])


if __name__ == "__main__":
    unittest.main(verbosity=1)
