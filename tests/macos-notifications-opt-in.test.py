#!/usr/bin/env python3
"""macOS notifications are off unless the owner sets `notifications.macos: true`.

One reader decides (`resolve_macos_notifications` in src/sutando_config.py, shell:
`scripts/sutando-config.sh macos-notifications`, used by src/macos-notify.sh), and every
macOS sender asks it. Every osascript here is a stub or a mock: no test shows a banner.

Run: python3 tests/macos-notifications-opt-in.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills" / "pending-questions" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sutando_config  # noqa: E402
from _helpers.macos_opt_in import macos_opt_in_env  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve their module by name
    spec.loader.exec_module(mod)
    return mod


def _repo(tmp, local=None, local_text=None):
    """A config root: tracked defaults with no notifications key, plus an optional local file."""
    root = Path(tmp)
    (root / "sutando.config.json").write_text(json.dumps({"workspace": {"path": str(root / "ws")}}))
    if local is not None:
        (root / "sutando.config.local.json").write_text(json.dumps(local))
    if local_text is not None:
        (root / "sutando.config.local.json").write_text(local_text)
    return root


def _child_env(**extra):
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ.get("HOME", "/tmp"),
           "PYTHONNOUSERSITE": "1"}
    env.update(extra)
    return env


class Policy(unittest.TestCase):
    def setUp(self):
        sutando_config._reset_cache_for_tests()
        self.addCleanup(sutando_config._reset_cache_for_tests)
        self.tmp = tempfile.mkdtemp(prefix="macos-notif-")

    def _resolve(self, root):
        sutando_config._reset_cache_for_tests()
        with redirect_stderr(io.StringIO()) as err:
            got = sutando_config.resolve_macos_notifications(root)
        return got, err.getvalue()

    def test_the_shipped_defaults_are_off(self):
        shipped = json.loads((REPO / "sutando.config.json").read_text())
        self.assertNotIn("notifications", shipped, "the tracked defaults must not opt anyone in")
        root = _repo(self.tmp)
        (root / "sutando.config.json").write_text(json.dumps({**shipped, "workspace": {"path": str(root / "ws")}}))
        self.assertEqual(self._resolve(root)[0], False)

    def test_no_key_is_off(self):
        self.assertIs(self._resolve(_repo(self.tmp))[0], False)
        self.assertIs(self._resolve(_repo(self.tmp, {"notifications": {}}))[0], False)
        self.assertIs(self._resolve(_repo(self.tmp, {"notifications": None}))[0], False)

    def test_json_true_opts_in(self):
        self.assertIs(self._resolve(_repo(self.tmp, {"notifications": {"macos": True}}))[0], True)

    def test_json_false_is_off(self):
        self.assertIs(self._resolve(_repo(self.tmp, {"notifications": {"macos": False}}))[0], False)

    def test_a_non_boolean_is_off_and_says_so(self):
        for val in ("true", 1, "yes", {"enabled": True}):
            with self.subTest(val=val):
                got, err = self._resolve(_repo(self.tmp, {"notifications": {"macos": val}}))
                self.assertIs(got, False)
                self.assertIn("must be a JSON boolean", err)

    def test_an_unreadable_config_is_off_and_never_raises(self):
        for text in ('{"notifications": {"macos": true', '{"notifications": true}', "[1]"):
            with self.subTest(text=text):
                got, err = self._resolve(_repo(self.tmp, local_text=text))
                self.assertIs(got, False)
                self.assertIn("cannot read notifications.macos", err)

    def test_the_workspace_layer_can_opt_in(self):
        root = _repo(self.tmp)
        ws = root / "ws"
        ws.mkdir()
        (ws / "sutando.config.local.json").write_text(json.dumps({"notifications": {"macos": True}}))
        self.assertIs(self._resolve(root)[0], True)


class ShellReader(unittest.TestCase):
    def _run(self, env_extra):
        return subprocess.run(["bash", str(REPO / "scripts" / "sutando-config.sh"), "macos-notifications"],
                              env=_child_env(**env_extra), capture_output=True, text=True, timeout=60)

    def test_off_by_default_on_when_opted_in(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(self._run(macos_opt_in_env(ws, on=False)).stdout, "false")
            self.assertEqual(self._run(macos_opt_in_env(ws, on=True)).stdout, "true")
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "sutando.config.local.json").write_text("{}")
            r = self._run({"SUTANDO_TEST_MODE": "1", "SUTANDO_WORKSPACE": ws})
            self.assertEqual(r.stdout, "false", r.stderr)


class ShellSender(unittest.TestCase):
    """src/macos-notify.sh: the one shell sender, gated by the shell reader."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="macos-notify-sh-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "calls"
        stub = self.bin / "osascript"
        stub.write_text(f'#!/bin/bash\nfor a in "$@"; do printf "%s\\n" "$a"; done >> "{self.calls}"\n')
        stub.chmod(0o755)

    def _send(self, on, *args):
        env = _child_env(**macos_opt_in_env(self.tmp / "ws", on=on))
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        return subprocess.run(["bash", str(REPO / "src" / "macos-notify.sh"), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def test_off_runs_no_osascript(self):
        r = self._send(False, "hello")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.calls.exists(), "osascript ran with notifications.macos off")

    def test_on_passes_message_and_title_as_argv(self):
        msg = '-e "quoted" $(not run) \\ back'
        r = self._send(True, msg, "Sutando — test")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = self.calls.read_text().splitlines()
        self.assertIn("display notification (item 1 of argv) with title (item 2 of argv)", argv)
        self.assertEqual(argv[-2:], [msg, "Sutando — test"], "message spliced into the script text")

    def test_title_defaults_to_sutando(self):
        self._send(True, "hi")
        self.assertEqual(self.calls.read_text().splitlines()[-2:], ["hi", "Sutando"])

    def test_message_cannot_skip_the_opt_in(self):
        for msg in ("--always", "--", "-e"):
            with self.subTest(msg=msg):
                r = self._send(False, msg)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertFalse(self.calls.exists(), f"message {msg!r} got past the opt-in")

    def test_on_a_flag_shaped_message_is_the_body(self):
        r = self._send(True, "--always")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls.read_text().splitlines()[-2:], ["--always", "Sutando"])

    def test_always_entrypoint_posts_when_off(self):
        env = _child_env(**macos_opt_in_env(self.tmp / "ws", on=False))
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        r = subprocess.run(["bash", str(REPO / "src" / "macos-notify-always.sh"), "no workspace"],
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls.read_text().splitlines()[-2:], ["no workspace", "Sutando"])


