#!/usr/bin/env python3
"""The owner's `[file:]` for a file in a local Antigravity agent's workspace is sent: the connector copies it
into results/ (the one folder the agent's bridge sends from) and names the copy. The bridge's allowlist is
unchanged: a workspace path itself is still refused there, for every route that reads through it.

Regression: Chi's agy agent attached a PDF from its workspace and Sparrow posted
"[attachment not sent: ... (path not allowlisted)]". Run: python3 tests/agy-owner-attach-staged.test.py"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

BASE = Path(os.path.realpath(tempfile.mkdtemp(prefix="agy-attach-")))
REAL_AGENTS = BASE / "real-agents"
ROOT = REAL_AGENTS / "probe-antigravity.agent_s.space"
os.environ.update(AGENT_CONNECT_TASK_DIR=str(ROOT / "tasks"), AGENT_CONNECT_RESULT_DIR=str(ROOT / "results"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "ag2-sparrow"))
from ag2_sparrow import acp_host, proactive_routing, send_allowlist  # noqa: E402
from ag2_sparrow.adapters.antigravity import antigravity_connector as gc  # noqa: E402

parse_markers = acp_host.parse_markers
OWNER, TEAM = {"access_tier": "owner"}, {"access_tier": "team"}


def bridge_reads(path):
    """What the agent's bridge does with an attach (result and proactive routes): its bytes, or why not."""
    return proactive_routing.read_attachment(path, send_allowlist.is_path_sendable, 1 << 20)[1:]


class OwnerAttachStaged(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(BASE, ignore_errors=True)
        for d in ("tasks", "results", "state", "workspace"):
            (ROOT / d).mkdir(parents=True)
        self.workspace = ROOT / "workspace"
        (self.workspace / "report.pdf").write_bytes(b"%PDF-1.4 probe")
        self.linked_agents = BASE / "linked-agents"     # a desktop-like symlinked agents dir
        os.symlink(REAL_AGENTS, self.linked_agents)
        (BASE / "secret.txt").write_text("not the agent's")
        self.staged_root = Path(os.path.realpath(ROOT / "results" / gc.SENT_FILES_DIR))

    def connector(self, cwd):
        c = object.__new__(gc.Connector)
        c.name, c.args = gc.NAME, SimpleNamespace(cwd=str(cwd), results=str(ROOT / "results"))
        return c

    def attaches(self, body):
        return [a.value for a in parse_markers(body).actions if a.kind == "attach"]

    def test_a_workspace_file_is_staged_into_results_and_sent_by_the_unchanged_bridge(self):
        linked_ws = self.linked_agents / ROOT.name / "workspace"
        for cwd in (self.workspace, linked_ws):
            for ref in ("report.pdf", str(cwd / "report.pdf"), str(linked_ws / "report.pdf"),
                        os.path.realpath(self.workspace / "report.pdf")):
                with self.subTest(cwd=str(cwd), ref=ref):
                    body = self.connector(cwd).answer_body(OWNER, "completed", "[file: %s]\nhere" % ref, "end_turn")
                    [copy] = self.attaches(body)
                    copy = Path(copy)
                    self.assertEqual((copy.name, copy.parent.parent), ("report.pdf", self.staged_root))
                    self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(copy.parent.stat().st_mode), 0o700)
                    self.assertEqual(bridge_reads(str(copy)), (b"%PDF-1.4 probe", ""))
        self.assertEqual((self.workspace / "report.pdf").read_bytes(), b"%PDF-1.4 probe", "copied, not moved")

    def test_the_same_name_twice_gets_two_copies(self):
        (self.workspace / "sub").mkdir()
        (self.workspace / "sub" / "report.pdf").write_bytes(b"other")
        body = self.connector(self.workspace).answer_body(
            OWNER, "completed", "[file: report.pdf]\n[file: sub/report.pdf]", "end_turn")
        copies = [Path(p) for p in self.attaches(body)]
        self.assertEqual([p.name for p in copies], ["report.pdf", "report.pdf"])
        self.assertEqual([p.read_bytes() for p in copies], [b"%PDF-1.4 probe", b"other"])

    def test_the_bridge_still_refuses_a_workspace_path(self):
        # Result and proactive attachments both read through this allowlist; no seat's workspace is in it.
        for path in (self.workspace / "report.pdf", self.linked_agents / ROOT.name / "workspace" / "report.pdf"):
            self.assertFalse(send_allowlist.is_path_sendable(str(path)))
            self.assertEqual(bridge_reads(str(path)), (None, "path not allowlisted"))
        other = REAL_AGENTS / "probe-codex.agent_s.space" / "workspace" / "x.txt"
        other.parent.mkdir(parents=True)
        other.write_text("x")
        self.assertEqual(bridge_reads(str(other)), (None, "path not allowlisted"))
        self.assertNotIn(str(self.workspace), send_allowlist._allowed_roots())

    def test_a_link_out_of_the_workspace_or_a_file_outside_is_not_staged(self):
        os.symlink(BASE / "secret.txt", self.workspace / "escape.txt")
        for ref in ("escape.txt", str(BASE / "secret.txt"), "../state", "nope.pdf"):
            with self.subTest(ref=ref):
                body = self.connector(self.workspace).answer_body(OWNER, "completed", "[file: %s]" % ref, "end_turn")
                self.assertEqual(self.attaches(body), [])
                self.assertIn("not attached", body)
        self.assertFalse(self.staged_root.exists())

    def test_non_owner_turns_are_unchanged(self):
        body = self.connector(self.workspace).answer_body(TEAM, "completed", "[file: report.pdf]", "end_turn")
        self.assertEqual(self.attaches(body), [])
        self.assertFalse(self.staged_root.exists())

    def test_a_file_over_the_limit_or_one_that_cannot_be_copied_is_said_not_attached(self):
        big = self.workspace / "big.bin"
        big.write_bytes(b"\0" * ((1 << 20) + 1))
        self.assertEqual(acp_host.stage_attachment(str(big), str(self.workspace), str(self.staged_root), 1 << 20),
                         (None, "over 1 MB"))
        failing = mock.patch.object(acp_host, "_write_copy", side_effect=OSError(28, "No space left on device"))
        with failing:
            body = self.connector(self.workspace).answer_body(OWNER, "completed", "[file: report.pdf]", "end_turn")
        self.assertEqual(self.attaches(body), [])
        self.assertIn("not attached: could not be copied (No space left on device)", body)
        self.assertEqual(os.listdir(self.staged_root), [], "the half-made folder is removed")

    def test_a_file_that_left_the_workspace_after_its_check_is_refused(self):
        src = os.path.realpath(self.workspace / "report.pdf")
        self.assertEqual(acp_host.stage_attachment(src, str(BASE / "elsewhere"), str(self.staged_root), 1 << 20),
                         (None, "it left the working directory"))
        self.assertEqual(acp_host.stage_attachment(str(self.workspace / "gone.pdf"), str(self.workspace),
                                                   str(self.staged_root), 1 << 20), (None, "missing"))

    def test_old_staged_copies_are_pruned(self):
        c = self.connector(self.workspace)
        old, _ = c.stage_sent_file(os.path.realpath(self.workspace / "report.pdf"))
        (self.staged_root / "stray-file").write_text("not a staged folder")
        os.utime(Path(old).parent, (1, 1))
        new, _ = c.stage_sent_file(os.path.realpath(self.workspace / "report.pdf"))
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        self.assertTrue((self.staged_root / "stray-file").exists())
        acp_host.prune_staged(str(BASE / "absent"), 0)     # nothing there: nothing to do


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(BASE, ignore_errors=True)
