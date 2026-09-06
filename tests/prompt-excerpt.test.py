#!/usr/bin/env python3
"""The pane filter is one module; the card and the relay both read it, neither keeps a copy."""
from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src")
sys.path.insert(0, _SRC)
from prompt_excerpt import first_readable_line, prompt_excerpt, readable_lines  # noqa: E402

LOGIN_PANE = (
    "╭──────────────────────────────────── sutando-core ─╮\n"
    "Browser didn't open? Use the url below to sign in:\n"
    "https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code&redirect_uri=http%3A%2F%2Flocalhost%3A65384%2Fcallback&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference&code_challenge=ncifI5jOgzI138TpX&state=uvUrUTS4WTI2FL\n"
    "s%3Aclaude_code+user%3Amcp_servers&code_challenge=ncifI5jOgzI138TpX&state=uvUrUTS4WTI2FL\n"
    "Paste code here if prompted >\n"
    "Esc to cancel\n"
    "──────────────────────────────────────────────────────\n"
    "⏵⏵ bypass permissions on (shift+tab to cycle)\n"
    "[sutando 2:monitor*                              ]\n"
)
NOISE = ("code_challenge", "%3A", "https://", "───", "bypass permissions", "2:monitor")


class PromptExcerptTest(unittest.TestCase):
    def test_readable_lines_keeps_the_prompt_and_drops_every_kind_of_chrome(self):
        got = readable_lines(LOGIN_PANE)
        self.assertEqual(got[0], "Browser didn't open? Use the url below to sign in:")
        self.assertIn("Paste code here if prompted >", got)
        self.assertIn("Esc to cancel", got)
        for tok in NOISE:
            self.assertFalse(any(tok in ln for ln in got), tok)

    def test_first_readable_line_is_the_prompt_not_the_rule_above_it(self):
        # The pane's first non-empty line is a box rule; the notice must not quote it.
        self.assertEqual(first_readable_line(LOGIN_PANE),
                         "Browser didn't open? Use the url below to sign in:")

    def test_prompt_excerpt_is_the_readable_tail(self):
        self.assertEqual(prompt_excerpt(LOGIN_PANE, limit=2),
                         ["Paste code here if prompted >", "Esc to cancel"])

    def test_all_chrome_falls_back_to_the_raw_lines_never_to_silence(self):
        pane = "────────\n⏵⏵ bypass permissions on\n"
        self.assertEqual(prompt_excerpt(pane), ["────────", "⏵⏵ bypass permissions on"])
        self.assertEqual(first_readable_line(pane), "────────")
        self.assertEqual(first_readable_line(""), "")
        self.assertEqual(first_readable_line(None), "")

    def test_the_filter_has_one_home_and_both_adapters_delegate(self):
        # A private copy of the regex in either adapter is the defect this module removes.
        watch = open(os.path.join(_SRC, "core-input-watch.py"), encoding="utf-8").read()
        relay = open(os.path.join(_SRC, "core-supervisor-relay.py"), encoding="utf-8").read()
        for src, name in ((watch, "core-input-watch"), (relay, "core-supervisor-relay")):
            self.assertNotIn("code_challenge", src, f"{name} keeps a private copy of the filter")
            self.assertNotIn("_NOISE_LINE", src, f"{name} keeps a private copy of the filter")
            self.assertIn("from prompt_excerpt import", src, f"{name} does not delegate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
