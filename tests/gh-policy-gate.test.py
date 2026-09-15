#!/usr/bin/env python3
"""PreToolUse gh-policy-gate: `gh issue create` and `gh pr comment`, run via the
Bash tool from ANY caller, are gated on gh-duplicate-check.py / pr-monologue-check.py
regardless of whether the calling skill remembers to chain them itself
(hooks/gh-policy-gate.py).

Run:  python3 tests/gh-policy-gate.test.py
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "gh-policy-gate.py"
_spec = importlib.util.spec_from_file_location("gpg", HOOK)
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)


def _stub(tmpdir, name, rc, stdout=""):
    """A tiny standalone script that ignores its argv and exits `rc`, printing
    `stdout` — stands in for gh-duplicate-check.py / pr-monologue-check.py so
    these tests don't depend on live GitHub state or a real trailing-comment run."""
    p = Path(tmpdir) / name
    p.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"print({stdout!r})\n"
        f"sys.exit({rc})\n"
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _echo_argv_stub(tmpdir, name, rc):
    """Stub that JSON-dumps the argv it received (sys.argv[1:]) then exits `rc` —
    used to verify WHAT gh-policy-gate passed to MONO_CHECK, not just whether it
    denied, since a stubbed rc alone can't distinguish 'URL passed through' from
    'URL silently dropped'."""
    p = Path(tmpdir) / name
    p.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "print(json.dumps(sys.argv[1:]))\n"
        f"sys.exit({rc})\n"
    )
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _words(command):
    """First gh-invoking segment's words (past `gh` itself), or []."""
    segs = G._gh_segments(command)
    return segs[0] if segs else []


class Tokenize(unittest.TestCase):
    def test_finds_issue_create_past_a_global_flag(self):
        words = _words('gh -R o/r issue create --title "x"')
        idx = G._find_subcommand(words, ("issue", "create"))
        self.assertEqual(words[idx:idx + 2], ["--title", "x"])

    def test_finds_pr_comment_with_equals_form_flags(self):
        words = _words('gh pr comment 42 --repo=o/r --body "hi"')
        idx = G._find_subcommand(words, ("pr", "comment"))
        self.assertIsNotNone(idx)

    def test_a_global_flag_between_the_subcommand_words_is_not_a_bypass(self):
        """Real `gh` accepts --repo/-R between the two subcommand words, not
        just before them — verified against the live binary (yixuan-ag2, PR
        #4268 review 2026-09-15). The old adjacency-only check missed this."""
        words = _words('gh issue --repo o/r create --title "x"')
        idx = G._find_subcommand(words, ("issue", "create"))
        self.assertIsNotNone(idx)
        self.assertEqual(words[idx:idx + 2], ["--title", "x"])

        words = _words('gh pr --repo o/r comment 42 --body "hi"')
        idx = G._find_subcommand(words, ("pr", "comment"))
        self.assertIsNotNone(idx)

    def test_a_between_flag_with_no_value_does_not_crash_or_falsely_match(self):
        """`gh issue --repo create` (no repo value, "create" consumed as the
        flag's value) must not be mistaken for a matched subcommand — and
        must not raise on the missing tokens past it."""
        words = _words('gh issue --repo create')
        idx = G._find_subcommand(words, ("issue", "create"))
        self.assertIsNone(idx)

    def test_a_between_word_that_is_not_a_recognized_flag_still_blocks_the_match(self):
        """Only the specific global flags this hook knows about are skipped —
        an arbitrary word between the subcommand words is still a real
        adjacency miss, not silently bridged."""
        words = _words('gh issue whatever create --title "x"')
        idx = G._find_subcommand(words, ("issue", "create"))
        self.assertIsNone(idx)

    def test_a_path_qualified_gh_is_still_gh(self):
        words = _words('/opt/homebrew/bin/gh issue create --title "x"')
        self.assertIsNotNone(G._find_subcommand(words, ("issue", "create")))

    def test_a_command_that_merely_mentions_gh_matches_no_subcommand(self):
        """`_gh_segments` finds `gh` by matching a WORD's basename, via the
        shared `_shell_scan` tokenizer — so a token that merely CONTAINS "gh"
        (inside a quoted string here) is never mistaken for the `gh` binary."""
        words = _words('echo "gh is just a word here"')
        self.assertIsNone(G._find_subcommand(words, ("issue", "create")))
        self.assertIsNone(G._find_subcommand(words, ("pr", "comment")))

    def test_unrelated_gh_subcommand_matches_neither_pair(self):
        words = _words('gh pr view 1 --json body')
        self.assertIsNone(G._find_subcommand(words, ("issue", "create")))
        self.assertIsNone(G._find_subcommand(words, ("pr", "comment")))

    def test_an_and_chain_does_not_leak_a_subcommand_across_segments(self):
        """`gh pr view` in one `&&`-ed command must not combine with `issue
        create` typed in a later, unrelated one — each gh segment is scanned
        on its own."""
        words_list = G._gh_segments('gh pr view 1 && gh issue create --title "x"')
        self.assertEqual(len(words_list), 2)
        self.assertIsNone(G._find_subcommand(words_list[0], ("issue", "create")))
        self.assertIsNotNone(G._find_subcommand(words_list[1], ("issue", "create")))


