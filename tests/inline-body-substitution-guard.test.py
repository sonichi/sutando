#!/usr/bin/env python3
"""A `gh --body` written inline in double quotes loses every code span.

Measured 2026-09-03 on sonichi/sutando#3829: a review reply containing
`shlex.shlex(...)` and `shlex.split` published with holes where those spans had
been — "Applied as you wrote it — with , reset on a separator, and ." The
command succeeded and returned a comment URL, so nothing looked wrong until the
comment was read back. Repaired by PATCHing the comment from a file.
"""
import importlib.util
import json
import os
import subprocess
import sys
import unittest

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "hooks", "inline-body-substitution-guard.py")
spec = importlib.util.spec_from_file_location("ibg", HOOK)
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)


class TestOffenders(unittest.TestCase):
    def test_an_unquoted_hash_comments_out_the_rest_of_ITS_line_only(self):
        """`shlex` keeps commenters='#', and once newlines became ';' nothing
        terminated a comment — so a '#' on line 1 hid a violation on line 2
        (vidhuUC, reproduced by sonichi, on both guards)."""
        self.assertEqual(
            G.offenders('echo hi # deploying now\n'
                        'gh pr comment 1 --body "cost is $(wc -l)"'), ["--body"])
        self.assertEqual(
            G.offenders('echo hi # deploying now\r\n'
                        'gh pr comment 1 --body "cost is $(wc -l)"'), ["--body"])

    def test_a_fully_commented_command_is_not_a_violation(self):
        """Why commenters='' would be the WRONG fix: it would flag this."""
        self.assertEqual(G.offenders('# gh pr comment 1 --body "x $(date)"'), [])

    def test_a_hash_inside_a_quoted_body_is_text(self):
        self.assertEqual(G.offenders('gh pr comment 1 --body "a # b"'), [])

    def test_an_escaped_backtick_publishes_literally(self):
        """It is not a rewrite, so denying it cries wolf (sonichi)."""
        self.assertEqual(G.offenders(r'gh pr comment 1 --body "use \`code\` here"'), [])
        self.assertEqual(G.offenders('gh pr comment 1 --body "use `code` here"'), ["--body"])

    def test_a_whole_value_substitution_is_the_intended_content(self):
        """`--body "$(cat f)"` is the standard file-passing idiom: the value IS
        the substitution, so nothing of the author's prose is lost."""
        self.assertEqual(G.offenders('gh pr comment 1 --body "$(cat /tmp/body.md)"'), [])
        self.assertEqual(
            G.offenders('gh pr comment 1 --body "built at $(date) ok"'), ["--body"])

    def test_a_newline_ends_the_command_like_a_semicolon(self):
        """`whitespace_split` eats a newline, so `armed` survived a line break
        and a non-gh tool on line 2 was denied (yixuan-ag2, #3830)."""
        self.assertEqual(
            G.offenders('gh pr view 1 --repo o/r --json title\n'
                        'my-notes-tool --title "x $(date)"'), [])

    def test_a_newline_INSIDE_a_quoted_body_is_body_text(self):
        """The obvious fix — split the raw string on newlines — loses this:
        the token breaks, the lexer hits an unterminated quote, and a real
        violation returns []. A multi-line body is how the incident was written."""
        self.assertEqual(
            G.offenders('gh pr comment 1 --body "built at $(date)\n'
                        'second line of the body"'), ["--body"])

    def test_a_violation_on_a_later_line_is_still_caught(self):
        self.assertEqual(
            G.offenders('echo hi\ngh pr comment 1 --body "x $(date)"'), ["--body"])

    def test_the_case_that_actually_published_with_holes(self):
        self.assertEqual(
            G.offenders('gh pr comment 3829 --repo o/r --body "Applied `shlex.shlex` as written"'),
            ["--body"])

    def test_a_subshell_is_eaten_the_same_way(self):
        self.assertEqual(
            G.offenders('gh pr comment 1 --body "head is $(git rev-parse HEAD)"'), ["--body"])

    def test_single_quotes_are_safe_and_allowed(self):
        """The shell does not substitute inside single quotes, so this publishes
        exactly what was written — denying it would be a false positive."""
        self.assertEqual(
            G.offenders("gh pr comment 1 --body 'Applied `shlex.shlex` as written'"), [])

    def test_body_file_is_the_fix_and_is_never_flagged(self):
        self.assertEqual(G.offenders("gh pr comment 1 --body-file /tmp/b.md"), [])

    def test_a_plain_body_with_no_substitution_is_allowed(self):
        self.assertEqual(G.offenders('gh pr comment 1 --body "no code spans here"'), [])

    def test_a_dollar_variable_is_not_flagged(self):
        """`$VAR` interpolation is ordinary and usually intended; flagging it
        would make the guard fire on routine commands and get it switched off."""
        self.assertEqual(G.offenders('gh pr comment 1 --body "head is $SHA"'), [])

    def test_equals_form_is_the_same_case(self):
        self.assertEqual(G.offenders('gh pr create --title="a `b` c"'), ["--title"])

    def test_notes_on_a_release_counts_too(self):
        self.assertEqual(
            G.offenders('gh release create v1 --notes "see `foo`"'), ["--notes"])

    def test_another_tool_is_not_this_guard_s_business(self):
        self.assertEqual(G.offenders('curl -d "a `b` c" https://x'), [])

    def test_a_non_gh_tool_using_the_same_flag_name_is_not_flagged(self):
        """The scoping is load-bearing, and only a non-gh command using one of
        these very flags can show it: `curl -d` shares no flag name, so it is
        satisfied whether or not the guard checks which tool it is."""
        self.assertEqual(G.offenders('glab mr note 1 --body "a `b` c"'), [])
        self.assertEqual(G.offenders('my-tool --notes "a `b` c"'), [])

    def test_a_separator_ends_the_gh_command(self):
        self.assertEqual(
            G.offenders('gh pr view 1 && curl -d "a `b` c" https://x'), [])

    def test_a_path_qualified_gh_still_arms(self):
        self.assertEqual(
            G.offenders('/opt/homebrew/bin/gh pr comment 1 --body "a `b` c"'), ["--body"])

    def test_unbalanced_quotes_do_not_raise(self):
        self.assertEqual(G.offenders('gh pr comment 1 --body "unclosed `'), [])


