#!/usr/bin/env python3
"""In-process branch coverage for scripts/access-mutate.py (#3318 blocker 1).

tests/access-mutate-cli-concurrency.test.py drives the CLI as a genuine
subprocess but only exercises the single group-append-success path (it
exists to prove lock coordination, not branch coverage). This file calls
`main(argv)` directly — same production module, loaded via
importlib.util.spec_from_file_location (the established pattern for
hyphenated-filename modules, e.g. tests/telegram-privacy-setting-log.test.py)
— to reach the remaining branches: usage error, unknown command, both
commands' "group does not exist" and no-op paths, and a corrupt access.json.

Isolation: resolve_discord_access_file() honors $CLAUDE_CONFIG_DIR (via
util_paths.claude_home_path, read fresh on every call — safe to set/restore
per test), but discord_access_backup_file() resolves through
workspace_default.resolve_workspace(), which does NOT honor
$CLAUDE_CONFIG_DIR or $SUTANDO_WORKSPACE — only sutando.config.local.json /
sutando.config.json / a bare "<repo>/workspace" default. Setting only
CLAUDE_CONFIG_DIR would leave every successful mutation's best-effort
_backup() call writing into the real workspace's
state/auth/discord-access-backup.json. Every test case therefore also
monkeypatches access_store.resolve_workspace directly (the pattern
established in tests/discord-access-backup.test.py's
TestAccessPathResolution) — this is safe because discord_access_backup_file()
looks up resolve_workspace() via access_store's own module globals at call
time, regardless of which caller (bridge, this CLI, or a test) invoked it.

Run: python3 tests/access-mutate-cli.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import access_store  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "access_mutate_cli", REPO / "scripts" / "access-mutate.py"
)
access_mutate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(access_mutate)


class _IsolatedAccessFile(unittest.TestCase):
    """Base: isolates both the live access.json path (CLAUDE_CONFIG_DIR) and
    the durable backup path (access_store.resolve_workspace) per test."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="access-mutate-cli-"))
        self._old_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.d / "ccd")
        self._old_resolve_workspace = access_store.resolve_workspace
        access_store.resolve_workspace = lambda *a, **kw: self.d / "workspace"
        self.access_file = self.d / "ccd" / "channels" / "discord" / "access.json"
        self.backup_file = self.d / "workspace" / "state" / "auth" / "discord-access-backup.json"

    def tearDown(self):
        if self._old_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._old_config_dir
        access_store.resolve_workspace = self._old_resolve_workspace

    def _write_access(self, doc):
        self.access_file.parent.mkdir(parents=True, exist_ok=True)
        self.access_file.write_text(json.dumps(doc))

    def _run(self, argv):
        """Call main(argv) in-process, capturing stdout/stderr and rc."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = access_mutate.main(argv)
        return rc, out.getvalue(), err.getvalue()


class TestArgParsing(_IsolatedAccessFile):
    def test_usage_error_too_few_args(self):
        rc, out, err = self._run(["access-mutate.py", "group-append", "chan"])
        self.assertEqual(rc, 1)
        self.assertIn("usage:", err)
        self.assertEqual(out, "")

    def test_no_args_at_all(self):
        rc, out, err = self._run(["access-mutate.py"])
        self.assertEqual(rc, 1)
        self.assertIn("usage:", err)

    def test_unknown_command(self):
        self._write_access({"dmPolicy": "pairing", "allowFrom": [], "groups": {}})
        rc, out, err = self._run(["access-mutate.py", "frobnicate", "chan", "s1"])
        self.assertEqual(rc, 1)
        self.assertIn("unknown command: 'frobnicate'", err)
        self.assertEqual(out, "")


class TestGroupAppend(_IsolatedAccessFile):
    def test_group_does_not_exist(self):
        self._write_access({"dmPolicy": "pairing", "allowFrom": [], "groups": {}})
        rc, out, err = self._run(["access-mutate.py", "group-append", "thread-1", "s1"])
        self.assertEqual(rc, 1)
        result = json.loads(out)
        self.assertFalse(result["ok"])
        self.assertIn("does not exist", result["error"])
        self.assertFalse(self.backup_file.exists(), "backup must not fire on a no-op")

    def test_success_and_durable_backup(self):
        self._write_access({
            "dmPolicy": "pairing", "allowFrom": [],
            "groups": {"thread-1": {"requireMention": False, "allowFrom": ["owner-1"]}},
        })
        rc, out, err = self._run(["access-mutate.py", "group-append", "thread-1", "newmember"])
        self.assertEqual(rc, 0, err)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["added"], ["newmember"])
        self.assertEqual(result["skipped"], [])
        final = json.loads(self.access_file.read_text())
        self.assertEqual(
            final["groups"]["thread-1"]["allowFrom"], ["owner-1", "newmember"]
        )
        # A successful mutation backs up to the ISOLATED workspace, never the
        # real one — the whole point of the resolve_workspace monkeypatch.
        self.assertTrue(self.backup_file.exists())
        backed_up = json.loads(self.backup_file.read_text())
        self.assertEqual(backed_up["groups"]["thread-1"]["allowFrom"], ["owner-1", "newmember"])

    def test_all_already_present_is_noop(self):
        self._write_access({
            "dmPolicy": "pairing", "allowFrom": [],
            "groups": {"thread-1": {"requireMention": False, "allowFrom": ["owner-1"]}},
        })
        rc, out, err = self._run(["access-mutate.py", "group-append", "thread-1", "owner-1"])
        self.assertEqual(rc, 0, err)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["added"], [])
        self.assertEqual(result["skipped"], ["owner-1"])
        self.assertFalse(self.backup_file.exists(), "no-op must not write a stale backup")

    def test_backup_skipped_when_doc_missing_allow_from(self):
        """A doc without a top-level allowFrom list fails _backup's own
        validity guard — the mutation must still succeed even though the
        best-effort backup silently declines to write."""
        self._write_access({
            "dmPolicy": "pairing",
            "groups": {"thread-1": {"requireMention": False, "allowFrom": ["owner-1"]}},
        })
        rc, out, err = self._run(["access-mutate.py", "group-append", "thread-1", "newmember"])
        self.assertEqual(rc, 0, err)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["added"], ["newmember"])
        self.assertFalse(self.backup_file.exists(), "guard must skip a doc without allowFrom")

    def test_backup_oserror_is_swallowed(self):
        """A failed best-effort backup write must not fail the mutation
        itself — mirrors discord-bridge.py's _backup_access_to_disk."""
        self._write_access({
            "dmPolicy": "pairing", "allowFrom": [],
            "groups": {"thread-1": {"requireMention": False, "allowFrom": ["owner-1"]}},
        })
        # workspace/state is a plain file, so mkdir(parents=True) on
        # state/auth/ raises OSError inside _backup's try block.
        (self.d / "workspace").mkdir(parents=True, exist_ok=True)
        (self.d / "workspace" / "state").write_text("not a directory")
        rc, out, err = self._run(["access-mutate.py", "group-append", "thread-1", "newmember"])
        self.assertEqual(rc, 0, err)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["added"], ["newmember"])