class CheckIssueCreate(unittest.TestCase):
    def test_no_candidate_allows(self):
        with tempfile.TemporaryDirectory() as td:
            G.DUP_CHECK = _stub(td, "dup.py", 0, "no candidate")
            words = _words('gh issue create --repo o/r --title "brand new title"')
            idx = G._find_subcommand(words, ("issue", "create"))
            self.assertIsNone(G.check_issue_create(words, idx))

    def test_a_real_duplicate_denies_with_a_reason(self):
        with tempfile.TemporaryDirectory() as td:
            G.DUP_CHECK = _stub(td, "dup.py", 1, "REFUSE: looks like #123")
            words = _words('gh issue create --repo o/r --title "dup"')
            idx = G._find_subcommand(words, ("issue", "create"))
            found = G.check_issue_create(words, idx)
            self.assertIsNotNone(found)
            self.assertEqual(found[0], "issue create")
            self.assertIn("#123", found[1])

    def test_cannot_answer_fails_open(self):
        with tempfile.TemporaryDirectory() as td:
            G.DUP_CHECK = _stub(td, "dup.py", 2, "no network")
            words = _words('gh issue create --repo o/r --title "x"')
            idx = G._find_subcommand(words, ("issue", "create"))
            self.assertIsNone(G.check_issue_create(words, idx))

    def test_unresolvable_title_fails_open_without_running_the_script(self):
        words = _words('gh issue create --repo o/r')  # no --title
        idx = G._find_subcommand(words, ("issue", "create"))
        self.assertIsNone(G.check_issue_create(words, idx))


class CheckPrComment(unittest.TestCase):
    def setUp(self):
        os.environ["SUTANDO_GH_LOGIN"] = "test-bot"

    def tearDown(self):
        os.environ.pop("SUTANDO_GH_LOGIN", None)

    def test_no_monologue_allows(self):
        with tempfile.TemporaryDirectory() as td:
            G.MONO_CHECK = _stub(td, "mono.py", 0, "safe to post")
            words = _words('gh pr comment 42 --repo o/r --body "hi"')
            idx = G._find_subcommand(words, ("pr", "comment"))
            self.assertIsNone(G.check_pr_comment(words, idx))

    def test_a_real_monologue_denies_with_a_reason(self):
        with tempfile.TemporaryDirectory() as td:
            G.MONO_CHECK = _stub(td, "mono.py", 1, "REFUSE: trailing run of yours = 4")
            words = _words('gh pr comment 42 --repo o/r --body "hi"')
            idx = G._find_subcommand(words, ("pr", "comment"))
            found = G.check_pr_comment(words, idx)
            self.assertIsNotNone(found)
            self.assertEqual(found[0], "pr comment")
            self.assertIn("trailing run", found[1])

    def test_cannot_answer_fails_open(self):
        with tempfile.TemporaryDirectory() as td:
            G.MONO_CHECK = _stub(td, "mono.py", 2, "cannot answer")
            words = _words('gh pr comment 42 --repo o/r --body "hi"')
            idx = G._find_subcommand(words, ("pr", "comment"))
            self.assertIsNone(G.check_pr_comment(words, idx))

    def test_unresolvable_pr_number_fails_open_without_running_the_script(self):
        words = _words('gh pr comment --repo o/r --body "no number here"')
        idx = G._find_subcommand(words, ("pr", "comment"))
        self.assertIsNone(G.check_pr_comment(words, idx))

    def test_a_full_pr_url_is_recognized_without_repo_or_digit(self):
        """A full PR URL carries no bare-digit word, so the old digit scan
        found nothing and this shape went ungated (#4268 review). It must
        reach MONO_CHECK now, with no --repo required."""
        with tempfile.TemporaryDirectory() as td:
            G.MONO_CHECK = _echo_argv_stub(td, "mono.py", 1)
            words = _words('gh pr comment https://github.com/o/r/pull/42 --body "hi"')
            idx = G._find_subcommand(words, ("pr", "comment"))
            found = G.check_pr_comment(words, idx)
            self.assertIsNotNone(found)
            argv = json.loads(found[1])
            self.assertEqual(argv[0], "https://github.com/o/r/pull/42")
            self.assertNotIn("--repo", argv)

    def test_a_url_with_an_explicit_repo_passes_it_through(self):
        """An explicit --repo alongside the URL must still reach MONO_CHECK,
        so a disagreeing one hits ITS OWN refusal (exit 2) rather than being
        silently dropped by the hook."""
        with tempfile.TemporaryDirectory() as td:
            G.MONO_CHECK = _echo_argv_stub(td, "mono.py", 1)
            words = _words(
                'gh pr comment https://github.com/o/r/pull/42 --repo other/repo --body "hi"')
            idx = G._find_subcommand(words, ("pr", "comment"))
            found = G.check_pr_comment(words, idx)
            argv = json.loads(found[1])
            self.assertIn("--repo", argv)
            self.assertEqual(argv[argv.index("--repo") + 1], "other/repo")

    def test_an_unresolvable_pr_url_re_fails_open_without_crashing(self):
        """If MONO_CHECK's PR_URL_RE never loaded (its own module missing at
        import time — the hook's own live failure mode, PR #4268 review
        2026-09-15), `check_pr_comment` must degrade to 'cannot answer', not
        raise AttributeError on `None.match(...)`."""
        saved = G.PR_URL_RE
        try:
            G.PR_URL_RE = None
            words = _words('gh pr comment https://github.com/o/r/pull/42 --body "hi"')
            idx = G._find_subcommand(words, ("pr", "comment"))
            self.assertIsNone(G.check_pr_comment(words, idx))
        finally:
            G.PR_URL_RE = saved

    def test_a_url_whose_repo_disagrees_fails_open(self):
        """MONO_CHECK itself exits 2 ('CANNOT ANSWER: --repo disagrees') for
        this shape; the hook must treat that the same as any other
        cannot-answer and allow, not crash or silently deny."""
        with tempfile.TemporaryDirectory() as td:
            G.MONO_CHECK = _stub(td, "mono.py", 2, "CANNOT ANSWER: --repo disagrees")
            words = _words(
                'gh pr comment https://github.com/o/r/pull/42 --repo other/repo --body "hi"')
            idx = G._find_subcommand(words, ("pr", "comment"))
            self.assertIsNone(G.check_pr_comment(words, idx))


