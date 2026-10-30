#!/usr/bin/env python3
"""Real tmux worker creation/resume/restart must use the resolved Claude config.

The endpoint is synthetic: these tests observe argv, environment and lifecycle,
not authentication, model responses or conversation continuity.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills/worker-pool/scripts"
sys.path.insert(0, str(REPO / "tests"))
from _helpers.hermetic_env import hermetic_env  # noqa: E402
from _helpers.process_group import process_exited  # noqa: E402

socket_setting = os.environ.get("SUTANDO_RUNTIME_SOCKET")
environment_file = Path(socket_setting).parent / "environment.json" if socket_setting else None
SUPPLIED = (json.loads(environment_file.read_text())
            if environment_file is not None and environment_file.is_file() else {})
RUNTIME_ROOT = environment_file.parent if SUPPLIED else None

# Outside the disposable runner, use the same minimal process boundary.
ENDPOINT = r"""
#include <signal.h>
#include <stdio.h>
#include <unistd.h>
static volatile sig_atomic_t done = 0;
static void stop(int sig) { (void)sig; done = 1; }
int main(void) {
    signal(SIGTERM, stop); signal(SIGINT, stop); signal(SIGHUP, stop);
    puts("synthetic endpoint ready; no conversation or authentication");
    fflush(stdout);
    while (!done) sleep(1);
    return 0;
}
"""


class WorkerConfigDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="worker-config-", dir=RUNTIME_ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home with spaces"
        self.ws = self.base / "workspace with spaces"
        self.folder = self.base / "project with spaces"
        self.receipts = self.base / "receipts"
        self.bin = self.base / "bin"
        for path in (self.home, self.ws / "state", self.folder, self.receipts, self.bin):
            path.mkdir(parents=True)
        self.socket = str(self.base / "tmux.sock")
        self.env = hermetic_env(home=self.home, **SUPPLIED)
        self.env.update({
            "HOME": str(self.home), "SHELL": "/bin/bash",
            "PATH": str(self.bin) + os.pathsep + self.env["PATH"],
            "PYTHONDONTWRITEBYTECODE": "1", "SUTANDO_TELEMETRY": "0",
            "SUTANDO_TEST_MODE": "1", "SUTANDO_PACKAGED": "", "SUTANDO_APP_SUPPORT": "",
            "SUTANDO_WORKSPACE": str(self.ws), "SUTANDO_WORKSPACE_DIR": str(self.ws),
            "SUTANDO_DEFAULT_WORKSPACE": str(self.ws),
            "SUTANDO_RUNTIME_STATE": str(self.ws / "state"),
            "SUTANDO_RUNTIME_DB": str(self.ws / "state/runtime.sqlite"),
            "SUTANDO_RUNTIME_SOCKET": str(self.base / "runtime.sock"),
            "SUTANDO_HA_DIR": str(self.ws / "state/ha"),
            "SUTANDO_RESULTS_DIR": str(self.ws / "results"),
            "SUTANDO_RUN_DIR": str(self.base / "run"),
            "SUTANDO_INSTANCE_REGISTRY": str(self.base / "registry"),
            "SUTANDO_TMUX_SOCKET": self.socket, "SUTANDO_INSTANCE_ID": "",
            "CLAUDE_CONFIG_DIR": str(self.base / "caller config with spaces"),
            "SUTANDO_ACCEPT_BYPASS_PERMISSIONS": "1",
            "SUTANDO_NOTIFIER_GRACE_PERIOD": "300", "SUTANDO_NOTIFIER_ROLE_POLL": "1",
            "ANTHROPIC_BASE_URL": "http://factory-runtime.invalid",
        })
        self.events = []
        self.server = None
        self.addCleanup(self.save_diagnostics)
        self.addCleanup(self.stop_server)
        if sys.platform == "darwin":
            self.env["TMPDIR"] = str(self.base / "tmp with spaces")
            Path(self.env["TMPDIR"]).mkdir()
            # macOS mktemp -t needs an explicit directory to stay inside the fixture.
            self.executable(self.bin / "mktemp",
                            '#!/bin/sh\nexec /usr/bin/mktemp -p "${TMPDIR:?}" "$@"\n')
        for tool in ("tmux", "cc"):
            if not shutil.which(tool, path=self.env["PATH"]):
                self.skipTest(f"real {tool} is required")
        for tool in ("launchctl", "osascript"):
            self.executable(self.bin / tool, "#!/bin/sh\nexit 1\n")
        endpoint_dir = self.base / "endpoint"
        endpoint_dir.mkdir()
        source = RUNTIME_ROOT / "runtime-endpoint.c" if RUNTIME_ROOT else None
        if source is None or not source.is_file():
            source = self.base / "endpoint.c"
            source.write_text(ENDPOINT)
        self.endpoint = endpoint_dir / "claude"
        self.run_command(["cc", str(source), "-o", str(self.endpoint)])
        witness = self.base / "witness.py"
        witness.write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            f"receipts = Path({str(self.receipts)!r})\n"
            "record = {'pid': os.getpid(), 'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
            "          'env': {k: v for k, v in os.environ.items()\n"
            "                  if k.startswith('SUTANDO_') or k == 'CLAUDE_CONFIG_DIR'}}\n"
            "tmp = receipts / (str(os.getpid()) + '.tmp')\n"
            "tmp.write_text(json.dumps(record))\n"
            "tmp.replace(tmp.with_suffix('.json'))\n"
            f"os.execv({str(self.endpoint)!r}, [{str(self.endpoint)!r}, *sys.argv[1:]])\n")
        self.executable(self.bin / "claude",
                        "#!/bin/sh\nexec " + shlex.join([sys.executable, str(witness)]) + ' "$@"\n')
        (self.ws / "sutando.config.local.json").write_text(json.dumps({
            "core_config_dirs": [{"id": "test-claude", "type": "claude",
                                  "env_name": "CLAUDE_CONFIG_DIR", "synced": True,
                                  "value": "${WORKSPACE_DIR}/configured claude directory"}],
        }))
        self.expected = self.run_command([
            "bash", "-c", 'source "$1/src/claude_config_dir.sh"; resolve_claude_config_dir "$1"',
            "config-test", str(REPO),
        ]).stdout.strip()
        self.assertEqual(self.expected, str(self.ws / "configured claude directory"))
        self.assertNotEqual(self.expected, self.env["CLAUDE_CONFIG_DIR"])
        self.server_log = self.base / "tmux.log"
        argv = ["tmux", "-D", "-f", "/dev/null", "-S", self.socket]
        with self.server_log.open("w") as log:
            self.server = subprocess.Popen(
                argv,
                cwd=REPO, env=self.env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        self.events.append({"at": datetime.now(timezone.utc).isoformat(),
                            "argv": argv, "pid": self.server.pid})
        self.wait_for(lambda: Path(self.socket).exists() or self.server.poll() is not None,
                      "foreground tmux socket")
        self.assertIsNone(self.server.poll(), self.server_log.read_text())
        self.tmux("set-option", "-g", "default-shell", "/bin/bash")
        self.other_config = str(self.base / "unrelated config with spaces")
        self.tmux("new-session", "-d", "-s", "unrelated", "-c", str(self.folder),
                  "-e", f"CLAUDE_CONFIG_DIR={self.other_config}",
                  "claude", "--name", "unrelated")
        self.other = self.receipt("unrelated", set())
        self.events.append({"ready": True, "tmux_pid": self.server.pid,
                            "unrelated_pid": self.other["pid"], "resolved": self.expected})

    @staticmethod
    def executable(path, content):
        path.write_text(content)
        path.chmod(0o755)

    def run_command(self, argv, *, expected=0, timeout=30):
        started = datetime.now(timezone.utc).isoformat()
        result = subprocess.run(argv, cwd=REPO, env=self.env, capture_output=True,
                                text=True, timeout=timeout)
        self.events.append({"at": started, "argv": list(argv), "rc": result.returncode,
                            "stdout": result.stdout[-12000:], "stderr": result.stderr[-12000:]})
        if expected is not None:
            self.assertEqual(result.returncode, expected,
                             f"{shlex.join(argv)}\n{result.stdout}\n{result.stderr}")
        return result

    def tmux(self, *args, expected=0):
        return self.run_command(["tmux", "-S", self.socket, *args], expected=expected)

    def wait_for(self, predicate, description, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail(f"timed out waiting for {description}")

    def receipt_ids(self):
        return {p.name for p in self.receipts.glob("*.json")}

    def receipt(self, name, before):
        found = []

        def observe():
            for filename in self.receipt_ids() - before:
                row = json.loads((self.receipts / filename).read_text())
                if row["argv"][:2] == ["--name", name]:
                    args = self.run_command(["ps", "-p", str(row["pid"]), "-o", "args="],
                                            expected=None).stdout
                    if args.startswith(str(self.endpoint) + " "):
                        found.append(row)
                        return True
            return False

        self.wait_for(observe, f"new synthetic runtime for {name}")
        self.assertEqual(int(self.tmux("list-panes", "-t", "=" + name,
                                      "-F", "#{pane_pid}").stdout), found[0]["pid"])
        return found[0]

    def set_server_config(self, value):
        if value is None:
            self.tmux("set-environment", "-gu", "CLAUDE_CONFIG_DIR")
        else:
            self.tmux("set-environment", "-g", "CLAUDE_CONFIG_DIR", value)
        self.server_environment = self.tmux("show-environment", "-g").stdout

    def assert_controls(self):
        self.assertEqual(self.tmux("show-environment", "-g").stdout, self.server_environment)
        self.assertEqual(int(self.tmux("list-panes", "-t", "=unrelated",
                                      "-F", "#{pane_pid}").stdout), self.other["pid"])
        self.assertEqual(self.tmux("show-environment", "-t", "=unrelated",
                                   "CLAUDE_CONFIG_DIR").stdout.strip(),
                         "CLAUDE_CONFIG_DIR=" + self.other_config)
        self.assertEqual(json.loads((self.receipts / f"{self.other['pid']}.json").read_text()),
                         self.other)
        args = self.run_command(["ps", "-p", str(self.other["pid"]), "-o", "args="]).stdout
        self.assertTrue(args.startswith(str(self.endpoint) + " --name unrelated"), args)

    def product(self, script, *args, expected=0):
        result = self.run_command([sys.executable, str(SCRIPTS / script),
                                   "--workspace", str(self.ws), "--repo", str(REPO), *args],
                                  expected=expected, timeout=45)
        self.assert_controls()
        return result

    def create(self, server_config):
        self.set_server_config(server_config)
        before = self.receipt_ids()
        made = json.loads(self.product(
            "create_worker.py", "--runtime", "claude", "--folder", str(self.folder),
            "--label", "Configuration regression worker", "--socket", self.socket, "--json").stdout)
        row = self.receipt(made["tmux"]["session_name"], before)
        self.assert_runtime(row, made, resumed=False)
        seed = json.loads((Path(self.expected) / ".claude.json").read_text())
        self.assertIs(seed["hasCompletedOnboarding"], True)
        self.assertIs(seed["hasCompletedClaudeInChromeOnboarding"], True)
        self.assertIs(seed["projects"][str(self.folder)]["hasTrustDialogAccepted"], True)
        settings = json.loads((Path(self.expected) / "settings.json").read_text())
        self.assertIs(settings["skipDangerousModePermissionPrompt"], True)
        self.assertFalse(Path(self.env["CLAUDE_CONFIG_DIR"]).exists())
        return made, row

    def assert_runtime(self, row, made, *, resumed):
        self.assertEqual(row["env"].get("CLAUDE_CONFIG_DIR"), self.expected)
        self.assertEqual(row["cwd"], made["cwd"])
        self.assertEqual(row["argv"][-2:], ["--", "/startup --worker"])
        flag, absent = ("--resume", "--session-id") if resumed else ("--session-id", "--resume")
        self.assertEqual(row["argv"][row["argv"].index(flag) + 1], made["runtime_session_id"])
        self.assertNotIn(absent, row["argv"])
        self.assertIn("--dangerously-skip-permissions", row["argv"])
        for key, value in {
            "SUTANDO_WORKER_ID": made["worker_id"], "SUTANDO_INSTANCE_ID": made["worker_id"],
            "SUTANDO_TASKS_DIR": made["delivery_dir"], "SUTANDO_INBOX_KIND": "deliveries",
            "SUTANDO_TMUX_SOCKET": self.socket,
            "SUTANDO_TMUX_SESSION": made["tmux"]["session_name"], "SUTANDO_CORE_SESSION": "",
        }.items():
            self.assertEqual(row["env"][key], value)

    def test_create_with_matching_server_config(self):
        self.create(self.expected)

    def test_create_with_different_server_config(self):
        self.create(str(self.base / "stale server config"))

    def test_create_with_absent_server_config(self):
        self.create(None)

    def worker_files(self):
        return {str(p.relative_to(self.ws)): p.read_bytes()
                for root in (self.ws / "state/workers", self.ws / "deliveries")
                for p in root.rglob("*") if p.is_file()}

    def interrupt(self, row, name):
        os.kill(row["pid"], signal.SIGINT)
        self.wait_for(lambda: process_exited(row["pid"]), "interrupted runtime exit")
        self.wait_for(lambda: self.tmux("has-session", "-t", "=" + name,
                                       expected=None).returncode == 1, "worker session exit")

    def test_resume_and_restart_preserve_worker_and_refuse_invalid_resumes(self):
        made, first = self.create(self.expected)
        worker_id, session_id = made["worker_id"], made["runtime_session_id"]
        name = made["tmux"]["session_name"]
        identity = self.ws / "state/workers" / worker_id
        lineage = (identity / "sessions.json").read_bytes()
        roster_path = self.ws / "state/roster.json"
        roster = roster_path.read_bytes()
        inbox = Path(made["delivery_dir"])
        pending = inbox / "pending-delivery"
        pending.write_text("keep this pending delivery")
        inbox_stat, pending_stat = inbox.stat(), pending.stat()
        self.set_server_config(str(self.base / "different restart configuration"))

        for sid, message in ((session_id, "already exists"), ("unknown-session", "no worker")):
            with self.subTest(refused=sid):
                before, files = self.receipt_ids(), self.worker_files()
                refused = self.product("spawn_worker.py", "--resume", sid,
                                       "--socket", self.socket, expected=2)
                self.assertIn(message, refused.stderr)
                self.assertEqual(self.receipt_ids(), before)
                self.assertEqual(self.worker_files(), files)
                self.assertEqual(roster_path.read_bytes(), roster)

        previous = first
        for count, script, args in (
            (2, "spawn_worker.py", ["--resume", session_id, "--socket", self.socket]),
            (3, "pool_remedy.py", ["--restart", worker_id]),
        ):
            with self.subTest(operation=script):
                self.interrupt(previous, name)
                before = self.receipt_ids()
                result = json.loads(self.product(script, *args).stdout)
                if script == "spawn_worker.py":
                    for key in ("worker_id", "label", "cwd", "delivery_dir", "tmux", "runtime_session_id"):
                        self.assertEqual(result[key], made[key])
                else:
                    self.assertEqual(result["result"], "restarted", result)
                    self.assertEqual(result["worker_id"], worker_id)
                    self.assertEqual(result["detail"]["session_id"], session_id)
                row = self.receipt(name, before)
                self.assertNotEqual(row["pid"], previous["pid"])
                previous = row
                self.assertEqual(len(self.receipt_ids() - before), 1)
                self.assert_runtime(row, made, resumed=True)
                self.assertEqual((identity / "sessions.json").read_bytes(), lineage)
                self.assertEqual(roster_path.read_bytes(), roster)
                self.assertEqual(sorted(p.name for p in identity.parent.iterdir() if p.is_dir()), [worker_id])
                runs = json.loads((identity / "incarnations.json").read_text())["incarnations"]
                self.assertEqual(len(runs), count)
                self.assertEqual(len({run["incarnation_id"] for run in runs}), count)
                for run in runs:
                    self.assertEqual(run["session_id"], session_id)
                    self.assertEqual(run["tmux"], made["tmux"])
                current = json.loads((identity / "current.json").read_text())
                self.assertEqual(current["session_id"], session_id)
                self.assertEqual(current["incarnation_id"], runs[-1]["incarnation_id"])
                self.assertEqual((inbox.stat().st_dev, inbox.stat().st_ino),
                                 (inbox_stat.st_dev, inbox_stat.st_ino))
                self.assertEqual((pending.stat().st_dev, pending.stat().st_ino),
                                 (pending_stat.st_dev, pending_stat.st_ino))
                self.assertEqual(pending.read_text(), "keep this pending delivery")

    @staticmethod
    def pid_exists(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def descendants(self, pid):
        result = self.run_command(["pgrep", "-P", str(pid)], expected=None)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        children = [int(p) for p in result.stdout.split()]
        return [child for pid in children for child in [*self.descendants(pid), pid]]

    def stop_server(self):
        if self.server is None:
            return
        descendants = []
        try:
            if self.server.poll() is None:
                descendants = self.descendants(self.server.pid)
            for pid in descendants:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 1
                while self.pid_exists(pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
            if self.server.poll() is None:
                self.tmux("kill-server", expected=None)
        finally:
            if self.server.poll() is None:
                self.server.terminate()
            try:
                self.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=5)
            self.events.append({"cleanup": {"tmux_pid": self.server.pid,
                                "returncode": self.server.returncode, "descendants": descendants,
                                "remaining": [p for p in descendants if self.pid_exists(p)]}})
            self.wait_for(lambda: not any(self.pid_exists(pid) for pid in descendants),
                          "disposable descendants to be reaped", timeout=5)

    def save_diagnostics(self):
        if RUNTIME_ROOT is not None:
            target = RUNTIME_ROOT / "diagnostics" / (self._testMethodName + ".json")
            target.write_text(json.dumps({
                "events": self.events,
                "receipts": [json.loads(p.read_text()) for p in self.receipts.glob("*.json")],
                "tmux_log": self.server_log.read_text()[-12000:] if self.server else "",
            }, indent=2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
