#!/usr/bin/env python3
"""The OpenAI-key rule needs a boundary before `sk-`, so task ids are not keys.

A task id such as ``task-ctl-1795286780672-19a2df-s_dspace`` contains
"ta**sk-**ctl…" followed by 33 key-alphabet characters, and the old unanchored
``sk-(?:proj-)?[A-Za-z0-9_-]{32,}`` rule redacted it as an "OpenAI Token".
The rule now requires that no letter or digit precede ``sk-``. Every key below
is an obvious placeholder in OpenAI's published shape; none is a real credential.

Run: python3 tests/chat-secret-filter-openai-boundary.test.py
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import chat_secret_filter
from chat_secret_filter import filter_chat_secrets

FAKE_KEY = "sk-" + "FAKE0000" * 5                 # 40-char body
FAKE_PROJ_KEY = "sk-proj-" + "fake_0000-" * 5      # project-key shape
PLACEHOLDER = "[REDACTED-OpenAI Token]"
OPENAI_RULE = dict(chat_secret_filter._FALLBACK_PATTERNS)["OpenAI Token"]


class TestTaskIdsAreNotKeys(unittest.TestCase):
    NOT_KEYS = (
        "task-ctl-1795286780672-19a2df-s_dspace",
        "task-ctl-1795286780672-19a2df-s_dspace.txt",
        "disk-" + "0123456789abcdef" * 3,
        "risk-" + "assessment_for_quarter_four_2026",
        "flask-" + "0" * 40,
        "ask-" + "A" * 40,
    )

    def test_fallback_rule_does_not_match(self):
        for text in self.NOT_KEYS:
            with self.subTest(text=text):
                self.assertIsNone(OPENAI_RULE.search(text))

    def test_filter_keeps_task_id_in_prose(self):
        text = "result for task-ctl-1795286780672-19a2df-s_dspace is ready"
        result = filter_chat_secrets(text)
        self.assertNotIn("OpenAI Token", result.secret_types)
        self.assertIn("task-ctl-1795286780672-19a2df-s_dspace", result.text)


class TestRealKeyShapesStillRedacted(unittest.TestCase):
    CONTEXTS = (
        "{k}",
        "key {k} here",
        "line one\n{k}\nline three",
        "OPENAI_API_KEY={k}",
        "openai: {k}",
        "'{k}'",
        '"{k}"',
        '{{"api_key": "{k}"}}',
        "Authorization: Bearer {k}",
        "use ({k})",
        "list [{k}]",
        "OPENAI_KEY_{k}",   # underscore is a boundary (\b would miss this)
        "x-{k}",            # hyphen is a boundary
        "key-{k}",
    )

    def test_every_context_is_redacted_whole(self):
        for key in (FAKE_KEY, FAKE_PROJ_KEY):
            for ctx in self.CONTEXTS:
                text = ctx.format(k=key)
                with self.subTest(text=text):
                    result = filter_chat_secrets(text)
                    self.assertNotIn(key, result.text)
                    self.assertIn("OpenAI Token", result.secret_types)
                    self.assertEqual(
                        OPENAI_RULE.sub(PLACEHOLDER, text),
                        ctx.format(k=PLACEHOLDER),
                        "fallback rule must redact the key as one span",
                    )


class TestVendoredCopyMatches(unittest.TestCase):
    def test_package_copy_is_byte_identical(self):
        src = REPO / "src" / "chat_secret_filter.py"
        vendored = REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "chat_secret_filter.py"
        self.assertEqual(src.read_bytes(), vendored.read_bytes())


if __name__ == "__main__":
    unittest.main()
