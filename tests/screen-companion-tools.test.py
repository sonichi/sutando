#!/usr/bin/env python3
"""
Tests for screen-companion tool additions (#819):
- vision_query / take_note / look_up_reference registration in tools.ts
- captureSendFrame export in src/vision-tools.ts
- guided-setup.yaml PLANNED comments removed
- SKILL.md take_note doc updated

Pure static/grep tests — no TS runtime required.
"""

import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

PASS = 0
FAIL = 0


def grep(pattern: str, path: Path, fixed: bool = False) -> list[str]:
    flag = "-F" if fixed else "-E"
    result = subprocess.run(
        ["grep", "-n", flag, pattern, str(path)],
        capture_output=True, text=True,
    )
    return [l for l in result.stdout.splitlines() if l]


class TestToolsTs(unittest.TestCase):
    def setUp(self):
        self.tools_ts = REPO / "skills" / "screen-companion" / "tools.ts"
        self.assertTrue(self.tools_ts.exists(), f"tools.ts missing: {self.tools_ts}")
        self.content = self.tools_ts.read_text()

    def test_vision_query_tool_defined(self):
        self.assertIn("const visionQueryTool", self.content)

    def test_take_note_tool_defined(self):
        self.assertIn("const takeNoteTool", self.content)

    def test_look_up_reference_tool_defined(self):
        self.assertIn("const lookUpReferenceTool", self.content)

    def test_capture_send_frame_imported(self):
        self.assertIn("captureSendFrame", self.content)

    def test_resolve_workspace_defined(self):
        self.assertIn("function resolveWorkspace()", self.content)

    def test_fs_imports_present(self):
        self.assertIn("writeFileSync", self.content)
        self.assertIn("mkdirSync", self.content)

    def test_join_imported(self):
        self.assertIn("join", self.content)

    def test_all_three_tools_exported(self):
        # The export line should include all five tools now
        export_lines = [l for l in self.content.splitlines() if "export const tools" in l]
        self.assertTrue(export_lines, "no export const tools line found")
        export_line = export_lines[-1]
        self.assertIn("visionQueryTool", export_line)
        self.assertIn("takeNoteTool", export_line)
        self.assertIn("lookUpReferenceTool", export_line)

    def test_vision_query_execution_inline(self):
        # vision_query and take_note must be inline (instant response)
        lines = self.content.splitlines()
        in_vision_query = False
        found_inline = False
        for line in lines:
            if "const visionQueryTool" in line:
                in_vision_query = True
            if in_vision_query and "execution: 'inline'" in line:
                found_inline = True
                break
            if in_vision_query and "const takeNoteTool" in line:
                break
        self.assertTrue(found_inline, "vision_query missing execution: 'inline'")

    def test_take_note_writes_to_notes_dir(self):
        self.assertIn("notesDir", self.content)
        self.assertIn("notes", self.content)

    def test_ddg_api_used_for_reference_lookup(self):
        self.assertIn("duckduckgo.com", self.content)

    def test_frame_sent_status_returned(self):
        self.assertIn("frame_sent", self.content)

    def test_not_found_status_handled(self):
        self.assertIn("not_found", self.content)

    def test_screen_companion_tag_in_frontmatter(self):
        self.assertIn("screen-companion, observation", self.content)


class TestVisionToolsTs(unittest.TestCase):
    def setUp(self):
        self.vision_ts = REPO / "src" / "vision-tools.ts"
        self.assertTrue(self.vision_ts.exists())
        self.content = self.vision_ts.read_text()

    def test_capture_send_frame_exported(self):
        self.assertIn("export async function captureSendFrame", self.content)

    def test_capture_send_frame_uses_resolve_source(self):
        lines = self.content.splitlines()
        fn_start = None
        for i, line in enumerate(lines):
            if "export async function captureSendFrame" in line:
                fn_start = i
                break
        self.assertIsNotNone(fn_start, "captureSendFrame not found")
        fn_body = "\n".join(lines[fn_start:fn_start + 15])
        self.assertIn("resolveSource", fn_body)
        self.assertIn("captureAndSend", fn_body)

    def test_capture_send_frame_returns_source_name(self):
        self.assertIn("source: source.name", self.content)

    def test_no_push_mode_required(self):
        # The function should NOT check pushMode
        lines = self.content.splitlines()
        fn_start = None
        fn_end = None
        for i, line in enumerate(lines):
            if "export async function captureSendFrame" in line:
                fn_start = i
            if fn_start and i > fn_start and line.strip() == "}":
                fn_end = i
                break
        if fn_start and fn_end:
            fn_body = "\n".join(lines[fn_start:fn_end + 1])
            self.assertNotIn("pushMode", fn_body)


class TestGuidedSetupYaml(unittest.TestCase):
    def setUp(self):
        self.yaml = REPO / "skills" / "screen-companion" / "configs" / "guided-setup.yaml"
        self.assertTrue(self.yaml.exists())
        self.content = self.yaml.read_text()

    def test_planned_comments_removed(self):
        self.assertNotIn("PLANNED", self.content)

    def test_tools_allow_present(self):
        self.assertIn("tools_allow:", self.content)

    def test_vision_query_listed(self):
        self.assertIn("vision_query", self.content)

    def test_take_note_listed(self):
        self.assertIn("take_note", self.content)

    def test_look_up_reference_listed(self):
        self.assertIn("look_up_reference", self.content)

    def test_duckduckgo_mentioned_in_comment(self):
        self.assertIn("DuckDuckGo", self.content)


class TestSkillMd(unittest.TestCase):
    def setUp(self):
        self.skill_md = REPO / "skills" / "screen-companion" / "SKILL.md"
        self.assertTrue(self.skill_md.exists())
        self.content = self.skill_md.read_text()

    def test_take_note_no_longer_planned(self):
        self.assertNotIn("planned; see issue #797", self.content)

    def test_take_note_functional_doc(self):
        self.assertIn("take_note", self.content)
        self.assertIn("screen-companion", self.content)


if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in [TestToolsTs, TestVisionToolsTs, TestGuidedSetupYaml, TestSkillMd]:
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
