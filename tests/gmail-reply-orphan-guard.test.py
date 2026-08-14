#!/usr/bin/env python3
# The guard denies via a stdout JSON permissionDecision and exits 0 — judging it
# by exit code reports a working guard as inert.
import json
import subprocess
import sys
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "skills" / "gws-gmail-voice" / "hooks" / "reply-orphan-guard.py"


def decide(tool, cmd, raw_tool_input=False):
    tool_input = cmd if raw_tool_input else {"command": cmd}
    r = subprocess.run([sys.executable, str(GUARD)],
                       input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                       capture_output=True, text=True)
    if not r.stdout.strip():
        return "allow"
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"]


class ReplyOrphanGuard(unittest.TestCase):
    def test_it_denies_an_unthreaded_reply_subject(self):
        for subj in ("Re: EGC 2027 keynote", "Fwd: contract", "RE: budget", "fw: notes"):
            self.assertEqual(
                decide("Bash", f'gws gmail +send --to a@b.com --subject "{subj}" --body hi'), "deny",
                f"+send with {subj!r} starts a NEW thread — the recipient sees an orphan")

    def test_it_allows_a_properly_threaded_reply(self):
        self.assertEqual(
            decide("Bash", 'gws gmail +reply --message-id 19f --subject "Re: EGC" --body hi'), "allow")

    def test_it_allows_a_genuinely_new_thread(self):
        self.assertEqual(
            decide("Bash", 'gws gmail +send --to a@b.com --subject "Intro call" --body hi'), "allow")

    def test_message_id_does_NOT_rescue_send(self):
        """+send has no thread flag, so --message-id there is not honoured.

        Treating it as safe reopened the exact orphan the guard exists to stop.
        """
        self.assertEqual(
            decide("Bash", 'gws gmail +send --message-id 19f --subject "Re: EGC" --body hi'), "deny")

    def test_reply_text_in_the_BODY_does_not_make_a_send_safe(self):
        """The reviewer's repro: a whole-command regex read body prose as proof
        of threading, so the plainest orphan there is was allowed."""
        for body in ("please use +reply next time", "set in-reply-to yourself"):
            self.assertEqual(
                decide("Bash", f'gws gmail +send --to a@b.com --subject "Re: EGC" --body "{body}"'),
                "deny", f"body text {body!r} must not mark a +send safe")

    def test_a_re_subject_only_in_the_body_is_not_a_reply(self):
        self.assertEqual(
            decide("Bash", 'gws gmail +send --to a@b.com --subject "Notes" --body "he wrote Re: X"'),
            "allow")

    def test_a_chained_orphan_after_a_safe_reply_is_still_denied(self):
        self.assertEqual(
            decide("Bash", 'gws gmail +reply --message-id a --body hi'
                           ' && gws gmail +send --subject "Re: Y" --body z'), "deny")

    def test_unparseable_quoting_fails_closed(self):
        self.assertEqual(
            decide("Bash", 'gws gmail +send --subject "Re: X --body hi'), "deny")

    def test_it_allows_reply_which_actually_threads(self):
        self.assertEqual(
            decide("Bash", 'gws gmail +reply --message-id 19f --subject "Re: EGC" --body hi'), "allow")

    def test_a_non_dict_tool_input_fails_open_with_a_decision_not_a_crash(self):
        """Real payloads have carried a str and a list here; .get() on those
        raises and the hook then emits no decision at all."""
        for bad in ("not an object", [1, 2], None, 7):
            self.assertEqual(decide("Bash", bad, raw_tool_input=True), "allow")

    def test_it_ignores_non_Bash_tools(self):
        self.assertEqual(decide("Write", 'gws gmail +send --subject "Re: x"'), "allow")

    def test_malformed_input_fails_OPEN_not_closed(self):
        r = subprocess.run([sys.executable, str(GUARD)], input="not json",
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "a crashing hook must never wedge the core")
        self.assertEqual(r.stdout.strip(), "", "fail-open: no decision emitted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
