#!/usr/bin/env python3
# The guard denies via a stdout JSON permissionDecision and exits 0 — judging it
# by exit code reports a working guard as inert.
import json
import subprocess
import sys
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "skills" / "gws-gmail-voice" / "hooks" / "reply-orphan-guard.py"


def decide(tool, cmd):
    r = subprocess.run([sys.executable, str(GUARD)],
                       input=json.dumps({"tool_name": tool, "tool_input": {"command": cmd}}),
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

    def test_it_allows_send_that_carries_threading(self):
        self.assertEqual(
            decide("Bash", 'gws gmail +send --message-id 19f --subject "Re: EGC" --body hi'), "allow")

    def test_it_ignores_non_Bash_tools(self):
        self.assertEqual(decide("Write", 'gws gmail +send --subject "Re: x"'), "allow")

    def test_malformed_input_fails_OPEN_not_closed(self):
        r = subprocess.run([sys.executable, str(GUARD)], input="not json",
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, "a crashing hook must never wedge the core")
        self.assertEqual(r.stdout.strip(), "", "fail-open: no decision emitted")


if __name__ == "__main__":
    unittest.main(verbosity=2)
