#!/usr/bin/env python3
"""Both task-notifier.sh implementations' probe_optional_task_handler() must see
a handler declared via the JSON config file (task-event-handler.json), not only
the legacy SUTANDO_TASK_EVENT_HANDLER env var -- the launcher deliberately
stopped setting that var as part of this PR's own design, so a probe that only
checks it would silently decline forever with a real, live JSON declaration in
place. `probe_optional_task_handler` is extracted from each shipped script and
run against a real task_event_handler-lookup.sh source, so the assertion is on
the shipped function, not a copy. Reviewed by keweichen on PR #4503.

Run: python3 tests/task-notifier-reads-json-handler.test.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOOKUP = REPO / "src" / "agent" / "task-event-handler-lookup.sh"


def _function_text(name: str, text: str) -> str:
    m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.S | re.M)
    assert m, f"{name} not found"
    return m.group(0)


class _ProbeReadsJsonHandlerMixin:
    NOTIFIER_SCRIPT = None  # set by subclass

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        (self.tmp / "state").mkdir()
        (self.tmp / "results").mkdir()
        (self.tmp / "tasks").mkdir()
        self.handler = self.tmp / "handler.sh"
        self.handler.write_text("#!/bin/sh\nexit 0\n")
        self.handler.chmod(0o755)
        self.cfg = self.tmp / "state" / "task-event-handler.json"

    def _probe(self, task_file_var: str = "TASKS_DIR", env_pin: str | None = None) -> subprocess.CompletedProcess:
        fn = _function_text("probe_optional_task_handler", self.NOTIFIER_SCRIPT.read_text())
        harness = LOOKUP.read_text() + "\n" + fn + '\nprobe_optional_task_handler "task-x.txt"\n'
        env = {**os.environ,
               "WORKSPACE_DIR": str(self.tmp), "TASKS_DIR": str(self.tmp / "tasks"),
               "RESULTS_DIR": str(self.tmp / "results"), "REPO": str(REPO),
               "HANDLER_CONFIG_PATH": str(self.cfg), "SUTANDO_PY_BIN": sys.executable}
        env.pop("SUTANDO_TASK_EVENT_HANDLER", None)
        if env_pin is not None:
            env["SUTANDO_TASK_EVENT_HANDLER"] = env_pin
        return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, env=env, timeout=15)

    def test_json_only_declaration_is_seen(self):
        self.cfg.write_text(json.dumps({"handler": str(self.handler)}))
        r = self._probe()
        self.assertEqual(r.returncode, 0,
                          f"JSON-declared handler was not invoked (stderr: {r.stderr})")

    def test_no_config_and_no_pin_declines(self):
        r = self._probe()
        self.assertEqual(r.returncode, 3, "with nothing declared, the probe must decline (rc 3)")

    def test_env_pin_still_wins_over_json(self):
        self.cfg.write_text(json.dumps({"handler": "/nonexistent/not-a-real-handler"}))
        r = self._probe(env_pin=str(self.handler))
        self.assertEqual(r.returncode, 0,
                          f"a live operator pin must override a stale JSON entry (stderr: {r.stderr})")


class ClaudeNotifierTest(_ProbeReadsJsonHandlerMixin, unittest.TestCase):
    NOTIFIER_SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "task-notifier.sh"


class CodexNotifierTest(_ProbeReadsJsonHandlerMixin, unittest.TestCase):
    NOTIFIER_SCRIPT = REPO / "src" / "agent" / "codex" / "cli" / "task-notifier.sh"


if __name__ == "__main__":
    unittest.main()
