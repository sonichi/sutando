#!/usr/bin/env python3
"""docs/built-in-tools.md is the capability catalog CLAUDE.md sends the agent to before refusing:
it must carry switch-model (the chat triggers, the verbatim `switched:` report, the current model
from state/model-switch.json, the core-model card), check-quota (state/quota-state.json's fields,
the 6-hour staleness rule), generate-an-image and activate-a-cloud-tool entries."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = (REPO / "docs" / "built-in-tools.md").read_text(encoding="utf-8")
FLAT = re.sub(r"\s+", " ", DOC)


class Catalog(unittest.TestCase):
    def test_switch_model_names_its_chat_triggers_and_the_verbatim_report(self):
        self.assertIn("## Switch model", DOC)
        for trigger in ("`/model <x>` typed in chat", "switch to opus", "which model are you on"):
            self.assertIn(trigger, FLAT, trigger)
        self.assertIn("Report the script's `switched:` line verbatim", FLAT)
        self.assertIn("`<workspace>/state/model-switch.json`", FLAT)
        self.assertIn("`core-model` local card", FLAT)
        self.assertIn("scripts/switch-model.sh", DOC)
        self.assertIn("skills/model-switch/", DOC)

    def test_check_quota_reads_the_state_file_and_treats_six_hours_as_unknown(self):
        self.assertIn("## Check quota", DOC)
        self.assertIn("`<workspace>/state/quota-state.json`", FLAT)
        for field in ("utilization_5h", "utilization_7d", "resets_at_5h", "last_checked"):
            self.assertIn(f"`{field}`", FLAT, field)
        self.assertIn("older than 6 hours, or no file, means the quota is **unknown**", FLAT)
        self.assertIn("skills/quota-tracker", DOC)

    def test_generate_an_image_and_activate_a_cloud_tool_are_listed(self):
        self.assertIn("**Generate an image**", DOC)
        self.assertIn("skills/image-generation/scripts/generate.py --prompt", DOC)
        self.assertIn("`[file: <path>]`", DOC)
        self.assertIn("no_key|refused|no_image|api_error", FLAT)
        self.assertIn("**Activate a cloud tool**", DOC)
        self.assertIn("usable at once through `station_find` / `station_call`", FLAT)
        self.assertIn("`RESTART REQUIRED`", FLAT)

    def test_the_catalog_is_what_claude_md_points_at(self):
        for name in ("CLAUDE.md", "AGENTS.md"):
            s = (REPO / name).read_text(encoding="utf-8")
            self.assertIn("docs/built-in-tools.md", s, name)


if __name__ == "__main__":
    unittest.main(verbosity=1)
