"""Hook driver contract, exercised as Claude Code would run it (subprocess,
JSON on stdin, JSON on stdout): allowlisted tool never creates a requirement;
a non-allowlisted tool creates one and blocks until a card decision arrives;
timeout denies and expires; AskUserQuestion is left to its own bridge."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from hitl.manager import HitlManager, HitlStore, default_store  # noqa: E402
from hitl.schema import ActionReply  # noqa: E402

HOOK = REPO / "hooks" / "hitl-hook-driver.py"


def run_hook(ws: Path, payload: dict, timeout="5", extra_env=None):
    env = {**os.environ, "SUTANDO_HITL_WORKSPACE": str(ws), "SUTANDO_HITL_TIMEOUT": timeout, "SUTANDO_HITL_POLL": "0.2"}
    env.update(extra_env or {})
    p = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=30)
    out = json.loads(p.stdout) if p.stdout.strip() else None
    return p.returncode, out, p.stderr


class HookDriverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.mgr = HitlManager(HitlStore(default_store(self.ws)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_allowlisted_tool_is_allowed_by_policy_with_a_record_and_no_card(self):
        rc, out, _ = run_hook(self.ws, {"tool_name": "Read", "tool_input": {"file_path": "/x"}})
        self.assertEqual(rc, 0)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow")
        self.assertIn("policy", out["hookSpecificOutput"]["permissionDecisionReason"])
        [req] = self.mgr.store.all()  # the Manager answered it; the record stays for audit
        self.assertEqual((req.status, req.chosen_action, req.decided_by, req.subject["tool"]),
                         ("resolved", "allow", "policy", "Read"))
        self.assertFalse(self.mgr.needs_projection(req.id))  # never a card

    def test_allowlist_env_override_reaches_the_policy(self):
        _, out, _ = run_hook(self.ws, {"tool_name": "Read", "tool_input": {"file_path": "/x"}},
                             timeout="1", extra_env={"SUTANDO_HITL_ALLOW_TOOLS": "Glob"})
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")  # not allowlisted -> card -> timed out
        [req] = self.mgr.store.all()
        self.assertEqual((req.status, req.decided_by), ("expired", None))

    def test_ask_user_question_is_left_to_its_own_bridge(self):
        rc, out, _ = run_hook(self.ws, {"tool_name": "AskUserQuestion", "tool_input": {}})
        self.assertEqual((rc, out), (0, None))

    def test_bash_blocks_then_honours_the_card_decision(self):
        payload = {"tool_name": "Bash", "tool_input": {"command": "rm -rf build"}}
        result = {}

        def answer():
            deadline = time.time() + 10
            while time.time() < deadline:
                active = self.mgr.active()
                if active:
                    req = active[0]
                    result["req"] = req
                    self.mgr.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision, action_id="allow", guard=req.guard))
                    return
                time.sleep(0.1)

        t = threading.Thread(target=answer); t.start()
        rc, out, err = run_hook(self.ws, payload, timeout="10")
        t.join()
        self.assertEqual(rc, 0, err)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "allow")
        req = result["req"]
        self.assertEqual((req.kind, req.runtime), ("permission", "claude"))
        self.assertIn("rm -rf build", req.message)
        self.assertTrue(req.guard.startswith("hook:"))
        self.assertEqual([a.kind for a in req.actions], ["allow_once", "reject_once", "open_terminal"])
        self.assertEqual(self.mgr.get(req.id).status, "resolved")

    def test_deny_decision_denies(self):
        def answer():
            deadline = time.time() + 10
            while time.time() < deadline:
                if self.mgr.active():
                    req = self.mgr.active()[0]
                    self.mgr.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision, action_id="deny", guard=req.guard))
                    return
                time.sleep(0.1)

        t = threading.Thread(target=answer); t.start()
        _, out, _ = run_hook(self.ws, {"tool_name": "Write", "tool_input": {"file_path": "/etc/hosts"}}, timeout="10")
        t.join()
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_timeout_denies_and_expires_never_allows(self):
        _, out, _ = run_hook(self.ws, {"tool_name": "Bash", "tool_input": {"command": "true"}}, timeout="1")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("No decision", out["hookSpecificOutput"]["permissionDecisionReason"])
        [req] = self.mgr.store.all()
        self.assertEqual(req.status, "expired")

    def test_exit_plan_mode_is_a_confirmation(self):
        _, out, _ = run_hook(self.ws, {"tool_name": "ExitPlanMode", "tool_input": {}}, timeout="1")
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")  # timed out
        [req] = self.mgr.store.all()
        self.assertEqual(req.kind, "confirmation")


if __name__ == "__main__":
    unittest.main()
