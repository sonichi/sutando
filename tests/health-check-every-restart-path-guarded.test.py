#!/usr/bin/env python3
"""Guard the fix loop's source-backed actions, including changing checkout state."""
import ast
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("health_check", REPO / "src/health-check.py")
hc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hc
spec.loader.exec_module(hc)


def issue(name, **extra):
    return {"name": name, "status": "stale", "detail": "stale code", **extra}


@contextlib.contextmanager
def isolated(checks):
    with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
        for name, value in (("run_all_checks", checks), ("fix_down_bridges", []),
                            ("publish_health_report", None), ("track_health_fix", None)):
            stack.enter_context(mock.patch.object(hc, name, return_value=value))
        stack.enter_context(mock.patch.object(hc, "WORKSPACE_DIR", Path(tmp)))
        stack.enter_context(mock.patch.object(sys, "argv", ["health-check.py", "--fix", "--quiet"]))
        yield stack


def invoke():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            hc.main()
        except SystemExit:
            pass
    return out.getvalue()


def action_audit(loop):
    actions, failures = Counter(), []
    external = {"/usr/bin/open", "ngrok", "ts_bin"}

    def block(statements, guarded=False):
        pending = False
        for stmt in statements:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call):
                pending = isinstance(stmt.value.func, ast.Name) and stmt.value.func.id in {
                    "_restart_gate", "stale_restart_allowed"}
            if isinstance(stmt, ast.If):
                block(stmt.body, guarded)
                block(stmt.orelse, guarded)
                if (pending and isinstance(stmt.test, ast.UnaryOp)
                        and isinstance(stmt.test.op, ast.Not)
                        and isinstance(stmt.test.operand, ast.Name)
                        and stmt.test.operand.id == "_ok"
                        and any(isinstance(n, ast.Continue) for n in stmt.body)):
                    guarded = True
                continue
            for call in (n for n in ast.walk(stmt) if isinstance(n, ast.Call)):
                fn = ast.unparse(call.func)
                if fn.startswith("fix_") or fn == "_restart_bridge":
                    actions[fn] += 1
                    if not guarded:
                        failures.append(fn)
                elif fn in {"subprocess.run", "subprocess.Popen"}:
                    arg = call.args[0]
                    if isinstance(arg, ast.BinOp):
                        arg = arg.left
                    first = arg.elts[0] if isinstance(arg, (ast.List, ast.Tuple)) else arg
                    command = first.value if isinstance(first, ast.Constant) else ast.unparse(first)
                    actions[f"command:{command}"] += 1
                    if command not in external and not guarded:
                        failures.append(f"command:{command}")
    block(loop.body)
    return actions, failures


class GuardedRestarts(unittest.TestCase):
    def test_unreadable_metadata_refuses_restart(self):
        with mock.patch.object(Path, "exists", side_effect=PermissionError("locked")):
            allowed, reason = hc._checkout_is_canonical(Path("/fixture"))
        self.assertFalse(allowed)
        self.assertIn(hc.CHECKOUT_UNREADABLE, reason)

    def test_unreadable_config_defaults_expected_branch(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
                "sutando_config.load_config", side_effect=OSError("unreadable")):
            self.assertEqual(hc._resolve_expected_branch(Path("/fixture")), "main")

    def test_each_existing_dispatch_refuses_and_permits(self):
        cases = [issue("com.sutando.voice-agent"), issue("conversation-server"),
                 issue("slack-bridge"), issue("voice-transport", status="fail", _stuck_connecting=True)]
        cases += [issue(name) for name in hc.LAUNCHD_BACKED_CHECKS]
        for check in cases:
            for allowed in (False, True):
                with self.subTest(check=check["name"], allowed=allowed), isolated([check]) as stack:
                    calls = []
                    stack.enter_context(mock.patch.object(hc, "stale_restart_allowed",
                                                          return_value=(allowed, "noncanonical")))
                    for name in ("fix_launchd", "fix_conversation_server"):
                        stack.enter_context(mock.patch.object(hc, name, side_effect=lambda *a: calls.append(a) or "restarted"))
                    stack.enter_context(mock.patch.object(hc, "_restart_bridge", side_effect=lambda *a, **kw: calls.append(a) or (True, "restarted")))
                    out = invoke()
                    self.assertEqual(bool(calls), allowed, out)
                    if not allowed:
                        self.assertIn("noncanonical", out)

    def test_real_checkout_is_rechecked_between_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-b", "main", tmp], check=True, capture_output=True)
            tracked = repo / "tracked"
            tracked.write_text("clean")
            subprocess.run(["git", "-C", tmp, "add", "tracked"], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", "commit", "-m", "fixture"], check=True, capture_output=True)
            for change in (False, True):
                tracked.write_text("clean")
                with self.subTest(change=change), isolated([issue("com.sutando.first"), issue("com.sutando.second")]) as stack:
                    calls = []
                    def launch(job):
                        calls.append(job)
                        if change:
                            tracked.write_text("dirty")
                        return "restarted"
                    stack.enter_context(mock.patch.object(hc, "REPO_DIR", repo))
                    stack.enter_context(mock.patch.dict(os.environ, {"SUTANDO_EXPECTED_BRANCH": "main"}))
                    stack.enter_context(mock.patch.object(hc, "fix_launchd", side_effect=launch))
                    invoke()
                    self.assertEqual(calls, ["com.sutando.first"] if change else ["com.sutando.first", "com.sutando.second"])

    def test_unrelated_check_does_not_probe_checkout(self):
        with isolated([issue("unrelated")]) as stack:
            guard = stack.enter_context(mock.patch.object(hc, "stale_restart_allowed"))
            invoke()
            guard.assert_not_called()

    def test_restart_action_inventory_and_unguarded_mutation(self):
        tree = ast.parse((REPO / "src/health-check.py").read_text())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        loop = next(n for n in ast.walk(main) if isinstance(n, ast.For) and isinstance(n.iter, ast.Name) and n.iter.id == "issues"
                    and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                            and c.func.id == "fix_launchd" for c in ast.walk(n)))
        actions, failures = action_audit(loop)
        self.assertEqual(failures, [])
        self.assertEqual(actions, Counter({"fix_launchd": 3, "_restart_bridge": 1,
            "fix_conversation_server": 1, "command:/usr/bin/open": 1,
            "command:ngrok": 1, "command:ts_bin": 1}))
        loop.body += ast.parse('if c["name"] == "metrics-sidecar":\n    subprocess.run(["launchctl", "kickstart", "-k", "metrics"])').body
        self.assertIn("command:launchctl", action_audit(loop)[1])


if __name__ == "__main__":
    unittest.main()