class TestGroupRmAllow(_IsolatedAccessFile):
    def test_group_does_not_exist(self):
        self._write_access({"dmPolicy": "pairing", "allowFrom": [], "groups": {}})
        rc, out, err = self._run(["access-mutate.py", "group-rm-allow", "thread-1", "s1"])
        self.assertEqual(rc, 1)
        result = json.loads(out)
        self.assertFalse(result["ok"])
        self.assertIn("does not exist", result["error"])

    def test_success(self):
        self._write_access({
            "dmPolicy": "pairing", "allowFrom": [],
            "groups": {"thread-1": {"requireMention": False, "allowFrom": ["owner-1", "gone"]}},
        })
        rc, out, err = self._run(["access-mutate.py", "group-rm-allow", "thread-1", "gone"])
        self.assertEqual(rc, 0, err)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], ["gone"])
        final = json.loads(self.access_file.read_text())
        self.assertEqual(final["groups"]["thread-1"]["allowFrom"], ["owner-1"])
        self.assertTrue(self.backup_file.exists())

    def test_all_already_absent_is_noop(self):
        self._write_access({
            "dmPolicy": "pairing", "allowFrom": [],
            "groups": {"thread-1": {"requireMention": False, "allowFrom": ["owner-1"]}},
        })
        rc, out, err = self._run(["access-mutate.py", "group-rm-allow", "thread-1", "never-there"])
        self.assertEqual(rc, 0, err)
        result = json.loads(out)
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["skipped"], ["never-there"])
        self.assertFalse(self.backup_file.exists())


class TestCorruptAccessFile(_IsolatedAccessFile):
    def test_corrupt_json_not_modified_group_append(self):
        self.access_file.parent.mkdir(parents=True, exist_ok=True)
        self.access_file.write_text("{not valid json")
        before = self.access_file.read_text()
        rc, out, err = self._run(["access-mutate.py", "group-append", "thread-1", "s1"])
        self.assertEqual(rc, 1)
        result = json.loads(out)
        self.assertFalse(result["ok"])
        self.assertIn("unreadable/corrupt", result["error"])
        self.assertEqual(self.access_file.read_text(), before, "corrupt file must not be touched")
        self.assertFalse(self.backup_file.exists())

    def test_corrupt_json_not_modified_group_rm_allow(self):
        self.access_file.parent.mkdir(parents=True, exist_ok=True)
        self.access_file.write_text("{not valid json")
        before = self.access_file.read_text()
        rc, out, err = self._run(["access-mutate.py", "group-rm-allow", "thread-1", "s1"])
        self.assertEqual(rc, 1)
        result = json.loads(out)
        self.assertFalse(result["ok"])
        self.assertIn("unreadable/corrupt", result["error"])
        self.assertEqual(self.access_file.read_text(), before, "corrupt file must not be touched")
        self.assertFalse(self.backup_file.exists())


if __name__ == "__main__":
    _r = unittest.main(exit=False)
    try:
        import coverage

        _cov = coverage.Coverage.current()
        if _cov is not None:
            _cov.save()
    except Exception:
        pass
    sys.exit(0 if _r.result.wasSuccessful() else 1)
