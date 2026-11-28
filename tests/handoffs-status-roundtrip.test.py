#!/usr/bin/env python3
"""Persisted handoffs consumed by the real activity status reader in fresh processes.

Run: python3 tests/handoffs-status-roundtrip.test.py -v (stdlib only)
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from _helpers.hermetic_env import hermetic_env, isolate_os_environ
from _helpers.subprocess_coverage import covered_code

isolate_os_environ(home=tempfile.mkdtemp(prefix="handoff-status-roundtrip-", dir="/tmp"))

REPO = Path(__file__).resolve().parents[1]
SCRATCH = Path(os.environ["HOME"])
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
PARENT_A = "task-ledger-parent-a"
PARENT_B = "task-ledger-parent-b"
CHILD = "task-ledger-child-a"
CONTROL = "task-without-handoff"
T0 = 1793491200
NOW = T0 + 90
CHILD_TIMEOUT_S = 15

PREAMBLE = """
import datetime
import json
import sys
from pathlib import Path
from unittest.mock import patch

repo = Path(sys.argv[1])
data = json.loads(sys.argv[2])
sys.path.insert(0, str(repo / "src"))
sys.path.insert(0, str(repo / "skills" / "agent-activity" / "scripts"))
import correlation_log as cl
import handoffs
import phase_line

class Clock(datetime.datetime):
    current = data["now"]

    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(cls.current, tz)

