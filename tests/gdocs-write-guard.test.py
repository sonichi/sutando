#!/usr/bin/env python3
"""hooks/gdocs-write-guard.py: a whole-document Google Docs replace is denied until the document
was read in the last 15 minutes; every SUCCESSFUL read is kept as a snapshot; a failed, errored or
empty read never vouches; partial edits, other toolkits and other tools pass; the hook fails open.
Loads the hook in-process and calls handle() with the workspace redirected through the real
resolver (SUTANDO_TEST_MODE + SUTANDO_WORKSPACE); one subprocess case drives it over stdin the
way Claude Code does.

Run: python3 tests/gdocs-write-guard.test.py
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "gdocs-write-guard.py"
EXEC = "mcp__sutando-station__composio_exec"
DOC = "1I08zjmaxqibo1pYmUn2B7jl3MLN9bNuWcFNV7B5aPUg"

_spec = importlib.util.spec_from_file_location("gdocs_write_guard", HOOK)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def pre(action, arguments=None, toolkit="googledocs", tool=EXEC):
    return {"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": tool,
            "tool_input": {"toolkit": toolkit, "action": action, "arguments": arguments or {"document_id": DOC}}}


def post(action, response, arguments=None, toolkit="googledocs"):
    return {"hook_event_name": "PostToolUse", "session_id": "s", "tool_name": EXEC,
            "tool_input": {"toolkit": toolkit, "action": action, "arguments": arguments or {"document_id": DOC}},
            "tool_response": response}


def envelope(successful=True, error=None, **data):
    return {"successful": successful, "error": error, "data": data}


def mcp(text):
    """The MCP result wrapper Claude Code hands a PostToolUse hook for a connector call."""
    return {"content": [{"type": "text", "text": text if isinstance(text, str) else json.dumps(text)}]}


class Guard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"SUTANDO_REPO_ROOT": str(REPO), "SUTANDO_TEST_MODE": "1",
                                           "SUTANDO_WORKSPACE": str(self.ws)})
        self.env.start()
        for key in (guard.ALLOW_KEY, guard.MAX_AGE_KEY):
            os.environ.pop(key, None)
        self.stderr = io.StringIO()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def handle(self, payload, now=None, argv=(), **env):
        with patch.dict(os.environ, env), redirect_stderr(self.stderr):
            return guard.handle(payload, now=now, argv=list(argv))

    def decision(self, payload, **kw):
        out = self.handle(payload, **kw)
        return None if out is None else out["hookSpecificOutput"]

    def backups(self):
        d = self.ws / "data" / "gdocs-backups" / DOC
        return sorted(d.iterdir()) if d.is_dir() else []

    def test_whole_replace_without_a_read_is_denied_with_the_partial_actions_named(self):
        # user feedback: "the document sometimes gets unexpectedly cleared".
        for action in ("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN", "GOOGLEDOCS_UPDATE_EXISTING_DOCUMENT",
                       "GOOGLEDOCS_DELETE_CONTENT_RANGE"):
            with self.subTest(action):
                d = self.decision(pre(action))
                self.assertEqual(d["permissionDecision"], "deny")
                reason = d["permissionDecisionReason"]
                self.assertIn(DOC, reason)
                self.assertIn("15 minutes", reason)
                self.assertIn("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", reason)
                self.assertIn("GOOGLEDOCS_REPLACE_ALL_TEXT", reason)
                self.assertIn("SUTANDO_ALLOW_GDOCS_WHOLE_REPLACE", reason)
                self.assertIn(str(self.ws / "data" / "gdocs-backups" / DOC), reason)

    def test_a_read_is_snapshotted_and_then_the_replace_is_allowed(self):
        text = "user feedback's User Feedback\nBugs\n1. Multi-image upload"
        self.assertIsNone(self.handle(post("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", mcp(envelope(plain_text=text)))),
                          "a PostToolUse hook stays silent")
        files = self.backups()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(), text, "the snapshot is the document text, not the envelope")
        self.assertIsNone(self.decision(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN")))

    def test_a_failed_read_never_vouches_for_a_whole_replace(self):
        # A transient 403 must not disarm the guard: the "snapshot" would be the error, not the doc.
        failed = [
            envelope(successful=False, error="403 The caller does not have permission"),
            mcp(envelope(successful=False, error="403 The caller does not have permission")),
            mcp({"success": False, "data": {"plain_text": "stale"}}),
            {"is_error": True, "content": [{"type": "text", "text": "timeout"}]},
            {"isError": True, "content": [{"type": "text", "text": "timeout"}]},
            json.dumps({"successful": True, "error": "quota exceeded", "data": {}}),
            envelope(successful=True),  # an empty read: nothing to restore
            mcp(envelope(plain_text="   \n")),
            [{"type": "text", "text": "  "}],
            "", None, [], [{"type": "image"}],
        ]
        for response in failed:
            with self.subTest(response):
                self.assertIsNone(self.handle(post("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", response)))
                self.assertEqual(self.backups(), [], "a failed or empty read is not a snapshot")
                self.assertEqual(self.decision(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"))["permissionDecision"], "deny")

    def test_every_successful_read_shape_is_a_snapshot(self):
        cases = [
            ("plain string", "the text", "the text"),
            ("content blocks", [{"type": "text", "text": "block one"}, "block two", {"type": "image"}], "block one\nblock two"),
            ("mcp wrapper over an envelope", mcp(envelope(plain_text="wrapped")), "wrapped"),
            ("top-level text", {"text": "top"}, "top"),
            ("structured document, kept as JSON", envelope(title="T", body={"content": []}), '"title": "T"'),
            ("non-serializable falls back to str", {"data": {"odd": {1, 2}}}, "odd"),
            ("empty content list, JSON kept", {"content": [], "plain_text": "after empty content"}, "after empty content"),
            ("a number", 7, "7"),
        ]
        for name, response, expect in cases:
            with self.subTest(name):
                for f in self.backups():
                    f.unlink()
                self.handle(post("GOOGLEDOCS_GET_DOCUMENT_BY_ID", response))
                (f,) = self.backups()
                self.assertIn(expect, f.read_text())

    def test_a_stale_snapshot_does_not_count_and_the_window_is_configurable(self):
        self.handle(post("GOOGLEDOCS_GET_DOCUMENT_BY_ID", [{"type": "text", "text": "{\"body\": \"old\"}"}]))
        (f,) = self.backups()
        old = time.time() - 3600
        os.utime(f, (old, old))
        self.assertEqual(self.decision(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"))["permissionDecision"], "deny")
        # A longer window, set by the owner, brings it back — and the reason names the window in force.
        self.assertIsNone(self.decision(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"),
                                        SUTANDO_GDOCS_BACKUP_MAX_AGE_S="7200"))
        d = self.decision(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"), SUTANDO_GDOCS_BACKUP_MAX_AGE_S="60")
        self.assertIn("1 minutes", d["permissionDecisionReason"])
        d = self.decision(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"), SUTANDO_GDOCS_BACKUP_MAX_AGE_S="90")
        self.assertIn("90 seconds", d["permissionDecisionReason"])
        # Garbage falls back to the default; a snapshot from the future never counts.
        d = self.decision(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"), SUTANDO_GDOCS_BACKUP_MAX_AGE_S="soon")
        self.assertIn("15 minutes", d["permissionDecisionReason"])
        self.assertIsNone(guard.fresh_backup(self.ws, DOC, now=old - 10, max_age=7200))
        # An emptied backup folder is the same as none.
        f.unlink()
        self.assertEqual(self.decision(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"))["permissionDecision"], "deny")

    def test_the_document_id_can_be_a_url_and_the_snapshot_is_keyed_by_the_id(self):
        url = f"https://docs.google.com/document/d/{DOC}/edit?tab=t.0"
        self.handle(post("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", "the text", arguments={"id": url}))
        self.assertEqual(len(self.backups()), 1)
        self.assertIsNone(self.decision(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN", arguments={"id": url})))
        # Another document's read never vouches for this one.
        d = self.decision(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN", arguments={"document_id": "other-doc-id"}))
        self.assertEqual(d["permissionDecision"], "deny")
        self.assertIsNone(guard.doc_id_of("not a dict"))

    def test_snapshots_are_capped_at_the_newest_twenty(self):
        for i in range(23):
            self.handle(post("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", f"v{i}"), now=1_000_000 + i)
        files = self.backups()
        self.assertEqual(len(files), 20)
        self.assertEqual(max(files, key=lambda p: p.stat().st_mtime).read_text(), "v22")
        # Pruning is best-effort: a listing error leaves the new snapshot in place.
        with patch.object(Path, "iterdir", side_effect=OSError("boom")):
            out = guard.write_backup(self.ws, DOC, "v23", now=1_000_100)
        self.assertEqual(out.read_text(), "v23")

    def test_partial_edits_reads_and_other_toolkits_and_tools_pass(self):
        cases = [pre("GOOGLEDOCS_INSERT_TEXT_ACTION"), pre("GOOGLEDOCS_REPLACE_ALL_TEXT"),
                 pre("GOOGLEDOCS_INSERT_TEXT_IN_TABLE_CELL"), pre("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT"),
                 pre("GOOGLEDOCS_UPDATE_DOCUMENT_STYLE"), pre("GOOGLEDOCS_CREATE_DOCUMENT_MARKDOWN"),
                 pre("GOOGLESHEETS_UPDATE_DOCUMENT_MARKDOWN", toolkit="googlesheets"),
                 pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN", tool="mcp__sutando-station__composio_find"),
                 post("GOOGLEDOCS_INSERT_TEXT_ACTION", "not a read"),
                 post("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", "unnamed doc", arguments={"markdown": "x"}),
                 {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}},
                 {"hook_event_name": "PreToolUse", "tool_name": EXEC, "tool_input": "not a dict"},
                 {"hook_event_name": "Notification", "tool_name": EXEC,
                  "tool_input": {"toolkit": "googledocs", "action": "GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"}}]
        for payload in cases:
            with self.subTest(payload.get("tool_input")):
                self.assertIsNone(self.handle(payload))
        self.assertEqual(self.backups(), [], "only a read writes a snapshot")

    def test_escape_hatch_lifts_the_guard(self):
        self.assertIsNone(self.handle(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"), SUTANDO_ALLOW_GDOCS_WHOLE_REPLACE="1"))

    def test_an_unnamed_document_is_denied(self):
        d = self.decision(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN", arguments={"markdown": "# new"}))
        self.assertEqual(d["permissionDecision"], "deny")
        self.assertIn("id not named", d["permissionDecisionReason"])

    def test_the_repo_root_is_configured_never_discovered(self):
        self.assertEqual(guard.repo_root(["--repo", "/a"]), "/a")
        self.assertEqual(guard.repo_root(["--repo=/b"]), "/b")
        self.assertEqual(guard.repo_root(["--repo"]), str(REPO), "a dangling flag falls back to the env")
        with patch.dict(os.environ, {"SUTANDO_REPO_ROOT": ""}):
            self.assertIsNone(guard.repo_root([]))
            # Without a root there is no workspace: a read records nothing, the replace stays denied.
            self.handle(post("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", "the text"))
            self.assertEqual(self.backups(), [])
            d = self.decision(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"))
            self.assertEqual(d["permissionDecision"], "deny")
            self.assertIn("data/gdocs-backups/<doc id>/", d["permissionDecisionReason"])
        self.assertIn("repo root not configured", self.stderr.getvalue())
        # The root from argv wins over the env, and handle() defaults to sys.argv.
        with patch.object(sys, "argv", [str(HOOK), "--repo", str(REPO)]):
            with redirect_stderr(self.stderr):
                self.assertIsNone(guard.handle(post("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", "argv root")))
        self.assertEqual(len(self.backups()), 1)

    def test_a_broken_resolver_or_config_never_wedges_the_hook(self):
        class Broken:
            @staticmethod
            def resolve_workspace():
                raise RuntimeError("no config here")

            @staticmethod
            def config_get(key):
                raise RuntimeError("no config here")

            config_get_env_first = config_get

        with patch.dict(sys.modules, {"workspace_default": Broken, "sutando_config": Broken}):
            self.handle(post("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", "the text"))
            self.assertEqual(self.backups(), [])
            d = self.decision(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"))
            self.assertEqual(d["permissionDecision"], "deny")
            self.assertIsNone(self.handle(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"),
                                          SUTANDO_ALLOW_GDOCS_WHOLE_REPLACE="1"),
                              "the escape hatch still reads the environment")
        err = self.stderr.getvalue()
        self.assertIn("workspace unresolved", err)
        self.assertIn("config unreadable", err)

    def test_main_prints_the_decision_and_exits_zero(self):
        for stdin, expect in ((json.dumps(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN")), "deny"),
                              (json.dumps(pre("GOOGLEDOCS_INSERT_TEXT_ACTION")), ""),
                              ("[1, 2]", ""), ("", "")):
            with self.subTest(stdin):
                out = io.StringIO()
                with patch.object(sys, "stdin", io.StringIO(stdin)), redirect_stdout(out), \
                        redirect_stderr(self.stderr), self.assertRaises(SystemExit) as cm:
                    guard.main(argv=["--repo", str(REPO)])
                self.assertEqual(cm.exception.code, 0)
                self.assertIn(expect, out.getvalue())
                if not expect:
                    self.assertEqual(out.getvalue(), "")

    def test_the_script_over_stdin_the_way_claude_code_runs_it(self):
        def run(payload, **env_extra):
            env = dict(os.environ, **env_extra)
            stdin = payload if isinstance(payload, str) else json.dumps(payload)
            return subprocess.run([sys.executable, str(HOOK), "--repo", str(REPO)], input=stdin,
                                  capture_output=True, text=True, timeout=30, env=env)

        r = run(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        r = run(post("GOOGLEDOCS_GET_DOCUMENT_PLAINTEXT", mcp(envelope(plain_text="from a subprocess"))))
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertEqual(len(self.backups()), 1)
        r = run(pre("GOOGLEDOCS_UPDATE_DOCUMENT_MARKDOWN"))
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        r = run("this is not json")
        self.assertEqual((r.returncode, r.stdout), (0, ""), "garbage fails open")
        self.assertIn("non-fatal", r.stderr)

    def test_the_launcher_registers_the_hook(self):
        launch = (REPO / "src" / "agent" / "claude" / "cli" / "session-launch.sh").read_text()
        self.assertIn('"$REPO/hooks/gdocs-write-guard.py"', launch)


if __name__ == "__main__":
    unittest.main(verbosity=1)