class AlwaysIsReservedForTheChannellessAlert(unittest.TestCase):
    """The ungated entrypoint is a thin real file over the helper; only the no-workspace alert names it."""

    def test_always_entrypoint_is_a_real_file_that_calls_the_helper(self):
        entry = REPO / "src" / "macos-notify-always.sh"
        self.assertFalse(entry.is_symlink(), "packaging may drop symlinks")
        text = entry.read_text()
        self.assertIn("macos-notify.sh", text)
        self.assertNotIn("osascript", text)

    def test_only_the_gateway_no_workspace_alert_uses_it(self):
        users = []
        for dirpath, dirnames, files in os.walk(REPO):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in files:
                p = Path(dirpath) / f
                if p.suffix in SCANNED and not p.name.startswith("macos-notify"):
                    for line in p.read_text(errors="replace").splitlines():
                        if "macos-notify-always" in line:
                            users.append((p.relative_to(REPO).as_posix(), line.strip()))
        self.assertEqual([u[0] for u in users], ["src/launchd/gateway-bridge-wrapper.sh"], users)
        self.assertIn("cannot resolve the workspace", users[0][1])


# Shell senders that used to call osascript themselves; each now goes through the helper.
SHELL_SENDERS = (
    "src/notify.sh",
    "src/auth-preflight-gate.sh",
    "src/context-drop.sh",
    "src/agent/claude/cli/task-notifier.sh",
    "src/agent/codex/cli/task-notifier-supervisor.sh",
    "src/launchd/channel-bridge-wrapper.sh",
    "src/launchd/gateway-bridge-wrapper.sh",
)