with patch.object(cl.datetime, "datetime", Clock), patch("time.time", lambda: Clock.current):
"""


class PersistedStatus(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="case-", dir=SCRATCH)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspaces = [self.root / "workspace-a", self.root / "workspace-b"]
        for name in ("home", "config", "claude", "codex", "tmp", "default-workspace"):
            (self.root / name).mkdir()
        for ws in self.workspaces:
            ws.mkdir()
        self.env = hermetic_env(
            home=self.root / "home", XDG_CONFIG_HOME=self.root / "config",
            CLAUDE_CONFIG_DIR=self.root / "claude", CODEX_HOME=self.root / "codex",
            TMPDIR=self.root / "tmp", SUTANDO_TEST_MODE="1",
            SUTANDO_WORKSPACE=self.root / "default-workspace", PHASE_MODEL="off",
            PYTHONDONTWRITEBYTECODE="1")
        self.ids = self.child("""
            ids = {}
            fixtures = [
                (0, data["parent_a"], "message", "ix_0123456789ab", "an agent", False, 0, "message"),
                (0, data["parent_a"], "pool", data["child"], "worker-a", True, 10, "pool"),
                (0, data["parent_b"], "agent", "toolu_01AbCdEf1234", "Explore", True, 20, "agent"),
                (1, data["parent_a"], "pool", data["child"], "worker-b", True, 40, "other_pool"),
                (1, data["parent_b"], "message", "ix_abcdef012345", "an agent", False, 50, "other_message"),
            ]
            for index, parent, kind, child, label, holds, offset, key in fixtures:
                Clock.current = data["base"] + offset
                hid = handoffs.open_handoff(data["workspaces"][index], parent, kind, child, label, holds=holds)
                assert hid is not None, key
                ids[key] = hid
            for index, line in enumerate(("Reviewing fixtures", "Checking fixture data")):
                ws = data["workspaces"][index]
                Clock.current = data["base"] + 60 + index
                assert phase_line.write(ws, {"task_id": data["child"], "seq": "child-step",
                                            "phase": "Reading files", "phase_source": "rules"})
                assert phase_line.write(ws, {"task_id": data["child"], "for_seq": "child-step",
                                            "phase": "Reviewing changes", "phase_source": "model", "line": line})
            print(json.dumps(ids))
        """)
        self.assertEqual(len(set(self.ids.values())), 5)

    def child(self, code, **extra):
        data = {"now": NOW, "base": T0, "parent_a": PARENT_A, "parent_b": PARENT_B,
                "child": CHILD, "workspaces": [str(ws) for ws in self.workspaces]}
        data.update(extra)
        program = PREAMBLE + textwrap.indent(textwrap.dedent(code), "    ")
        result = subprocess.run(
            [sys.executable, "-c", covered_code(program), str(REPO), json.dumps(data)],
            cwd=self.root, env=self.env, capture_output=True, text=True, timeout=CHILD_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def read(self, workspace, parent, now=NOW):
        return self.child("""
            ws, task, now = Path(data["workspace"]), data["parent"], data["now"]
            local = phase_line.status_for(ws, task, now=now)
            published = [phase_line.published_status(ws, task, share_line=share, now=now)
                         for share in (False, True)]
            print(json.dumps({"local": local, "published": published,
                              "open": handoffs.open_handoffs(ws, task, now=now),
                              "delegated": handoffs.delegated_status(ws, task, now=now),
                              "closed": cl.read("handoff_closed", workspace=ws)}))
        """, workspace=str(workspace), parent=parent, now=now)

    def assert_delegated(self, actual, line, since, ids, holding):
        self.assertEqual(actual["local"], {"phase": "Delegated", "line": line, "since": since,
                                           "waiting": {"reason": "delegated"}})
        public = {"phase": "Delegated", "summary": "Delegated", "since": since,
                  "waiting": {"reason": "delegated"}}
        self.assertEqual(actual["published"], [public, public])
        self.assertEqual([h["handoff_id"] for h in actual["open"]], ids)
        holding_since = {self.ids["pool"]: T0 + 10, self.ids["agent"]: T0 + 20,
                         self.ids["other_pool"]: T0 + 40}
        starts = [holding_since[hid] for hid in ids if hid in holding_since]
        self.assertEqual(actual["delegated"], {
            "phase": "Delegated", "line": line, "handoffs": len(ids), "holding": holding,
            "deadline": min(starts) + 86400 if starts else None})

    def assert_no_handoff(self, actual):
        self.assertIsNone(actual["local"])
        self.assertEqual(actual["published"], [None, None])
        self.assertEqual(actual["open"], [])
        self.assertIsNone(actual["delegated"])

    def test_real_consumer_selects_parent_and_workspace_and_preserves_since(self):
        cases = [
            (0, PARENT_A, "Delegated: 2 helpers, reviewing fixtures", T0,
             [self.ids["message"], self.ids["pool"]], 1),
            (0, PARENT_B, "Delegated: Explore", T0 + 20, [self.ids["agent"]], 1),
            (1, PARENT_A, "Delegated: worker-b, checking fixture data", T0 + 40,
             [self.ids["other_pool"]], 1),
            (1, PARENT_B, "Delegated: an agent", T0 + 50, [self.ids["other_message"]], 0),
        ]
        for index, parent, line, since, ids, holding in cases:
            with self.subTest(workspace=index, parent=parent):
                actual = self.read(self.workspaces[index], parent)
                self.assert_delegated(actual, line, since, ids, holding)
                self.assertEqual(actual["closed"], [])
        later = self.read(self.workspaces[0], PARENT_A, now=NOW + 30)
        self.assert_delegated(later, "Delegated: 2 helpers, reviewing fixtures", T0,
                              [self.ids["message"], self.ids["pool"]], 1)

    def test_ready_pool_result_closes_once_without_affecting_siblings_or_other_workspace(self):
        before = self.read(self.workspaces[0], PARENT_A)
        self.assert_delegated(before, "Delegated: 2 helpers, reviewing fixtures", T0,
                              [self.ids["message"], self.ids["pool"]], 1)
        published = self.child("""
            import result_publish
            from task_lookup import find_ready_result
            ws = Path(data["workspace"])
            target = result_publish.publish_text(ws / "results" / (data["child"] + ".txt"), data["body"])
            ready = find_ready_result(ws, data["child"])
            assert ready is not None
            print(json.dumps({"target": str(target), "ready": str(ready), "body": ready.read_text(),
                              "closed": cl.read("handoff_closed", workspace=ws)}))
        """, workspace=str(self.workspaces[0]), body="Reviewed the fixture successfully.\n")
        target = str(self.workspaces[0] / "results" / (CHILD + ".txt"))
        self.assertEqual(published, {"target": target, "ready": target,
                                     "body": "Reviewed the fixture successfully.\n", "closed": []})
        for attempt in range(3):
            with self.subTest(read=attempt):
                actual = self.read(self.workspaces[0], PARENT_A, now=NOW + attempt)
                self.assert_delegated(actual, "Delegated: an agent", T0, [self.ids["message"]], 0)
                self.assertEqual([(r["handoff_id"], r["outcome"]) for r in actual["closed"]],
                                 [(self.ids["pool"], "done")])
        sibling = self.read(self.workspaces[0], PARENT_B)
        self.assert_delegated(sibling, "Delegated: Explore", T0 + 20, [self.ids["agent"]], 1)
        other = self.read(self.workspaces[1], PARENT_A)
        self.assert_delegated(other, "Delegated: worker-b, checking fixture data", T0 + 40,
                              [self.ids["other_pool"]], 1)
        self.assertEqual(other["closed"], [])
        other_parent = self.read(self.workspaces[1], PARENT_B)
        self.assert_delegated(other_parent, "Delegated: an agent", T0 + 50,
                              [self.ids["other_message"]], 0)

    def test_explicit_close_in_another_process_is_visible_to_fresh_readers(self):
        before = self.read(self.workspaces[0], PARENT_B)
        self.assert_delegated(before, "Delegated: Explore", T0 + 20, [self.ids["agent"]], 1)
        closed = self.child("""
            print(json.dumps(handoffs.close_handoff(data["workspace"], data["handoff"], "failed")))
        """, workspace=str(self.workspaces[0]), handoff=self.ids["agent"])
        self.assertIs(closed, True)
        actual = self.read(self.workspaces[0], PARENT_B)
        self.assert_no_handoff(actual)
        self.assertEqual([(r["handoff_id"], r["outcome"]) for r in actual["closed"]],
                         [(self.ids["agent"], "failed")])
        still_open = self.read(self.workspaces[0], PARENT_A)
        self.assert_delegated(still_open, "Delegated: 2 helpers, reviewing fixtures", T0,
                              [self.ids["message"], self.ids["pool"]], 1)
        other = self.read(self.workspaces[1], PARENT_B)
        self.assert_delegated(other, "Delegated: an agent", T0 + 50, [self.ids["other_message"]], 0)
        self.assertEqual(other["closed"], [])

    def test_no_handoff_controls_do_not_borrow_another_parents_status(self):
        for ws in self.workspaces:
            with self.subTest(workspace=ws.name):
                positive = self.read(ws, PARENT_A)
                self.assertEqual(positive["local"]["phase"], "Delegated")
                control = self.read(ws, CONTROL)
                self.assert_no_handoff(control)
                self.assertEqual(control["closed"], [])


if __name__ == "__main__":
    unittest.main()
