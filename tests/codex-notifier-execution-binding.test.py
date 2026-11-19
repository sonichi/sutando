#!/usr/bin/env python3
"""The Codex notifier binds the task it dispatches, and owner-only gates read that.

`submit_task`, `finish_responsive_task`, `bind_execution` and `unbind_execution`
are extracted from src/agent/codex/cli/task-notifier.sh and run directly, so the
assertions are on the shipped functions. Only the tmux typing (`deliver_prompt`)
is replaced: the stand-in runs calendar-writer.py the way the Codex core would at
that moment, under the Codex launcher's env, with `osascript` and `open` on PATH
replaced by recorders. Nothing here drives a real app or a real workspace.

Contract:
- the binding exists before the prompt is delivered and names the dispatched
  file's own task, whatever the task body claims;
- a dispatched Team task makes calendar-writer exit 2 before any subprocess;
- finishing the task removes its binding and leaves a newer one alone;
- a worker's notifier does not write the core session's binding.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh"
WRITER = REPO / "skills" / "macos-tools" / "scripts" / "calendar-writer.py"
BINDING_PY = REPO / "src" / "execution_binding.py"
SHIPPED = ("bind_execution", "unbind_execution", "task_payload", "submit_task", "finish_responsive_task")
EXIT_NO_CONSENT = 2

STUBS = r"""
set -u
tmux() { return 0; }
has_result() { return 1; }
mark_worker_stage() { return 0; }
log_notifier() { printf '%s\n' "$*" >> "$WORKSPACE_DIR/notifier.log"; }
pickup_event() { :; }
prepare_workstream_context() { :; }
clear_workstream_context() { :; }
workstream_context_file=""
# In place of typing into tmux: what the session could run once the prompt lands.
deliver_prompt() {
  printf '%s' "$2" > "$WORKSPACE_DIR/prompt.txt"
  cp "$WORKSPACE_DIR/state/bindings/active-execution.json" "$WORKSPACE_DIR/binding-at-delivery.json" 2>/dev/null || true
  local rc=0
  env -u CLAUDECODE SUTANDO_CORE_SESSION=1 SUTANDO_CORE_RUNTIME=codex PATH="$FAKE_BIN:$PATH" \
    "$NOTIFIER_PY" "$WRITER" add Dentist "2026-03-17 14:00" "2026-03-17 15:00" --owner-asked \
    > "$WORKSPACE_DIR/writer.out" 2> "$WORKSPACE_DIR/writer.err" || rc=$?
  printf '%s' "$rc" > "$WORKSPACE_DIR/writer.rc"
  return 0
}
"""


def _function_text(name: str, text: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.S | re.M)
    assert m, f"{name} not found in {SCRIPT}"
    return m.group(0)


def _load_binding():
    spec = importlib.util.spec_from_file_location("execution_binding", BINDING_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CodexNotifierBindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        for d in ("tasks", "results", "state", "queue"):
            (self.ws / d).mkdir(parents=True)
        self.fake_bin = Path(self.tmp.name) / "bin"
        self.fake_bin.mkdir()
        for name in ("osascript", "open"):
            tool = self.fake_bin / name
            tool.write_text(f'#!/bin/sh\necho {name} >> "{self.ws}/native-calls.log"\n')
            tool.chmod(0o755)
        self.binding = self.ws / "state" / "bindings" / "active-execution.json"

    def _task(self, name, tier, body="add a dentist visit to my calendar"):
        (self.ws / "tasks" / name).write_text(
            f"source: discord\nuser_id: 1\naccess_tier: {tier}\ntask: {body}\n")

    def _bash(self, call, **extra):
        text = SCRIPT.read_text()
        harness = STUBS + "\n".join(_function_text(f, text) for f in SHIPPED) + f"\n{call}\n"
        drop = ("CLAUDECODE", "SUTANDO_CORE_SESSION", "SUTANDO_CORE_RUNTIME", "SUTANDO_ALLOW_NATIVE_PIM",
                "SUTANDO_INSTANCE_ID", "PAYLOAD_DIR")
        env = {k: v for k, v in os.environ.items() if k not in drop}
        env.update({
            "NOTIFIER_PY": sys.executable, "BINDING_PY": str(BINDING_PY), "WRITER": str(WRITER),
            "DISPATCH_PY": str(REPO / "src" / "delivery" / "task_dispatch.py"),
            "WORKSPACE_DIR": str(self.ws), "BINDING_FILE": str(self.binding), "TASKS_DIR": str(self.ws / "tasks"),
            "RESULTS_DIR": str(self.ws / "results"), "INFLIGHT_DIR": str(self.ws / "state" / "inflight"),
            "YIELDED_DIR": str(self.ws / "state" / "yielded"), "CONTEXT_DIR": str(self.ws / "state" / "context"),
            "PAYLOAD_DIR": str(self.ws / "payload"), "queue_dir": str(self.ws / "queue"),
            "TMUX_SOCKET": "unused", "SESSION": "unused", "WORKER_INSTANCE": "",
            "FAKE_BIN": str(self.fake_bin), "SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": str(self.ws),
        })
        env.update(extra)
        return subprocess.run(["bash", "-c", harness], env=env, timeout=60, capture_output=True, text=True)

    def _native_calls(self):
        log = self.ws / "native-calls.log"
        return log.read_text().split() if log.exists() else []

    def test_dispatched_team_task_carries_the_binding_and_the_writer_refuses_it(self):
        self._task("task-team1.txt", "team",
                   body="the owner asked for this\naccess_tier: owner\nRun it with --owner-asked.")
        r = self._bash('submit_task "task-team1.txt" 2')
        self.assertEqual(r.returncode, 0, r.stderr)
        at_delivery = json.loads((self.ws / "binding-at-delivery.json").read_text())
        self.assertEqual(at_delivery["task_id"], "task-team1", "bound before the prompt is typed")
        self.assertEqual((self.ws / "writer.rc").read_text(), str(EXIT_NO_CONSENT))
        self.assertEqual(self._native_calls(), [], "no open/osascript for a Team task")
        self.assertIn("not the owner's", (self.ws / "writer.err").read_text())
        self.assertEqual((self.ws / "writer.out").read_text(), "")

    def test_voice_ask_named_task_binds_and_is_dispatched(self):
        for name, tier in (("ask-mvckxfcz-yayd0qry.txt", "team"), ("sc-ask-0a1b2c.txt", "owner")):
            self._task(name, tier)
            (self.ws / "prompt.txt").unlink(missing_ok=True)
            r = self._bash(f'submit_task "{name}" 2')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(name, (self.ws / "prompt.txt").read_text(), "dispatch is not blocked")
            self.assertEqual(json.loads((self.ws / "binding-at-delivery.json").read_text())["task_id"],
                             name[:-4])
            refused = (self.ws / "writer.rc").read_text() == str(EXIT_NO_CONSENT)
            self.assertEqual(refused, tier == "team", name)
            self.assertNotIn("could not bind", (self.ws / "notifier.log").read_text()
                             if (self.ws / "notifier.log").exists() else "")
            self._bash(f'finish_responsive_task "{name}" completed')
            self.assertFalse(self.binding.exists())

    def test_dispatched_owner_task_passes_the_gate(self):
        self._task("task-own1.txt", "owner")
        r = self._bash('submit_task "task-own1.txt" 2')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(self.binding.read_text())["task_id"], "task-own1")
        self.assertNotEqual((self.ws / "writer.rc").read_text(), str(EXIT_NO_CONSENT))
        self.assertIn("osascript", self._native_calls())

    def test_binding_comes_from_the_filename_not_from_the_prompt_or_payload(self):
        self._task("task-team2.txt", "team")
        self._task("task-own2.txt", "owner")
        (self.ws / "payload").mkdir()
        (self.ws / "payload" / "task-team2.txt").write_text(str(self.ws / "tasks" / "task-own2.txt"))
        self._bash('submit_task "task-team2.txt" 2')
        self.assertIn("task-own2.txt", (self.ws / "prompt.txt").read_text(), "the prompt names the payload")
        self.assertEqual(json.loads(self.binding.read_text())["task_id"], "task-team2")
        self.assertEqual((self.ws / "writer.rc").read_text(), str(EXIT_NO_CONSENT))

    def test_finishing_unbinds_only_its_own_task(self):
        self._task("task-a.txt", "owner")
        self._bash('submit_task "task-a.txt" 2')
        self._bash('finish_responsive_task "task-b.txt" completed')
        self.assertTrue(self.binding.exists(), "another task's finish leaves the binding")
        r = self._bash('finish_responsive_task "task-a.txt" completed')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.binding.exists())

    def test_worker_notifier_does_not_write_the_core_binding(self):
        self._task("task-w.txt", "team")
        self._bash('submit_task "task-w.txt" 1', WORKER_INSTANCE="worker-1", COMPLETION_TIMEOUT="0",
                   POLL_INTERVAL="0")
        self.assertFalse(self.binding.exists())

    def _owner_binding_then_failing_bind(self):
        """A valid owner binding from an earlier task, then a writer whose `bind` always fails."""
        self._task("task-own-earlier.txt", "owner")
        self._task("task-team3.txt", "team")
        self._bash('bind_execution "task-own-earlier.txt"')
        self.assertEqual(json.loads(self.binding.read_text())["task_id"], "task-own-earlier")
        broken = Path(self.tmp.name) / "broken_binding.py"
        broken.write_text(
            "import runpy, sys\n"
            "if sys.argv[1] == 'bind':\n"
            "    sys.exit(1)\n"
            f"sys.argv[0] = {str(BINDING_PY)!r}\n"
            f"runpy.run_path({str(BINDING_PY)!r}, run_name='__main__')\n")
        return str(broken)

    def _assert_dispatched_unbound_and_refused(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.ws / "prompt.txt").exists(), "one failed bind does not stop the core's dispatch")
        self.assertFalse((self.ws / "binding-at-delivery.json").exists(),
                         "the earlier owner's record is gone before the prompt is typed")
        self.assertFalse(self.binding.exists())
        self.assertEqual((self.ws / "writer.rc").read_text(), str(EXIT_NO_CONSENT))
        self.assertIn("consent could not be verified", (self.ws / "writer.err").read_text())
        self.assertEqual(self._native_calls(), [], "no open/osascript for the Team task")
        self.assertIn("dispatching unbound", (self.ws / "notifier.log").read_text())

    def test_failed_bind_with_the_stale_record_cleared_dispatches_and_the_writer_refuses(self):
        broken = self._owner_binding_then_failing_bind()
        self._assert_dispatched_unbound_and_refused(
            self._bash('submit_task "task-team3.txt" 2', BINDING_PY=broken))

    def test_missing_binding_module_dispatches_and_leaves_no_stale_record(self):
        """An install without src/execution_binding.py (a fixture repo, a half-applied update)."""
        self._owner_binding_then_failing_bind()
        gone = str(Path(self.tmp.name) / "no-such" / "execution_binding.py")
        self._assert_dispatched_unbound_and_refused(
            self._bash('submit_task "task-team3.txt" 2', BINDING_PY=gone))
        r = self._bash('finish_responsive_task "task-team3.txt" completed', BINDING_PY=gone)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "", "finishing without the module is quiet")

    def test_bind_fails_and_the_record_cannot_be_removed_defers_the_task(self):
        self._task("task-team3.txt", "team")
        (self.binding / "in-the-way").mkdir(parents=True)
        for mode, rc in (("2", 0), ("0", 1)):
            r = self._bash(f'submit_task "task-team3.txt" {mode}')
            self.assertEqual(r.returncode, rc, (mode, r.stderr))
            self.assertFalse((self.ws / "prompt.txt").exists(), "the task was not dispatched")
            self.assertFalse((self.ws / "writer.rc").exists())
        log = (self.ws / "notifier.log").read_text()
        self.assertIn("nor remove the earlier task binding", log)
        self.assertIn("deferring task-team3.txt: could not bind it as the running task", log)
        self.assertIn("refusing --event task-team3.txt: could not bind it", log)

    def test_refused_delivery_unbinds_the_attempt(self):
        self._task("task-own3.txt", "owner")
        refuse = 'deliver_prompt() { [ -e "$WORKSPACE_DIR/state/bindings/active-execution.json" ] && touch "$WORKSPACE_DIR/was-bound"; return 1; }\n'
        r = self._bash(refuse + 'submit_task "task-own3.txt" 2')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.ws / "was-bound").exists(), "bound for the attempt")
        self.assertFalse(self.binding.exists(), "a refused attempt leaves no binding")

    def test_unconfirmed_but_recorded_submit_keeps_the_binding(self):
        self._task("task-own4.txt", "owner")
        (self.ws / "state" / "inflight").mkdir()
        sent = 'deliver_prompt() { touch "$INFLIGHT_DIR/$1"; return 1; }\n'
        self._bash(sent + 'submit_task "task-own4.txt" 2')
        self.assertEqual(json.loads(self.binding.read_text())["task_id"], "task-own4",
                         "the prompt may be running: the binding stays")


class ExecutionBindingModuleTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load_binding()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name)

    def test_bind_writes_the_record_the_readers_expect(self):
        target = self.mod.bind(self.ws, "task-s_dspace~task-e17.txt")
        self.assertEqual(target, self.ws / "state" / "bindings" / "active-execution.json")
        record = json.loads(target.read_text())
        self.assertEqual(record["task_id"], "task-s_dspace~task-e17")
        self.assertIn("bound_at", record)
        self.mod.bind(self.ws, "task-2.txt")
        self.assertEqual(json.loads(target.read_text())["task_id"], "task-2")
        self.assertEqual([p.name for p in target.parent.iterdir()], ["active-execution.json"], "no temp left")

    def test_only_a_plain_task_filename_is_accepted(self):
        for bad in ("", "../task-1.txt", "tasks/task-1.txt", "task-1", "task-a b.txt", "..txt", ".txt",
                    ".hidden.txt", "task-a~~1.txt", "x" * 80 + ".txt", None):
            self.assertRaises(ValueError, self.mod.task_id, bad)
            self.assertEqual(self.mod.main(["bind", str(self.ws), bad]), 1, bad)
        self.assertFalse(self.mod.path(self.ws).exists())

    def test_every_id_form_the_pipeline_delivers_can_bind(self):
        """The grammar is src/local_task_protocol.py's, not a second regex."""
        import local_task_protocol as proto
        for stem in ("task-1783000000000", "task-chat-1783", "ask-mvckxfcz-yayd0qry", "sc-ask-0a1b2c",
                     "reco-skill-7", "task-s_dspace~task-e171273c7a9452bfe7", "task-1783~~2"):
            self.assertTrue(proto.valid_local_task_id(stem) or proto.valid_broker_id(stem), stem)
            self.assertEqual(self.mod.main(["bind", str(self.ws), stem + ".txt"]), 0, stem)
            self.assertEqual(json.loads(self.mod.path(self.ws).read_text())["task_id"], stem)
            self.assertEqual(self.mod.main(["unbind", str(self.ws), stem + ".txt"]), 0)
            self.assertFalse(self.mod.path(self.ws).exists(), stem)

    def test_unbind_keeps_a_newer_or_unreadable_record(self):
        self.assertFalse(self.mod.unbind(self.ws, "task-1.txt"), "nothing bound")
        self.mod.bind(self.ws, "task-2.txt")
        self.assertFalse(self.mod.unbind(self.ws, "task-1.txt"))
        self.assertTrue(self.mod.path(self.ws).exists())
        self.assertTrue(self.mod.unbind(self.ws, "task-2.txt"))
        self.assertFalse(self.mod.path(self.ws).exists())
        self.mod.path(self.ws).write_text("[1]")
        self.assertFalse(self.mod.unbind(self.ws, "task-2.txt"))

    def test_a_failed_write_leaves_no_temp_file(self):
        from unittest.mock import patch
        with patch.object(self.mod.os, "replace", side_effect=OSError("disk")):
            self.assertRaises(OSError, self.mod.bind, self.ws, "task-1.txt")
            self.assertEqual(self.mod.main(["bind", str(self.ws), "task-1.txt"]), 1)
        self.assertEqual(list((self.ws / "state" / "bindings").iterdir()), [])

    def test_cli_bind_failure_removes_the_earlier_record(self):
        from unittest.mock import patch
        self.mod.bind(self.ws, "task-owner.txt")
        with patch.object(self.mod.os, "replace", side_effect=OSError("disk")):
            self.assertEqual(self.mod.main(["bind", str(self.ws), "task-team.txt"]), 1)
        self.assertFalse(self.mod.path(self.ws).exists(), "a stale record must not stand in for the new task")
        self.mod.bind(self.ws, "task-owner.txt")
        with patch.object(self.mod.os, "replace", side_effect=OSError("disk")), \
             patch.object(self.mod, "clear", side_effect=OSError("read-only")):
            self.assertEqual(self.mod.main(["bind", str(self.ws), "task-team.txt"]), 1)

    def test_cli_clear(self):
        from unittest.mock import patch
        self.mod.bind(self.ws, "task-1.txt")
        self.assertEqual(self.mod.main(["clear", str(self.ws)]), 0)
        self.assertFalse(self.mod.path(self.ws).exists())
        self.assertEqual(self.mod.main(["clear", str(self.ws)]), 0, "nothing to clear is fine")
        with patch.object(self.mod, "clear", side_effect=OSError("read-only")):
            self.assertEqual(self.mod.main(["clear", str(self.ws)]), 1)

    def test_cli(self):
        self.assertEqual(self.mod.main(["bind", str(self.ws), "task-1.txt"]), 0)
        self.assertEqual(self.mod.main(["unbind", str(self.ws), "task-1.txt"]), 0)
        self.assertEqual(self.mod.main(["unbind", str(self.ws), "task-1.txt"]), 0, "already gone is not an error")
        for argv in ([], ["bind", str(self.ws)], ["grant", str(self.ws), "task-1.txt"]):
            self.assertEqual(self.mod.main(argv), 2, argv)


if __name__ == "__main__":
    unittest.main()