# Every file allowed to spell an osascript notification, and the gate it must carry.
GATED_SENDERS = {
    "src/macos-notify.sh": "sutando-config.sh\" macos-notifications",
    "src/sutando_platform.py": "resolve_macos_notifications()",
    "src/platform.ts": "resolveMacosNotifications()",
    "src/health-check.py": "resolve_macos_notifications()",
    "src/core-supervisor-relay.py": "_macos_notifications_on()",
    "skills/pending-questions/scripts/pending_questions_ask.py": "resolve_macos_notifications()",
    "skills/pending-questions/scripts/pending_questions_remind.py": "resolve_macos_notifications()",
    "skills/screen-record/scripts/record.py": "_macos_notifications_on()",
}
SCANNED = {".py", ".sh", ".ts", ".js", ".mjs", ".cjs", ".swift"}
SKIP_DIRS = {"node_modules", ".git", "tests", "dist", "build", "workspace", "cache"}


class EverySenderIsGated(unittest.TestCase):
    def test_no_ungated_notification_sender(self):
        found = set()
        for dirpath, dirnames, files in os.walk(REPO):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in files:
                p = Path(dirpath) / f
                if p.suffix not in SCANNED or ".test." in f:
                    continue
                try:
                    text = p.read_text(errors="replace")
                except OSError:
                    continue
                if re.search(r"display notification|terminal-notifier", text):
                    found.add(p.relative_to(REPO).as_posix())
        self.assertGreaterEqual(len(found), 5, f"the scan narrowed: {sorted(found)}")
        ungated = sorted(found - set(GATED_SENDERS) - {"src/voice-error-classifier.ts"})
        self.assertEqual(ungated, [], "a macOS notification sender that does not ask notifications.macos")
        for rel, gate in GATED_SENDERS.items():
            with self.subTest(rel=rel):
                self.assertIn(gate, (REPO / rel).read_text())

    def test_shell_senders_route_through_the_helper(self):
        for rel in SHELL_SENDERS:
            with self.subTest(rel=rel):
                text = (REPO / rel).read_text()
                self.assertIn("macos-notify.sh", text)
                self.assertNotRegex(text, r"osascript\s+-e\s+[\"'].*display notification")


