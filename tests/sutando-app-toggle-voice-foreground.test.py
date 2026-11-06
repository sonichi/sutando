#!/usr/bin/env python3
import os
import re
import unittest
from pathlib import Path


class ToggleVoiceForegroundTest(unittest.TestCase):
    def test_toggle_voice_opens_web_ui_after_toggle(self):
        source_path = Path(
            os.environ.get("SUTANDO_MAIN_SWIFT", "src/Sutando/main.swift")
        )
        source = source_path.read_text()
        match = re.search(
            r"@objc func toggleVoice\(\) \{(?P<body>.*?)\n    \}",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        self.assertLess(
            body.index('httpToggle(endpoint: "toggle")'),
            body.index("openWebUI()"),
        )


if __name__ == "__main__":
    unittest.main()
