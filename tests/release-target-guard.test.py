#!/usr/bin/env python3
"""`gh release --target <abbreviated sha>` must be denied before it runs.

GitHub answers `Release.target_commitish is invalid` and creates nothing, so the
release reads as cut at the exact moment it did not happen. Measured twice in
fourteen hours on one host, the correction written between the two.
"""
import importlib.util
import json
import os
import subprocess
import sys
import unittest

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "hooks", "release-target-guard.py")

spec = importlib.util.spec_from_file_location("rtg", HOOK)
G = importlib.util.module_from_spec(spec)
spec.loader.exec_module(G)

SHORT = "f653b4d9f8a4"
FULL = "f653b4d9f8a43e35486d85da909ede509b3b85de"


class TestOffenders(unittest.TestCase):
    def test_the_abbreviated_form_that_actually_failed(self):
        self.assertEqual(
            G.offenders(f"gh release edit v0.6.3 --repo o/r --draft=false --target {SHORT}"),
            [SHORT])

    def test_the_full_sha_is_allowed(self):
        self.assertEqual(G.offenders(f"gh release edit v0.6.3 --target {FULL}"), [])

    def test_a_branch_name_is_allowed(self):
        self.assertEqual(G.offenders("gh release create v1 --target main"), [])

    def test_equals_form_is_the_same_case(self):
        self.assertEqual(G.offenders(f"gh release create v1 --target={SHORT}"), [SHORT])

    def test_a_seven_char_sha_is_still_abbreviated(self):
        self.assertEqual(G.offenders("gh release create v1 --target 544a68f"), ["544a68f"])

    def test_a_short_hex_branch_name_is_a_name_not_a_paste(self):
        """git abbreviates to 7 by default, so a shorter hex run is a branch
        someone named. Denying it would refuse a legitimate cut."""
        self.assertEqual(G.offenders("gh release create v1 --target dad"), [])
        self.assertEqual(G.offenders("gh release create v1 --target abcdef"), [])

    def test_a_41_char_hex_run_is_not_a_sha_either(self):
        self.assertEqual(G.offenders("gh release create v1 --target " + "a" * 41), ["a" * 41])

    def test_a_branch_whose_name_is_hex_words_is_not_a_sha(self):
        """`deadbeef-fix` and `main` are names; only a bare hex run is the paste
        this guard exists to catch."""
        self.assertEqual(G.offenders("gh release create v1 --target deadbeef-fix"), [])

    def test_a_target_belonging_to_another_command_is_not_read(self):
        """The flag is common. Reading one that belongs to a different tool would
        refuse work this guard has no claim on."""
        self.assertEqual(G.offenders(f"some-other-tool --target {SHORT}"), [])

    def test_gh_pr_is_not_gh_release(self):
        self.assertEqual(G.offenders(f"gh pr merge 1 --target {SHORT}"), [])

    def test_a_later_gh_command_disarms_the_previous_release(self):
        self.assertEqual(
            G.offenders(f"gh release create v1 --target main && gh pr view 1 --target {SHORT}"),
            [])

    def test_both_targets_in_one_chain_are_reported(self):
        self.assertEqual(
            G.offenders(f"gh release create v1 --target {SHORT} ; gh release edit v2 --target 544a68f"),
            [SHORT, "544a68f"])

    def test_a_separator_ends_the_gh_command(self):
        """`shlex.split` glues `;` to the word before it, so the incident command
        with a trailing `; echo` read as target `<sha>;` and was ALLOWED — the
        guard missing the exact command it was built for (vidhuUC, #3829)."""
        self.assertEqual(G.offenders(f"gh release edit v1 --target {SHORT}; echo done"), [SHORT])
        self.assertEqual(G.offenders(f"gh release edit v1 --target {SHORT} && echo ok"), [SHORT])

    def test_a_global_flag_between_gh_and_release_does_not_disarm(self):
        """`targets` armed only on the two words immediately after `gh`, so a
        global flag disarmed it entirely — and `-R` is exactly how a release is
        cut from another repo's checkout (Sutando-Mini via sonichi, #3829)."""
        self.assertEqual(
            G.offenders("gh -R sonichi/sutando release create v1 --target abcdef1"), ["abcdef1"])
        self.assertEqual(
            G.offenders(f"gh --repo o/r release edit v1 --target {SHORT}"), [SHORT])
        self.assertEqual(
            G.offenders(f"/opt/homebrew/bin/gh -R o/r release edit v1 --target {SHORT}"), [SHORT])

    def test_a_global_flag_does_not_make_every_gh_a_release_cut(self):
        """Arming on `release create|edit` anywhere in the segment must not
        arm on a different subcommand that merely follows a flag."""
        self.assertEqual(G.offenders(f"gh -R o/r pr view 1 --target {SHORT}"), [])
        self.assertEqual(G.offenders(f"gh -R o/r release list --target {SHORT}"), [])

    def test_the_arm_scan_stops_at_a_separator(self):
        """Scanning past a separator would let a LATER `gh release create` arm
        an EARLIER, unrelated gh command — so its --target gets flagged."""
        self.assertEqual(
            G.offenders(f"gh pr view 1 --target {SHORT} && gh release create v2 --target main"), [])

    def test_an_abbreviated_sha_is_caught_in_any_case(self):
        """git resolves an uppercase abbreviated sha and GitHub rejects it the
        same way, so a case-sensitive HEX_RUN let it through (sonichi, #3829)."""
        self.assertEqual(G.offenders("gh release create v1 --target ABCDEF1"), ["ABCDEF1"])
        self.assertEqual(G.offenders("gh release create v1 --target AbCdEf1"), ["AbCdEf1"])

    def test_a_full_uppercase_sha_is_still_allowed(self):
        """The pair must move together: widening HEX_RUN alone would deny a
        40-char uppercase sha, which is valid and is what --target wants."""
        self.assertEqual(G.offenders("gh release create v1 --target " + "F" * 40), [])
        self.assertEqual(G.offenders("gh release create v1 --target " + "aF" * 20), [])

    def test_a_newline_ends_the_command(self):
        """`armed` survived a line break, so a --target on a LATER, unrelated
        line was flagged. Raised three times by vidhuUC on #3829."""
        self.assertEqual(
            G.offenders("gh release create v1 --target main\ncargo build --target deadbeef1"), [])
        self.assertEqual(
            G.offenders("gh release create v1 --target main\ndocker build --target abcdef1234 ."), [])

    def test_CRLF_is_ONE_separator_not_two(self):
        """Mapping both \r and \n to ';' yields ';;', which lexes as a single
        token that is not in SEPARATORS — so `armed` would never reset and the
        CRLF case would keep failing while the LF case passed."""
        self.assertEqual(
            G.offenders("gh release create v1 --target main\r\ncargo build --target deadbeef1"), [])

    def test_a_violation_on_a_later_line_is_still_caught(self):
        self.assertEqual(
            G.offenders("echo hi\ngh release edit v1 --target f653b4d9f8a4"), ["f653b4d9f8a4"])
        self.assertEqual(
            G.offenders("echo hi\r\ngh release edit v1 --target f653b4d9f8a4"), ["f653b4d9f8a4"])

    def test_a_path_qualified_gh_still_arms(self):
        self.assertEqual(
            G.offenders(f"/opt/homebrew/bin/gh release edit v1 --target {SHORT}"), [SHORT])

    def test_another_tool_after_a_gh_command_is_not_attributed_to_it(self):
        """`armed` used to survive every non-`gh` word, so a later tool's own
        --target was denied. A false denial is how a guard gets switched off."""
        self.assertEqual(
            G.offenders("gh release create v1 --target main && docker build --target abcdef1234 ."), [])
        self.assertEqual(
            G.offenders("gh release create v1 --target main; cargo build --target deadbeef1"), [])

    def test_a_quoted_separator_stays_one_word(self):
        """The tokenizer change must not split quoted text — that is the obvious
        way this fix breaks something."""
        self.assertEqual(
            G.offenders(f'gh release create v1 --notes "a && b" --target {SHORT}'), [SHORT])

    def test_unbalanced_quotes_do_not_raise(self):
        self.assertEqual(G.offenders("gh release create v1 --target 'unclosed"), [])


