#!/usr/bin/env python3
"""hooks/gdocs-write-guard.py: a whole-document Google Docs replace is denied until the document
was read in the last 15 minutes; every read is kept as a snapshot; partial edits, other toolkits
and other tools pass; the hook fails open. Drives the real script over stdin the way Claude Code
does, with SUTANDO_WORKSPACE_DIR pointed at a temp workspace.

Run: python3 tests/gdocs-write-guard.test.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "gdocs-write-guard.py"
EXEC = "mcp__sutando-station__composio_exec"
DOC = "1I08zjmaxqibo1pYmUn2B7jl3MLN9bNuWcFNV7B5aPUg"


def run(payload, ws, env_extra=None):
    env = dict(os.environ, SUTANDO_WORKSPACE_DIR=str(ws))
    env.pop("SUTANDO_ALLOW_GDOCS_WHOLE_REPLACE", None)
    env.pop("SUTANDO_GDOCS_BACKUP_MAX_AGE_S", None)
    env.update(env_extra or {})
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True,
                          timeout=30, env=env)


def decision(r):
    try:
        return json.loads(r.stdout)["hookSpecificOutput"]
    except (ValueError, KeyError):
        return None


def pre(action, arguments=None, toolkit="googledocs", tool=EXEC):
    return {"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": tool,
            "tool_input": {"toolkit": toolkit, "action": action, "arguments": arguments or {"document_id": DOC}}}


def post(action, response, arguments=None, toolkit="googledocs"):
    return {"hook_event_name": "PostToolUse", "session_id": "s", "tool_name": EXEC,
            "tool_input": {"toolkit": toolkit, "action": action, "arguments": arguments or {"document_id": DOC}},
            "tool_response": response}


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def backups(self):
        d = self.ws / "data" / "gdocs-backups" / DOC
        return sorted(d.iterdir()) if d.is_dir() else []

    def test_whole_replace_without_a_read_is_denied_with_the_partial_actions_named(self):
        # user feedback 2026-09-20: "the document sometimes gets unexpectedly cleared".
        for action in ("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN", "GOOGLEDOCS_UPDATE_EXISTING_DOCUMENT",
                       "GOOGLEDOCS_DELETE_CONTENT_RANGE"):
            with self.subTest(action):
                r = run(pre(action), self.ws)
                d = decision(r)
                self.assertEqual(r.returncode, 0)
                self.assertEqual(d["permissionDecision"], "deny", r.stdout)
                reason = d["permissionDecisionReason"]
                self.assertIn(DOC, reason)
                self.assertIn("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", reason)
                self.assertIn("GOOGLEDOCS_REPLACE_ALL_TEXT", reason)
                self.assertIn("SUTANDO_ALLOW_GDOCS_WHOLE_REPLACE", reason)

    def test_a_read_is_snapshotted_and_then_the_replace_is_allowed(self):
        r = run(post("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT",
                     {"document_id": DOC, "plain_text": "user feedback's User Feedback\nBugs\n1. Multi-image upload"}),
                self.ws)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), "a PostToolUse hook stays silent")
        files = self.backups()
        self.assertEqual(len(files), 1)
        self.assertIn("Multi-image upload", files[0].read_text())
        r = run(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"), self.ws)
        self.assertEqual((r.returncode, decision(r)), (0, None), r.stdout)

    def test_a_stale_snapshot_does_not_count(self):
        run(post("GOOGLEDOCS_GET_DOCUMENT_BY_ID", [{"type": "text", "text": "{\"body\": \"old\"}"}]), self.ws)
        (f,) = self.backups()
        old = time.time() - 3600
        os.utime(f, (old, old))
        self.assertEqual(decision(run(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"), self.ws))["permissionDecision"], "deny")
        # A longer window, set by the owner, brings it back.
        r = run(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"), self.ws, {"SUTANDO_GDOCS_BACKUP_MAX_AGE_S": "7200"})
        self.assertIsNone(decision(r))

    def test_the_document_id_can_be_a_url_and_the_snapshot_is_keyed_by_the_id(self):
        url = f"https://docs.google.com/document/d/{DOC}/edit?tab=t.0"
        run(post("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", "the text", arguments={"id": url}), self.ws)
        self.assertEqual(len(self.backups()), 1)
        r = run(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN", arguments={"id": url}), self.ws)
        self.assertIsNone(decision(r))
        # Another document's read never vouches for this one.
        r = run(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN", arguments={"document_id": "other-doc-id"}), self.ws)
        self.assertEqual(decision(r)["permissionDecision"], "deny")

    def test_snapshots_are_capped_at_the_newest_twenty(self):
        for i in range(23):
            run(post("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", f"v{i}"), self.ws)
        files = self.backups()
        self.assertEqual(len(files), 20)
        self.assertEqual(max(files, key=lambda p: p.stat().st_mtime).read_text(), "v22")

    def test_partial_edits_reads_and_other_toolkits_and_tools_pass(self):
        cases = [pre("GOOGLEDOCS_INSERT_TEXT_ACTION"), pre("GOOGLEDOCS_REPLACE_ALL_TEXT"),
                 pre("GOOGLEDOCS_INSERT_TEXT_IN_TABLE_CELL"), pre("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT"),
                 pre("GOOGLEDOCS_UPDATE_DOCUMENT_STYLE"), pre("GOOGLEDOCS_CREATE_DOCUMENT_MARKDOWN"),
                 pre("GOOGLESHEETS_UPDATE_DOCUMENT_MARKDOWN", toolkit="googlesheets"),
                 pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN", tool="mcp__sutando-station__composio_find"),
                 {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}]
        for payload in cases:
            with self.subTest(payload.get("tool_input")):
                r = run(payload, self.ws)
                self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stdout)
        self.assertEqual(self.backups(), [], "a Pre event never writes a snapshot")

    def test_escape_hatch_lifts_the_guard(self):
        r = run(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"), self.ws, {"SUTANDO_ALLOW_GDOCS_WHOLE_REPLACE": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))

    def test_an_unnamed_document_is_denied_and_garbage_fails_open(self):
        r = run(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN", arguments={"markdown": "# new"}), self.ws)
        self.assertEqual(decision(r)["permissionDecision"], "deny")
        self.assertIn("id not named", decision(r)["permissionDecisionReason"])
        r = run("this is not json", self.ws)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))

    def test_the_launcher_registers_the_hook(self):
        launch = (HOOK.parent.parent / "src" / "agent" / "claude" / "cli" / "session-launch.sh").read_text()
        self.assertIn('"$REPO/hooks/gdocs-write-guard.py"', launch)


if __name__ == "__main__":
    unittest.main(verbosity=1)
