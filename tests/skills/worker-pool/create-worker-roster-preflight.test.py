#!/usr/bin/env python3
"""Fresh creation processes must refuse bad rosters before launching anything.

Real private tmux runs a synthetic endpoint, proving launch and preservation of
live processes, not model conversations. Only host timer installation is stubbed.
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

socket_setting = os.environ.get("SUTANDO_RUNTIME_SOCKET")
environment_file = Path(socket_setting).parent / "environment.json" if socket_setting else None
SUPPLIED = (json.loads(environment_file.read_text())
            if environment_file is not None and environment_file.is_file() else {})
RUNTIME_ROOT = environment_file.parent if SUPPLIED else None
ROOM = "roster-preflight-room"

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


class RosterPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="roster-", dir=RUNTIME_ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.ws = self.base / "workspace"
        self.home = self.base / "home"
        self.folder = self.base / "project"
        self.bin = self.base / "bin"
        self.hooks = self.base / "hooks"
        for path in (self.ws / "state", self.home, self.folder, self.bin, self.hooks):
            path.mkdir(parents=True)
        self.socket = str(self.base / "tmux.sock")
        self.roster = self.ws / "state/roster.json"
        self.boundary_log = self.base / "boundary.jsonl"
        self.late_failure = self.base / "late-failure"
        self.env = hermetic_env(home=self.home, **SUPPLIED)
        self.env.update({
            "HOME": str(self.home), "SHELL": "/bin/bash",
            "PATH": str(self.bin) + os.pathsep + self.env["PATH"],
            "PYTHONPATH": str(self.hooks) + os.pathsep + self.env.get("PYTHONPATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1", "SUTANDO_TEST_MODE": "1",
            "SUTANDO_PACKAGED": "", "SUTANDO_APP_SUPPORT": "",
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
            "CLAUDE_CONFIG_DIR": str(self.base / "claude"),
            "SUTANDO_ACCEPT_BYPASS_PERMISSIONS": "1",
            "SUTANDO_TASK_EVENT_HANDLER": str(SCRIPTS / "pool_route_handler.py"),
        })
        self.events = []
        self.server = None
        self.addCleanup(self.save_diagnostics)
        self.addCleanup(self.stop_server)
        for tool in ("tmux", "cc"):
            if not shutil.which(tool, path=self.env["PATH"]):
                self.skipTest(f"real {tool} is required")
        for tool in ("launchctl", "osascript"):
            self.executable(self.bin / tool, "#!/bin/sh\nexit 1\n")
        self.install_timer_boundary()
        source = RUNTIME_ROOT / "runtime-endpoint.c" if RUNTIME_ROOT else None
        if source is None or not source.is_file():
            source = self.base / "endpoint.c"
            source.write_text(ENDPOINT)
        self.endpoint = self.base / "endpoint/claude"
        self.endpoint.parent.mkdir()
        self.run_command(["cc", str(source), "-o", str(self.endpoint)])
        self.executable(self.bin / "claude", "#!/bin/sh\nexec " + shlex.quote(str(self.endpoint)) + ' "$@"\n')
        self.server_log = self.base / "tmux.log"
        argv = ["tmux", "-D", "-f", "/dev/null", "-S", self.socket]
        with self.server_log.open("w") as log:
            self.server = subprocess.Popen(argv, cwd=REPO, env=self.env,
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        self.events.append({"at": self.now(), "argv": argv, "pid": self.server.pid})
        self.wait_for(lambda: Path(self.socket).exists() or self.server.poll() is not None,
                      "foreground tmux socket")
        self.assertIsNone(self.server.poll(), self.server_log.read_text())
        self.tmux("set-option", "-g", "default-shell", "/bin/bash")
        pane = self.tmux("new-session", "-d", "-s", "control", "-P", "-F", "#{pane_id}",
                         str(self.endpoint), "--name", "control").stdout.strip()
        self.wait_for(lambda: "synthetic endpoint ready" in self.tmux(
            "capture-pane", "-p", "-t", pane).stdout, "control session readiness")

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def executable(path, content):
        path.write_text(content)
        path.chmod(0o755)

    def install_timer_boundary(self):
        # Fresh CLI entrypoints keep the real launcher and registration writer.
        # The timer boundary records calls and cannot reach host installation.
        (self.hooks / "sitecustomize.py").write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            f"if Path(sys.argv[0]).resolve() == Path({str(SCRIPTS / 'create_worker.py')!r}):\n"
            f"    sys.path.insert(0, {str(SCRIPTS)!r})\n"
            "    import spawn_worker\n"
            "    def record(kind, **fields):\n"
            f"        with open({str(self.boundary_log)!r}, 'a') as log:\n"
            "            log.write(json.dumps({'kind': kind, 'pid': os.getpid(), **fields}) + '\\n')\n"
            "    def timer(workspace, repo, **kwargs):\n"
            "        record('timer-suppressed')\n"
            f"        if Path({str(self.late_failure)!r}).exists():\n"
            "            (Path(workspace) / 'state/roster.json').write_text('{late invalid')\n"
            "            record('late-roster-failure')\n"
            "        return {'ensured': False, 'why': 'test suppresses host timer installation'}\n"
            "    spawn_worker.ensure_remedy_timer = timer\n"
            "    def audit(event, args):\n"
            "        if event == 'subprocess.Popen':\n"
            "            record('subprocess', argv=args[1])\n"
            "    sys.addaudithook(audit)\n"
            "    record('ready')\n")
        compile((self.hooks / "sitecustomize.py").read_text(), "sitecustomize.py", "exec")

    def run_command(self, argv, *, expected=0, timeout=30):
        started = self.now()
        result = subprocess.run(argv, cwd=REPO, env=self.env, capture_output=True,
                                text=True, timeout=timeout)
        self.events.append({"at": started, "argv": list(argv), "rc": result.returncode,
                            "stdout": result.stdout[-10000:], "stderr": result.stderr[-10000:]})
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

    def create(self, *args, expected=0):
        return self.run_command([
            sys.executable, str(SCRIPTS / "create_worker.py"), "--workspace", str(self.ws),
            "--repo", str(REPO), "--runtime", "claude", "--folder", str(self.folder),
            "--label", "Preflight worker", "--socket", self.socket, "--json", *args,
        ], expected=expected, timeout=45)

    def boundaries(self):
        return ([json.loads(line) for line in self.boundary_log.read_text().splitlines()]
                if self.boundary_log.exists() else [])

    def panes(self):
        return self.tmux("list-panes", "-a", "-F", "#{session_name} #{pane_pid}").stdout

    def live_worker(self, name):
        pid = int(self.tmux("list-panes", "-t", "=" + name, "-F", "#{pane_pid}").stdout)
        args = self.run_command(["ps", "-p", str(pid), "-o", "args="]).stdout.strip()
        self.assertTrue(args.startswith(str(self.endpoint) + " "), args)
        self.assertIn("-- /startup --worker", args)
        pane = self.tmux("list-panes", "-t", "=" + name, "-F", "#{pane_id}").stdout.strip()
        self.assertIn("synthetic endpoint ready", self.tmux("capture-pane", "-p", "-t", pane).stdout)
        return pid

    def snapshot(self):
        result = {}
        for root in (self.ws, self.home, self.base / "run", self.base / "registry",
                     self.base / "claude"):
            for path in sorted(root.rglob("*")):
                stat = path.stat()
                content = None
                if path.is_file():
                    try:
                        content = path.read_bytes()
                    except PermissionError:
                        content = "unreadable"
                result[str(path.relative_to(self.base))] = (stat.st_ino, stat.st_mode, content)
        return result

    def assert_existing_work_unchanged(self, before):
        after = self.snapshot()
        for name, state in before.items():
            if name.startswith(("workspace/tasks/", "workspace/deliveries/",
                                "workspace/state/workers/")) or name == "workspace/state/bindings.json":
                self.assertEqual(after[name], state, name)

    def existing_work(self):
        self.assertFalse(self.roster.exists())
        preview = json.loads(self.create("--dry-run").stdout)
        self.assertEqual(preview["unrostered_records"], [])
        self.assertFalse(self.roster.exists())
        made = json.loads(self.create("--room", ROOM).stdout)
        self.assertTrue(made["started"])
        self.assertEqual(made["roster_version"], 1)
        pid = self.live_worker(made["tmux"]["session_name"])
        result = self.run_command([
            sys.executable, "-c",
            "import json, sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import pool_router as router\nimport pool_delivery as delivery\n"
            "ws, wid, room = Path(sys.argv[2]), sys.argv[3], sys.argv[4]\n"
            "(ws / 'tasks').mkdir(exist_ok=True)\n"
            "for tid in ('task-pending', 'task-accepted'):\n"
            "    (ws / 'tasks' / (tid + '.txt')).write_text(\n"
            "        f'id: {tid}\\nsource: test\\nchannel_id: {room}\\ntask: keep this payload\\n')\n"
            "    assert router.route(ws, {'id': tid, 'channel_id': room})['delivered'] == [wid]\n"
            "delivery.accept(ws / 'deliveries' / wid / 'task-accepted.txt')\n"
            "print(json.dumps({tid: router.committed_recipient(ws, tid)\n"
            "                  for tid in ('task-pending', 'task-accepted')}))\n",
            str(SCRIPTS), str(self.ws), made["worker_id"], ROOM,
        ])
        self.assertEqual(json.loads(result.stdout), {
            "task-pending": made["worker_id"], "task-accepted": made["worker_id"],
        })
        self.events.append({"ready": True, "tmux_pid": self.server.pid,
                            "worker_pid": pid, "worker_id": made["worker_id"]})
        return made, pid

    def assert_refusals(self, explanation):
        before = self.snapshot()
        panes = self.panes()
        for attempt in range(2):
            for args in ((), ("--dry-run",)):
                with self.subTest(attempt=attempt, args=args):
                    offset = len(self.boundaries())
                    result = self.create(*args, expected=2)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("create-worker: " + explanation, result.stderr)
                    self.assertNotIn("was created", result.stderr)
                    events = self.boundaries()[offset:]
                    self.assertEqual([event["kind"] for event in events], ["ready"])
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(self.panes(), panes)

    def test_malformed_rosters_preserve_live_work_and_restoration_allows_one_create(self):
        made, pid = self.existing_work()
        valid = self.roster.read_bytes()
        wid = made["worker_id"]
        invalid = [
            ("{not json", "roster is not valid JSON"),
            ("[]", "roster is missing 'workers'"),
            ("{}", "roster is missing 'workers'"),
            ('{"workers": []}', "workers must be an object"),
            (json.dumps({"workers": {wid: None}}), f"worker {wid!r} row must be an object"),
            ('{"workers": {"bad id": {"state": "live"}}}', "worker id must match"),
            (json.dumps({"workers": {wid: {"state": "idle"}}}), f"worker {wid!r} has state"),
            (json.dumps({"workers": {wid: {"state": "live", "display_label": " "}}}),
             f"worker {wid!r} has an invalid display label"),
        ]
        for text, explanation in invalid:
            with self.subTest(roster=text):
                self.roster.write_text(text)
                self.assert_refusals(explanation)
                self.assertEqual(self.roster.read_text(), text)
                self.assertEqual(self.live_worker(made["tmux"]["session_name"]), pid)
        self.roster.write_bytes(valid)
        before = self.snapshot()
        self.create("--dry-run")
        self.assertEqual(self.snapshot(), before)
        second = json.loads(self.create().stdout)
        self.assertNotEqual(second["worker_id"], wid)
        roster = json.loads(self.roster.read_text())
        self.assertEqual(set(roster["workers"]), {wid, second["worker_id"]})
        self.assertEqual(roster["version"], 2)
        self.assertEqual(roster["bindings"], {ROOM: wid})
        names = {made["tmux"]["session_name"], second["tmux"]["session_name"]}
        self.assertEqual({line.split()[0] for line in self.panes().splitlines()},
                         {"control", *names, *(name + "-watcher" for name in names)})
        self.assertEqual(self.live_worker(made["tmux"]["session_name"]), pid)
        self.live_worker(second["tmux"]["session_name"])
        self.assert_existing_work_unchanged(before)
        self.assertEqual(sum(e["kind"] == "timer-suppressed" for e in self.boundaries()), 2)

    @unittest.skipIf(os.geteuid() == 0, "root ignores file permissions")
    def test_unreadable_roster_refuses_without_changing_live_work(self):
        made, pid = self.existing_work()
        valid = self.roster.read_bytes()
        self.roster.chmod(0o000)
        try:
            probe = self.run_command([
                sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).read_bytes()",
                str(self.roster),
            ], expected=1)
            self.assertIn("PermissionError", probe.stderr)
            self.assert_refusals("roster unreadable")
            self.assertEqual(self.live_worker(made["tmux"]["session_name"]), pid)
        finally:
            self.roster.chmod(0o644)
        self.assertEqual(self.roster.read_bytes(), valid)

    def test_failure_after_preflight_reports_and_retains_the_live_worker(self):
        made, pid = self.existing_work()
        before = self.snapshot()
        self.late_failure.touch()
        offset = len(self.boundaries())
        result = self.create(expected=1)
        events = self.boundaries()[offset:]
        self.assertEqual([e["kind"] for e in events if e["kind"] != "subprocess"],
                         ["ready", "timer-suppressed", "late-roster-failure"])
        new_ids = {p.name for p in (self.ws / "state/workers").iterdir() if p.is_dir()} - {made["worker_id"]}
        self.assertEqual(len(new_ids), 1)
        wid = new_ids.pop()
        self.assertIn(f"worker {wid} was created, but the roster could not be compiled", result.stderr)
        self.assertIn("roster is not valid JSON", result.stderr)
        self.assertIn("it will receive nothing until the roster names it", result.stderr)
        self.assertEqual(self.roster.read_text(), "{late invalid")
        self.assertTrue((self.ws / "deliveries" / wid).is_dir())
        self.assertEqual(self.live_worker(made["tmux"]["session_name"]), pid)
        runs = json.loads((self.ws / "state/workers" / wid / "incarnations.json").read_text())
        self.assertEqual(len(runs["incarnations"]), 1)
        run = runs["incarnations"][0]
        self.assertIsNone(run["ended_at"])
        self.assertEqual(run["tmux"]["socket"], self.socket)
        retained_pid = self.live_worker(run["tmux"]["session_name"])
        self.assertNotEqual(retained_pid, pid)
        self.events.append({"retained_worker_id": wid, "retained_worker_pid": retained_pid})
        self.assert_existing_work_unchanged(before)

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
            self.wait_for(lambda: not any(self.pid_exists(pid) for pid in descendants),
                          "disposable descendants to be reaped", timeout=5)
            self.events.append({"cleanup": {"tmux_pid": self.server.pid,
                                "returncode": self.server.returncode,
                                "descendants": descendants, "remaining": []}})

    def save_diagnostics(self):
        if RUNTIME_ROOT is not None:
            (RUNTIME_ROOT / "diagnostics" / (self._testMethodName + ".json")).write_text(json.dumps({
                "events": self.events, "boundaries": self.boundaries(),
                "tmux_log": self.server_log.read_text()[-10000:] if self.server else "",
            }, indent=2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