class TestHookIO(unittest.TestCase):
    def _run(self, payload, env=None):
        e = dict(os.environ)
        e.pop("SUTANDO_SKIP_RELEASE_TARGET_GUARD", None)
        e.update(env or {})
        p = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                           capture_output=True, text=True, env=e)
        return p.returncode, p.stdout

    def test_denies_with_a_pretooluse_decision(self):
        rc, out = self._run({"tool_name": "Bash", "tool_input": {
            "command": f"gh release edit v0.6.3 --target {SHORT}"}})
        self.assertEqual(rc, 0)
        d = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(d["permissionDecision"], "deny")
        self.assertIn("Release.target_commitish is invalid", d["permissionDecisionReason"])
        self.assertIn("gh api", d["permissionDecisionReason"])

    def test_allows_the_full_sha_silently(self):
        rc, out = self._run({"tool_name": "Bash", "tool_input": {
            "command": f"gh release edit v0.6.3 --target {FULL}"}})
        self.assertEqual((rc, out.strip()), (0, ""))

    def test_a_non_bash_tool_is_not_this_guard_s_business(self):
        rc, out = self._run({"tool_name": "Write", "tool_input": {
            "command": f"gh release edit v1 --target {SHORT}"}})
        self.assertEqual((rc, out.strip()), (0, ""))

    def test_the_escape_hatch_allows(self):
        rc, out = self._run({"tool_name": "Bash", "tool_input": {
            "command": f"gh release edit v1 --target {SHORT}"}},
            env={"SUTANDO_SKIP_RELEASE_TARGET_GUARD": "1"})
        self.assertEqual((rc, out.strip()), (0, ""))

    def test_malformed_input_fails_open_rather_than_wedging_the_core(self):
        p = subprocess.run([sys.executable, HOOK], input="not json",
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