class TestHookIO(unittest.TestCase):
    def _run(self, payload, env=None):
        e = dict(os.environ)
        e.pop("SUTANDO_SKIP_INLINE_BODY_GUARD", None)
        e.update(env or {})
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                           capture_output=True, text=True, env=e)
        return p.returncode, p.stdout

    def test_denies_and_names_the_fix(self):
        rc, out = self._run({"tool_name": "Bash", "tool_input": {
            "command": 'gh pr comment 1 --body "a `b` c"'}})
        self.assertEqual(rc, 0)
        d = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(d["permissionDecision"], "deny")
        self.assertIn("--body-file", d["permissionDecisionReason"])

    def test_allows_body_file_silently(self):
        rc, out = self._run({"tool_name": "Bash", "tool_input": {
            "command": "gh pr comment 1 --body-file /tmp/b.md"}})
        self.assertEqual((rc, out.strip()), (0, ""))

    def test_a_non_bash_tool_is_ignored(self):
        rc, out = self._run({"tool_name": "Write", "tool_input": {
            "command": 'gh pr comment 1 --body "a `b` c"'}})
        self.assertEqual((rc, out.strip()), (0, ""))

    def test_the_escape_hatch_allows(self):
        rc, out = self._run({"tool_name": "Bash", "tool_input": {
            "command": 'gh pr comment 1 --body "a `b` c"'}},
            env={"SUTANDO_SKIP_INLINE_BODY_GUARD": "1"})
        self.assertEqual((rc, out.strip()), (0, ""))

    def test_malformed_input_fails_open(self):
        p = subprocess.run([sys.executable, HOOK], input="not json",
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
