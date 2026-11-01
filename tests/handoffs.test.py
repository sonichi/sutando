#!/usr/bin/env python3
"""src/handoffs.py: delegated work recorded where it is given away, closed by what shows it came back.

The `handoff` records and their per-path checks, open and close, the result and timeout closes, the
parent's delegated line, pool_ask's parent headers, and the activity hook's subagent and SendMessage
paths. In process, so coverage sees it.

Run: python3 tests/handoffs.test.py   (stdlib only)
"""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "worker-pool" / "scripts"))
import correlation_log as cl  # noqa: E402
import handoffs as ho  # noqa: E402

PARENT = "task-s_dspace~task-9905cc47dfa0f6b12e"
CHILD = "task-0123456789abcdef01"


def _hook():
    spec = importlib.util.spec_from_file_location("activity_hook_h", REPO / "skills" / "agent-activity" / "hooks" / "activity-hook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Records(unittest.TestCase):
    def test_the_log_checks_each_path_child_and_label(self):
        good = {"handoff_id": "ho_0123456789ab", "parent_task_id": PARENT, "path": "pool", "child_ref": CHILD,
                "delegate_label": "worker-a", "holds": "yes"}
        self.assertEqual(cl.validate("handoff", good)["path"], "pool")
        self.assertEqual(cl.validate("handoff", dict(good, path="agent", child_ref="toolu_01AbCdEf1234"))["path"], "agent")
        self.assertEqual(cl.validate("handoff", dict(good, path="message", child_ref="ix_0123456789ab"))["path"], "message")
        for bad, why in ((dict(good, child_ref="toolu_01AbCdEf1234"), "child_ref"),
                         (dict(good, path="agent", child_ref=CHILD), "child_ref"),
                         (dict(good, path="email"), "path must be"),
                         (dict(good, parent_task_id="@chi:s.space"), "parent_task_id"),
                         (dict(good, delegate_label="Chi <chi@x.co>"), "delegate_label"),
                         (dict(good, handoff_id="ho_x"), "handoff_id"),
                         (dict(good, holds="maybe"), "holds must be")):
            with self.assertRaises(ValueError) as cm:
                cl.validate("handoff", bad)
            self.assertIn(why, str(cm.exception))
        self.assertEqual(cl.validate("handoff_closed", {"handoff_id": "ho_0123456789ab", "outcome": "timeout"})["outcome"],
                         "timeout")
        self.assertRegex(cl.mint_handoff_id(), r"^ho_[0-9a-f]{12}$")

    def test_safe_label_trims_to_what_the_log_accepts(self):
        self.assertEqual(ho.safe_label("worker-a", "x"), "worker-a")
        self.assertEqual(ho.safe_label("general-purpose", "x"), "general-purpose")
        self.assertEqual(ho.safe_label("<script>", "a worker"), "script")
        self.assertEqual(ho.safe_label("", "a worker"), "a worker")
        self.assertEqual(ho.safe_label("@!#", "a worker"), "a worker")


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def test_a_pool_handoff_holds_the_parent_until_the_child_result_exists(self):
        hid = ho.open_handoff(self.ws, PARENT, "pool", CHILD, "worker-a", holds=True, parent_generation=3)
        self.assertRegex(hid, r"^ho_")
        st = ho.delegated_status(self.ws, PARENT)
        self.assertEqual((st["phase"], st["line"], st["handoffs"], st["holding"]),
                         ("Delegated", "Delegated: worker-a", 1, 1))
        (self.ws / "results").mkdir()
        (self.ws / "results" / f"{CHILD}.txt").write_text("[no-send]\nok")
        self.assertEqual(ho.open_handoffs(self.ws, PARENT), [])
        self.assertIsNone(ho.delegated_status(self.ws, PARENT))
        closed = [r for r in cl.read(workspace=self.ws) if r["kind"] == "handoff_closed"]
        self.assertEqual([r["outcome"] for r in closed], ["done"], "closed once, and recorded")
        self.assertEqual(ho.open_handoffs(self.ws, PARENT), [])
        self.assertEqual(len([r for r in cl.read(workspace=self.ws) if r["kind"] == "handoff_closed"]), 1)

    def test_a_handoff_out_too_long_times_out(self):
        ho.open_handoff(self.ws, PARENT, "pool", CHILD, "worker-a", holds=True)
        self.assertEqual(ho.open_handoffs(self.ws, PARENT, now=time.time() + ho.MAX_OPEN_S + 60), [])
        self.assertEqual([r["outcome"] for r in cl.read(workspace=self.ws) if r["kind"] == "handoff_closed"], ["timeout"])

    def test_only_holding_handoffs_hold_and_several_read_as_helpers(self):
        ho.open_handoff(self.ws, PARENT, "message", "ix_0123456789ab", "helper-b", holds=False)
        self.assertEqual(ho.open_handoffs(self.ws, PARENT, holding_only=True), [])
        ho.open_handoff(self.ws, PARENT, "agent", "toolu_01AbCdEf1234", "general-purpose", holds=True)
        st = ho.delegated_status(self.ws, PARENT)
        self.assertEqual((st["line"], st["handoffs"], st["holding"]), ("Delegated: 2 helpers", 2, 1))
        self.assertEqual(ho.close_by_child(self.ws, "agent", "toolu_01AbCdEf1234"), 1)
        self.assertEqual(ho.delegated_status(self.ws, PARENT)["line"], "Delegated: helper-b")
        self.assertIsNone(ho.delegated_status(self.ws, "task-other-1"))

    def test_the_delegates_own_line_rolls_up_and_is_capped(self):
        ho.open_handoff(self.ws, PARENT, "pool", CHILD, "helper-b", holds=True)
        fake = types.SimpleNamespace(latest=lambda ws, tid: {"line": "Reviewing 14 PRs on the dev board for the owner today"}
                                     if tid == CHILD else None)
        with patch.dict(sys.modules, {"phase_line": fake}):
            line = ho.delegated_status(self.ws, PARENT)["line"]
        self.assertTrue(line.startswith("Delegated: helper-b, reviewing 14 PRs"), line)
        self.assertLessEqual(len(line), ho.LINE_MAX)
        broken = types.SimpleNamespace(latest=lambda ws, tid: (_ for _ in ()).throw(RuntimeError("x")))
        with patch.dict(sys.modules, {"phase_line": broken}):
            self.assertEqual(ho.delegated_status(self.ws, PARENT)["line"], "Delegated: helper-b")

    def test_a_refused_record_is_no_handoff(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(ho.open_handoff(self.ws, "@chi:s.space", "pool", CHILD, "worker-a", holds=True))
        self.assertEqual(ho.open_handoffs(self.ws, "@chi:s.space"), [])

    def test_the_cli_lists_and_closes(self):
        hid = ho.open_handoff(self.ws, PARENT, "message", "ix_0123456789ab", "helper-b", holds=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ho.main(["open-list", "--task", PARENT, "--workspace", str(self.ws)]), 0)
        self.assertEqual(json.loads(out.getvalue())["status"]["line"], "Delegated: helper-b")
        self.assertEqual(ho.main(["close", "--handoff", hid, "--workspace", str(self.ws)]), 0)
        self.assertEqual(ho.open_handoffs(self.ws, PARENT), [])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ho.main(["close", "--handoff", "bad", "--workspace", str(self.ws)]), 2)


class PoolAsk(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(REPO / "tests" / "skills" / "worker-pool"))
        spec = importlib.util.spec_from_file_location("pool_ask_test", REPO / "tests" / "skills" / "worker-pool" / "pool-ask.test.py")
        self.t = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.t)
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "tasks").mkdir()
        self.alpha = self.t.make_worker(self.ws, "alpha")

    def test_an_ask_with_a_parent_names_it_and_records_the_handoff(self):
        out = self.t.pa.ask(self.ws, "alpha", "review #1", parent_task=PARENT, parent_generation=4)
        text = (self.ws / "tasks" / f"{out['task_id']}.txt").read_text()
        headers = dict(line.split(": ", 1) for line in text.split("\ntask:")[0].splitlines())
        self.assertEqual((headers["parent_task"], headers["parent_generation"]), (PARENT, "4"))
        h = ho.open_handoffs(self.ws, PARENT)
        self.assertEqual([(x["path"], x["child_ref"], x["delegate_label"], x["holds"], x["parent_generation"]) for x in h],
                         [("pool", out["task_id"], "alpha", "yes", "4")])
        self.assertEqual(out["handoff_id"], h[0]["handoff_id"])

    def test_an_ask_without_a_parent_records_nothing(self):
        out = self.t.pa.ask(self.ws, "alpha", "status?")
        self.assertNotIn("parent_task", (self.ws / "tasks" / f"{out['task_id']}.txt").read_text())
        self.assertNotIn("handoff_id", out)
        self.assertEqual([r for r in cl.read(workspace=self.ws) if r["kind"] == "handoff"], [])

    def test_a_parent_that_is_not_a_task_id_is_refused(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            self.t.pa.main(["--workspace", str(self.ws), "--to", "alpha", "--ask", "x", "--parent-task", "@chi:s.space"])
        self.assertEqual(cm.exception.code, 2)


class Hook(unittest.TestCase):
    def setUp(self):
        self.hook = _hook()
        self.ws = Path(tempfile.mkdtemp())
        self.p = dict(self.hook.paths(self.ws), ws=self.ws)

    def _pre(self, tool, inp, tool_use_id="toolu_01AbCdEf1234"):
        with patch.object(self.hook, "bound_task", return_value=({"id": PARENT}, "!r:x")):
            self.hook.handle({"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": tool,
                              "tool_input": inp, "tool_use_id": tool_use_id}, self.p, run=lambda *a, **k: None)

    def test_a_foreground_subagent_holds_until_its_call_returns(self):
        self._pre("Agent", {"description": "Review the diff", "subagent_type": "general-purpose", "prompt": "x"})
        h = ho.open_handoffs(self.ws, PARENT)
        self.assertEqual([(x["path"], x["delegate_label"], x["holds"]) for x in h], [("agent", "general-purpose", "yes")])
        self.hook.handle({"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Agent",
                          "tool_input": {"subagent_type": "general-purpose"}, "tool_use_id": "toolu_01AbCdEf1234"},
                         self.p, run=lambda *a, **k: None)
        self.assertEqual(ho.open_handoffs(self.ws, PARENT), [])

    def test_a_background_subagent_is_shown_but_does_not_hold(self):
        self._pre("Agent", {"description": "Label", "run_in_background": True, "prompt": "x"})
        self.hook.handoff_post(self.ws, "Agent", {"run_in_background": True}, "toolu_01AbCdEf1234")
        h = ho.open_handoffs(self.ws, PARENT)
        self.assertEqual([(x["delegate_label"], x["holds"]) for x in h], [("agent", "no")])

    def test_a_sendmessage_ask_with_an_id_is_a_handoff_and_a_plain_send_is_not(self):
        self._pre("SendMessage", {"to": "sutando-core", "message": "status?"})
        self.assertEqual(ho.open_handoffs(self.ws, PARENT), [])
        self._pre("SendMessage", {"to": "worker-a", "message": "please review\n\nInteraction-Id: ix_0123456789ab"})
        h = ho.open_handoffs(self.ws, PARENT)
        self.assertEqual([(x["path"], x["child_ref"], x["delegate_label"], x["holds"]) for x in h],
                         [("message", "ix_0123456789ab", "worker-a", "no")])

    def test_an_unbound_call_records_nothing_and_a_broken_module_breaks_nothing(self):
        with patch.object(self.hook, "bound_task", return_value=(None, None)):
            self.hook.handle({"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Agent",
                              "tool_input": {"description": "x"}, "tool_use_id": "toolu_01AbCdEf1234"}, self.p,
                             run=lambda *a, **k: None)
        self.assertEqual([r for r in cl.read(workspace=self.ws) if r["kind"] == "handoff"], [])
        broken = types.SimpleNamespace(open_handoff=lambda *a, **k: 1 / 0, close_by_child=lambda *a, **k: 1 / 0,
                                       safe_label=lambda *a: "x")
        with patch.dict(sys.modules, {"handoffs": broken}):
            self.hook.handoff_pre(self.ws, PARENT, "Agent", {}, "toolu_01AbCdEf1234")
            self.hook.handoff_post(self.ws, "Agent", {}, "toolu_01AbCdEf1234")


if __name__ == "__main__":
    unittest.main()