class EndToEnd(unittest.TestCase):
    def test_a_non_bash_tool_is_ignored(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Read", "tool_input": {"command": "gh issue create"}}),
            capture_output=True, text=True,
        )
        self.assertNotIn('"permissionDecision"', r.stdout)

    def test_a_non_gh_bash_command_is_ignored(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}}),
            capture_output=True, text=True,
        )
        self.assertNotIn('"permissionDecision"', r.stdout)

    def test_the_override_env_var_bypasses_everything(self):
        """SUTANDO_ALLOW_UNGATED_GH=1 must win even against a command that would
        otherwise deny — it is the documented one-shot escape hatch."""
        env = dict(os.environ)
        env["SUTANDO_ALLOW_UNGATED_GH"] = "1"
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"tool_name": "Bash", "tool_input": {
                "command": 'gh issue create --repo o/r --title "whatever"'}}),
            capture_output=True, text=True, env=env,
        )
        self.assertNotIn('"permissionDecision"', r.stdout)

    def test_unresolvable_repo_and_title_does_not_crash_the_hook(self):
        """No --repo, not inside a git checkout with a remote: the hook must
        print rc 0 with no deny, not raise."""
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run(
                [sys.executable, str(HOOK)],
                input=json.dumps({"tool_name": "Bash", "tool_input": {
                    "command": "gh issue create --title x"}}),
                capture_output=True, text=True, cwd=td,
            )
            self.assertEqual(r.returncode, 0)
            self.assertNotIn('"permissionDecision": "deny"', r.stdout)

    def test_a_missing_mono_check_module_does_not_crash_the_hook_at_import(self):
        """Reproduces the reviewer's exact live repro: a checkout where
        skills/proactive-loop/scripts/pr-monologue-check.py doesn't exist used
        to raise FileNotFoundError the moment gh-policy-gate.py was imported
        (PR #4268 review 2026-09-15). Copy just the hook into an isolated tree
        with no such sibling and confirm the process still starts and exits
        cleanly rather than crashing on import."""
        with tempfile.TemporaryDirectory() as td:
            isolated_hooks = Path(td) / "hooks"
            isolated_hooks.mkdir()
            (isolated_hooks / "gh-policy-gate.py").write_text(HOOK.read_text())
            (isolated_hooks / "_shell_scan.py").write_text(
                (HOOK.parent / "_shell_scan.py").read_text())
            # No skills/proactive-loop/scripts/ tree at all under td.
            r = subprocess.run(
                [sys.executable, str(isolated_hooks / "gh-policy-gate.py")],
                input=json.dumps({"tool_name": "Bash", "tool_input": {
                    "command": 'gh pr comment https://github.com/o/r/pull/1 --body "hi"'}}),
                capture_output=True, text=True,
            )
            self.assertEqual(r.returncode, 0, msg=f"stderr: {r.stderr}")
            self.assertNotIn("Traceback", r.stderr)
            self.assertNotIn('"permissionDecision": "deny"', r.stdout)


if __name__ == "__main__":
    unittest.main()
