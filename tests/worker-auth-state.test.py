#!/usr/bin/env python3
"""src/worker_auth_state.py: a pane stays signed out until it shows a turn that ran.

User feedback 2026-09-29: four workers on a six-seat pool host printed
"Login expired · Please run /login" in a 0 s turn and every "session expired"
card was edited to "Resolved" within 30 s. The reading here is what the monitor
and the pool sweep share: the latest refusal stands until positive proof of a
signed-in turn follows it; a newer prompt, a spinner or an empty capture is not
proof. Which lines are the refusal is cli_wedge's needs-login grammar, read through
it. Run: python3 tests/worker-auth-state.test.py
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from cli_wedge import live_banner_lines  # noqa: E402
from worker_auth_state import (auth_expired, authenticated_turn, login_expired,  # noqa: E402
                               login_refusal, signed_in_since)

FOOTER = ("────────\n❯ \n────────\n"
          "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents")
EXPIRED = "Login expired · Please run /login"
REFUSED = f"❯ /startup\n  ⎿  {EXPIRED}\n✻ Worked for 0s\n"
REAL = "❯ /startup\n● /startup complete: watcher streaming, 3 crons registered.\n✻ Worked for 2m 14s · done 12:55 PM\n"


class TheRefusalStands(unittest.TestCase):
    def test_the_clis_refusal_forms_are_read_at_line_start(self):
        for line in (EXPIRED, "Not logged in · Please run /login",
                     "OAuth access token has expired · Please run /login",
                     "You are not logged in. Run /login", "Run /login first: not logged in"):
            pane = f"❯ /startup\n  ⎿  {line}\n✻ Worked for 0s\n" + FOOTER
            self.assertEqual(login_expired(pane), line)
            self.assertTrue(auth_expired(pane))
            self.assertFalse(authenticated_turn(pane), line)

    def test_the_grammar_is_cli_wedges_needs_login_family(self):
        # One grammar, cli_wedge's: every line it names needs-login is a refusal here (the
        # login menu included), and the bare three words with no /login token are prose.
        for line in ("Select login method", "Session expired. Run /login",
                     "Please log in to continue"):
            self.assertEqual([n for _f, n, _l in live_banner_lines(line)], ["needs-login"], line)
            self.assertTrue(login_refusal(line), line)
            self.assertEqual(login_expired(f"❯ x\n  ⎿  {line}\n" + FOOTER), line)
        self.assertEqual(live_banner_lines("not logged in"), [])
        self.assertFalse(login_refusal("not logged in"))
        self.assertIsNone(login_expired("❯ /startup\n  ⎿  not logged in\n✻ Worked for 0s\n" + FOOTER))

    def test_a_refusal_under_a_newer_typed_prompt_still_stands(self):
        # The P1-41 flip: the pool re-arms the seat by typing, so the refusal is no longer
        # the last completed turn, and the seat is exactly as signed out as before.
        pane = REFUSED + FOOTER.replace("❯ \n", "❯ try again\n", 1)
        self.assertEqual(login_expired(pane), EXPIRED)
        self.assertFalse(authenticated_turn(pane))

    def test_a_refusal_under_a_turn_still_spinning_stands(self):
        pane = REFUSED + "❯ try again\n✻ Perambulating… (1m 46s · ↓ 5.9k tokens)\n" + FOOTER
        self.assertEqual(login_expired(pane), EXPIRED)
        self.assertFalse(authenticated_turn(pane))

    def test_a_second_refusal_is_the_one_that_stands(self):
        second = "OAuth access token has expired · Please run /login"
        pane = REFUSED + f"❯ /startup\n  ⎿  {second}\n✻ Cooked for 1s · done 12:42 PM\n" + FOOTER
        self.assertEqual(login_expired(pane), second)

    def test_a_refusal_after_a_real_turn_stands(self):
        pane = REAL + REFUSED + FOOTER
        self.assertEqual(login_expired(pane), EXPIRED)
        self.assertFalse(authenticated_turn(pane))

    def test_the_status_line_form_under_the_footer_stands(self):
        pane = FOOTER + "\n     Not logged in · Run /login"
        self.assertEqual(login_expired(pane), "Not logged in · Run /login")

    def test_nothing_is_read_from_an_empty_capture(self):
        for pane in (None, "", "   \n"):
            self.assertIsNone(login_expired(pane))
            self.assertFalse(auth_expired(pane))
            self.assertFalse(authenticated_turn(pane))


class TheRefusalIsCleared(unittest.TestCase):
    def test_login_successful_after_it(self):
        pane = REFUSED + "  ⎿  Login successful\n" + FOOTER
        self.assertIsNone(login_expired(pane))
        self.assertTrue(authenticated_turn(pane))

    def test_a_completed_turn_that_outran_a_refusal(self):
        for done in ("✻ Worked for 12s · done 1:00 PM", "✻ Worked for 2m 3s · done 1:00 PM",
                     "✻ Cooked for 10s"):
            pane = REFUSED + f"❯ hi\n{done}\n" + FOOTER
            self.assertIsNone(login_expired(pane), done)
            self.assertTrue(authenticated_turn(pane), done)

    def test_the_agents_own_output_after_it(self):
        pane = REFUSED + "❯ hi\n● Hello. What should I do next?\n" + FOOTER
        self.assertIsNone(login_expired(pane))

    def test_a_tool_call_after_it(self):
        pane = REFUSED + "❯ hi\n⏺ Bash(ls)\n" + FOOTER
        self.assertIsNone(login_expired(pane))

    def test_a_short_turn_that_ran_is_proof(self):
        # One second, but the agent called a tool in it: not a refusal's shape.
        pane = REFUSED + "❯ hi\n⏺ Bash(true)\n  ⎿  (no output)\n✻ Worked for 1s\n" + FOOTER
        self.assertIsNone(login_expired(pane))
        self.assertTrue(authenticated_turn(pane))


class TheClearingRuleIsShared(unittest.TestCase):
    # signed_in_since is what runtime-health's needs_login reads below its own marker
    # line, so the rule is pinned here once, on the text that follows a refusal.
    def test_what_clears(self):
        for tail in ("❯ go\n⏺ Bash(ls)\n", "❯ go\n● Done.\n", "❯ /login\n  ⎿  Login successful\n",
                     "❯ hi\n✻ Cooked for 1m 3s · done 1:00 PM\n", "✻ Worked for 0s\n❯ hi\n✻ Worked for 12s\n"):
            self.assertTrue(signed_in_since(tail + FOOTER), tail)
            self.assertIsNone(login_expired(REFUSED + tail + FOOTER), tail)

    def test_what_does_not(self):
        for tail in ("", "✻ Worked for 0s\n", "❯ try again\n", "❯ try again\n✻ Perambulating… (1m 46s)\n",
                     "❯ hi\n  ⎿  Unknown slash command: /hi\n✻ Cooked for 1s · done 1:00 PM\n"):
            self.assertFalse(signed_in_since(tail + FOOTER), tail)
            self.assertEqual(login_expired(REFUSED + tail + FOOTER), EXPIRED, tail)
        for empty in (None, "", "  \n"):
            self.assertFalse(signed_in_since(empty))

    def test_a_slow_refusal_is_read_as_a_turn_that_ran(self):
        # Accepted edge: the refusal's own done line is the only duration the pane
        # offers, so a refusal that took longer than 1 s (network latency) reads as work.
        pane = f"❯ /startup\n  ⎿  {EXPIRED}\n✻ Worked for 2s\n" + FOOTER
        self.assertIsNone(login_expired(pane))
        self.assertTrue(authenticated_turn(pane))


class TheWordsAreNotARefusal(unittest.TestCase):
    def test_inside_a_turn_that_ran(self):
        # A tool read the line and the agent answered: the result is the tool's.
        pane = ("❯ check auth\n⏺ Bash(cat diagnostic.txt)\n"
                f"  ⎿  {EXPIRED}\n● The diagnostic was read successfully.\n"
                "✻ Worked for 1s\n" + FOOTER)
        self.assertIsNone(login_expired(pane))
        self.assertTrue(authenticated_turn(pane))

    def test_a_tool_result_alone_in_a_short_turn(self):
        pane = f"❯ check auth\n⏺ Bash(cat diagnostic.txt)\n  ⎿  {EXPIRED}\n✻ Worked for 0s\n" + FOOTER
        self.assertIsNone(login_expired(pane))

    def test_three_common_words_in_ordinary_output(self):
        for line in ("gh: not logged in to github.com",
                     "src/auth.py:42: raise RuntimeError('not logged in')",
                     "shown to a visitor who is not logged in",
                     "ok test_errors_when_not_logged_in"):
            pane = f"❯ /somecommand\n  ⎿  {line}\n✻ Worked for 1s\n" + FOOTER
            self.assertIsNone(login_expired(pane), line)

    def test_the_agent_saying_the_words_is_its_own_line(self):
        pane = "❯ status?\n● Login expired is what the log said; I re-ran it.\n✻ Worked for 3s\n" + FOOTER
        self.assertIsNone(login_expired(pane))


class ProofOfASignedInTurn(unittest.TestCase):
    def test_a_real_turn_with_no_refusal(self):
        self.assertTrue(authenticated_turn(REAL + FOOTER))

    def test_a_footer_alone_is_not_proof(self):
        self.assertFalse(authenticated_turn(FOOTER))

    def test_a_bare_short_turn_is_not_proof(self):
        pane = "❯ /nosuch\n  ⎿  Unknown slash command: /nosuch\n✻ Worked for 0s\n" + FOOTER
        self.assertFalse(authenticated_turn(pane))

    def test_a_spinner_is_not_a_completed_turn(self):
        pane = "❯ hi\n● Checking.\n✻ Perambulating… (1m 46s · ↓ 5.9k tokens)\n" + FOOTER
        self.assertFalse(authenticated_turn(pane))

    def test_a_replayed_transcript_without_a_completed_turn_is_not_proof(self):
        # A resumed session re-renders old `●` / `⏺` lines; only a completed turn counts.
        pane = "● Earlier answer.\n⏺ Bash(ls)\n  ⎿  a b c\n" + FOOTER
        self.assertFalse(authenticated_turn(pane))

    def test_a_completed_turn_without_a_duration_counts_only_if_it_ran(self):
        self.assertTrue(authenticated_turn("❯ hi\n● Done.\n✻ Cooked · done 1:00 PM\n" + FOOTER))
        self.assertFalse(authenticated_turn("❯ hi\n  ⎿  x\n✻ Cooked · done 1:00 PM\n" + FOOTER))


if __name__ == "__main__":
    unittest.main()