class PythonSendersDelegate(unittest.TestCase):
    """Each Python sender asks the shared reader; off means no osascript at all."""

    def _gate(self, on):
        return mock.patch.object(sutando_config, "resolve_macos_notifications", return_value=on)

    def test_pending_questions_ask(self):
        pqa = _load("pqa_gate", REPO / "skills/pending-questions/scripts/pending_questions_ask.py")
        self.assertIs(pqa.resolve_macos_notifications, sutando_config.resolve_macos_notifications)
        with mock.patch.object(pqa, "resolve_macos_notifications", return_value=False), \
                mock.patch.object(pqa.subprocess, "run") as run:
            self.assertEqual(pqa.notify_macos("Question: q?"), (None, None))
        run.assert_not_called()
        with mock.patch.object(pqa, "resolve_macos_notifications", return_value=True), \
                mock.patch.object(pqa.subprocess, "run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            self.assertEqual(pqa.notify_macos("Question: q?"), (True, None))
        self.assertEqual(run.call_args.args[0][0], "osascript")
        self.assertNotIn("macos:", "\n".join(
            pqa.report_lines({"macos": None, "outbox": "x", "heading": "## x", "proactive_file": "p",
                              "where": "owner-dm"})))

    def test_pending_questions_remind(self):
        rem = _load("rem_gate", REPO / "skills/pending-questions/scripts/pending_questions_remind.py")
        self.assertIs(rem.resolve_macos_notifications, sutando_config.resolve_macos_notifications)
        with mock.patch.object(rem, "resolve_macos_notifications", return_value=False), \
                mock.patch.object(rem.subprocess, "run") as run:
            self.assertIsNone(rem.notify_macos(2, ["a", "b"]))
        run.assert_not_called()
        summary, warning = rem.notify_summary(2, None, False, ["proactive-old.txt"])
        self.assertIn("macos=off", summary)
        self.assertIn("macOS notifications are off", warning)
        self.assertNotIn("only the macOS notification is real", warning)

    def test_health_check(self):
        with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            hc = _load("hc_gate", REPO / "src" / "health-check.py")
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "health-last-notified.json"
            failing = [{"name": "discord-bridge", "status": "down", "detail": "x"}]
            with self._gate(False), mock.patch.object(hc.subprocess, "run") as run:
                self.assertIs(hc._default_local_notifier("bridge down"), False)
                hc.notify_for_failures(failing, state_file=state)
            run.assert_not_called()
            self.assertFalse(state.exists(), "an alert never shown must not be recorded as shown")
            with self._gate(True), mock.patch.object(hc.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
                hc.notify_for_failures(failing, state_file=state)
            self.assertEqual(run.call_args.args[0][0], "osascript")

    def test_core_supervisor_relay(self):
        relay = _load("relay_gate", REPO / "src" / "core-supervisor-relay.py")
        seen = []
        with tempfile.TemporaryDirectory() as td:
            sig = Path(td) / "core-supervisor.json"
            sig.write_text(json.dumps({"state": "blocked-human", "detail": "awaiting user: login",
                                       "prompt": "Login", "kind": "login"}))
            fake_cycle = lambda *a, **k: seen.append(k["macos"])  # noqa: E731
            with self._gate(False), mock.patch.object(relay, "run_cycle", fake_cycle), \
                    mock.patch.object(relay, "_macos_notify") as notify, redirect_stdout(io.StringIO()):
                relay.main(["--signal", str(sig), "--no-seats"])
            notify.assert_not_called()
            with self._gate(True), mock.patch.object(relay, "run_cycle", fake_cycle), redirect_stdout(io.StringIO()):
                relay.main(["--signal", str(sig), "--no-seats"])
                relay.main(["--signal", str(sig), "--no-seats", "--no-macos"])
        self.assertEqual(seen, [False, True, False])
        src = os.path.realpath(REPO / "src")
        bare = [x for x in sys.path if os.path.realpath(x or ".") != src]
        with mock.patch.object(sys, "path", bare), self._gate(False):
            self.assertIs(relay._macos_notifications_on(), False)
        self.assertIn(src, [os.path.realpath(x) for x in bare], "the relay finds src/ from its own file")

    def test_screen_record(self):
        rec = _load("rec_gate", REPO / "skills/screen-record/scripts/record.py")
        with self._gate(False), mock.patch.object(rec.subprocess, "Popen") as popen:
            rec._notify("Screen recording started")
        popen.assert_not_called()
        with self._gate(True), mock.patch.object(rec.subprocess, "Popen") as popen:
            rec._notify("Screen recording started")
        self.assertEqual(popen.call_args.args[0][0], "osascript")
        with mock.patch.dict(sys.modules, {"sutando_config": None}):
            self.assertIs(rec._macos_notifications_on(), False, "no reader means off")

    def test_screen_record_indicator_paths_stay_silent_when_off(self):
        rec = _load("rec_gate2", REPO / "skills/screen-record/scripts/record.py")
        with tempfile.TemporaryDirectory() as td:
            rec.SCRIPT_DIR, rec.INDICATOR_PID_FILE = td, os.path.join(td, "rec.pid")
            with self._gate(False), mock.patch.object(rec.subprocess, "Popen") as popen, \
                    redirect_stderr(io.StringIO()):
                rec._show_indicator()
                rec._hide_indicator()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
