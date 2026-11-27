#!/usr/bin/env python3
"""An installed agent's own workspace is sendable: a `[file:]` marker for a file under
<agents dir>/<agent>/workspace (the folder the Antigravity connector's prompt names) is not refused
as "path not allowlisted" by the agent's bridge, however the path reaches it — plainly, through a
symlinked agents dir (a desktop layout), or as its realpath. A link in the workspace that leaves it,
a file outside it, and the same folder in a layout that is not an installed agent's stay refused.

Regression: Chi's Antigravity agent attached a PDF it had copied into its workspace; the bridge's
allowlist held only results/, so it posted "[attachment not sent: ... (path not allowlisted)]".
Run: python3 tests/sparrow-agent-workspace-sendable.test.py"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "packages" / "ag2-sparrow"))

from ag2_sparrow import _dirs, proactive_routing, send_allowlist  # noqa: E402
from ag2_sparrow.adapters.antigravity import cli as agy_cli  # noqa: E402

read_attachment = proactive_routing.read_attachment

AGENT = "probe-antigravity.agent_s.space"


class AgentWorkspaceSendable(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="agent-ws-send-")))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(setattr, _dirs, "_injected", _dirs._injected)
        self.real_agents = self.tmp / "real-agents"
        self.root = self.real_agents / AGENT
        for d in agy_cli.WORK_DIRS:
            (self.root / d).mkdir(parents=True)
        self.workspace = self.root / agy_cli.DEFAULT_CWD
        self.pdf = self.workspace / "report.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 probe")
        # The desktop-like layout: the agents dir the bridge is given is a symlink to the real one.
        self.linked_agents = self.tmp / "linked-agents"
        os.symlink(self.real_agents, self.linked_agents)
        self.outside = self.tmp / "secret.txt"
        self.outside.write_text("not the agent's")

    def bridge_dirs(self, agents):
        """The dirs an installed agent's bridge runs with (connect_bridge.bridge_env), under `agents`."""
        root = agents / AGENT
        _dirs.set_dirs(task_dir=root / "tasks", result_dir=root / "results", state_dir=self.tmp / "bridge-state",
                       agents_dir=agents)

    def test_the_workspace_is_the_one_the_connector_works_in(self):
        self.assertEqual(agy_cli.DEFAULT_CWD, _dirs.WORKSPACE_DIRNAME)
        self.bridge_dirs(self.real_agents)
        self.assertEqual(_dirs.agent_workspace_dir(), self.workspace)

    def test_a_workspace_file_is_sent_by_plain_path_linked_parent_and_realpath(self):
        for agents in (self.real_agents, self.linked_agents):
            self.bridge_dirs(agents)
            for path in (str(agents / AGENT / "workspace" / "report.pdf"),
                         str(self.linked_agents / AGENT / "workspace" / "report.pdf"),
                         os.path.realpath(self.pdf)):
                with self.subTest(agents=agents.name, path=path):
                    self.assertTrue(send_allowlist.is_path_sendable(path))
                    _, data, reason = read_attachment(path, send_allowlist.is_path_sendable, 1 << 20)
                    self.assertEqual((data, reason), (b"%PDF-1.4 probe", ""))

    def test_a_link_out_of_the_workspace_is_refused(self):
        os.symlink(self.outside, self.workspace / "escape.txt")
        for agents in (self.real_agents, self.linked_agents):
            self.bridge_dirs(agents)
            path = str(agents / AGENT / "workspace" / "escape.txt")
            self.assertFalse(send_allowlist.is_path_sendable(path))
            self.assertEqual(read_attachment(path, send_allowlist.is_path_sendable, 1 << 20)[1:],
                             (None, "path not allowlisted"))

    def test_a_file_outside_the_workspace_is_refused(self):
        self.bridge_dirs(self.linked_agents)
        sibling = self.root / "state" / "x.txt"
        sibling.write_text("state")
        prefix_twin = self.root / "workspace-evil" / "x.txt"
        prefix_twin.parent.mkdir()
        prefix_twin.write_text("twin")
        for path in (self.outside, sibling, prefix_twin, self.workspace):
            with self.subTest(path=path):
                self.assertFalse(send_allowlist.is_path_sendable(str(path)))

    def test_a_workspace_link_is_not_a_workspace(self):
        self.bridge_dirs(self.real_agents)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "a.txt").write_text("a")
        self.workspace.rename(self.tmp / "old-workspace")
        os.symlink(elsewhere, self.workspace)
        self.assertIsNone(_dirs.agent_workspace_dir())
        self.assertFalse(send_allowlist.is_path_sendable(str(self.workspace / "a.txt")))

    def test_no_workspace_outside_an_installed_agent_layout(self):
        # A root that is not directly under the agents dir (e.g. an embedding app's own workspace).
        self.bridge_dirs(self.real_agents)
        _dirs.set_dirs(task_dir=self.root / "tasks", result_dir=self.root / "results",
                       agents_dir=self.tmp / "other-agents")
        self.assertIsNone(_dirs.agent_workspace_dir())
        self.assertFalse(send_allowlist.is_path_sendable(str(self.pdf)))
        # tasks/ and results/ of different roots.
        _dirs.set_dirs(task_dir=self.tmp / "tasks", result_dir=self.root / "results", agents_dir=self.real_agents)
        self.assertIsNone(_dirs.agent_workspace_dir())
        # A result dir not named results/.
        _dirs.set_dirs(task_dir=self.root / "tasks", result_dir=self.root / "out", agents_dir=self.real_agents)
        self.assertIsNone(_dirs.agent_workspace_dir())
        # No workspace folder at all (a claude-local or codex root).
        _dirs.set_dirs(task_dir=self.root / "tasks", result_dir=self.root / "results", agents_dir=self.real_agents)
        shutil.rmtree(self.workspace)
        self.assertIsNone(_dirs.agent_workspace_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
