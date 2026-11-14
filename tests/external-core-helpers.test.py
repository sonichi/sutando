#!/usr/bin/env python3
"""Real helper receipts and guarded Codex launch without a provider or live workspace."""
import contextlib
import io
import runpy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(os.environ.get("SUTANDO_TEST_REPO", Path(__file__).resolve().parents[1])).resolve()
sys.path.insert(0, str(ROOT / "src"))
import external_core_helpers as helpers


class RealHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="sutando-external-")
        cls.base = Path(cls.temporary.name).resolve()
        cls.repo = cls.base / "repo with spaces"
        shutil.copytree(ROOT / "src", cls.repo / "src", ignore=shutil.ignore_patterns("__pycache__"))
        (cls.repo / "scripts").mkdir()
        for name in ("python-binary.sh", "sutando-config.sh"):
            shutil.copy2(ROOT / "scripts" / name, cls.repo / "scripts" / name)
        cls.workspace = cls.base / "workspace"
        cls.receipts = cls.workspace / "state" / "external"
        cls.receipts.mkdir(parents=True, mode=0o700)
        (cls.repo / "sutando.config.json").write_text(json.dumps({
            "workspace": {"path": str(cls.workspace)}, "core": {"runtime": "codex"},
        }))
        cls.socket = cls.base / "private.sock"
        cls.session = "receipt-core"
        cls.bin = cls.base / "bin"
        cls.bin.mkdir()
        cls.tmux_log = cls.base / "tmux.log"
        cls.core_mark = cls.base / "core.started"
        tmux = cls.bin / "tmux"
        tmux.write_text("""#!/bin/bash
printf '%s\n' "$*" >> "$TEST_TMUX_LOG"
[ "${1:-}" = -S ] && shift 2
case "${1:-}" in
  has-session) [ -f "$TEST_CORE_MARK" ] && [ "${3:-}" = "=$SUTANDO_TMUX_SESSION" ]; exit $?;;
  show-environment) printf 'SUTANDO_CORE_RUNTIME=codex\n';;
  new-session)
    if [ "${4:-}" = "$SUTANDO_TMUX_SESSION" ]; then
      touch "$TEST_CORE_MARK"
      [ -z "${TEST_DIE_DURING_LAUNCH:-}" ] || kill -TERM "$TEST_DIE_DURING_LAUNCH"
    fi;;
  list-sessions|display-message|list-panes|capture-pane) exit 1;;
esac
exit 0
""")
        tmux.chmod(0o755)
        for name, body in {
            "codex": '\n'.join(['#!/bin/bash', 'if [ "${1:-}" = login ]; then',
                                '  if [ -n "${TEST_DIE_DURING_AUTH:-}" ]; then',
                                '    kill -KILL "$TEST_DIE_DURING_AUTH"; sleep 0.1',
                                '  fi', '  exit 0', 'fi', 'exit 99', '']),
            "fswatch": '#!/bin/bash\nexit 0\n',
            "uname": '#!/bin/bash\nprintf "Linux\n"\n',
        }.items():
            f = cls.bin / name
            f.write_text(body)
            f.chmod(0o755)
        cls.env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ}
        cls.env.update({"PATH": str(cls.bin) + os.pathsep + cls.env["PATH"],
                        "SUTANDO_PY": sys.executable, "DO_NOT_TRACK": "1",
                        "SUTANDO_TMUX_SOCKET": str(cls.socket), "SUTANDO_TMUX_SESSION": cls.session,
                        "SUTANDO_HOST_LABEL": "receipt-fixture", "TEST_TMUX_LOG": str(cls.tmux_log),
                        "TEST_CORE_MARK": str(cls.core_mark)})

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.processes = []
        for path in self.receipts.glob("*.json"):
            path.unlink()
        self.core_mark.unlink(missing_ok=True)
        self.tmux_log.unlink(missing_ok=True)

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def start(self, role, *, passive=True, socket=None, env=None, extra=()):
        args = [sys.executable, "-B", str(self.repo / "src" / helpers.SCRIPTS[role])]
        if role == "monitor":
            args += ["--socket", str(socket or self.socket), "--session", self.session,
                     "--out", str(self.workspace / "state/core-supervisor.json"), "--interval", "0.1"]
            if passive:
                args += ["--no-auto-answer", "--no-chat-escalation"]
        args += ["--helper-receipt-dir", str(self.receipts), *extra]
        output = self.base / (role + ".output")
        with output.open("w") as stream:
            process = subprocess.Popen(args, env=env or self.env, stdout=stream, stderr=stream)
        self.processes.append(process)
        receipt = self.receipts / (role + ".json")
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and process.poll() is None and not receipt.exists():
            time.sleep(0.02)
        return process, receipt, output

    def pair(self):
        processes = []
        for role in helpers.SCRIPTS:
            process, receipt, output = self.start(role)
            self.assertTrue(receipt.exists(), output.read_text())
            self.assertIsNone(process.poll(), output.read_text())
            processes.append(process)
        return processes

    def validate(self, **kw):
        args = dict(directory=self.receipts, repo=self.repo, workspace=self.workspace,
                    socket=self.socket, session=self.session)
        args.update(kw)
        return helpers.validate(**args)

    @contextlib.contextmanager
    def helper_tripwire(self):
        # Running helpers keep their loaded code; any new start of these copies marks this test's launch.
        mark = self.base / "helper-restarted"
        mark.unlink(missing_ok=True)
        saved = {}
        for name in helpers.SCRIPTS.values():
            script = self.repo / "src" / name
            saved[script] = script.read_bytes()
            script.write_text("import pathlib\n"
                              f"pathlib.Path({str(mark)!r}).open('a').write({name!r} + '\\n')\n")
        try:
            yield mark
        finally:
            for script, body in saved.items():
                script.write_bytes(body)

    def launch(self, *extra, env=None):
        return subprocess.run(["bash", str(self.repo / "src/agent/start-cli.sh"), "--runtime", "codex",
                               *extra, "--external-helpers", str(self.receipts)], env=env or self.env,
                              capture_output=True, text=True, timeout=25)

    def test_real_helpers_publish_bound_receipts_without_a_core(self):
        processes = self.pair()
        got = self.validate()
        self.assertEqual([got[r]["pid"] for r in helpers.SCRIPTS], [p.pid for p in processes])
        self.assertFalse(list((self.workspace / "state/cores").glob("*.alive")))
        for role in helpers.SCRIPTS:
            self.assertEqual((self.receipts / (role + ".json")).stat().st_mode & 0o777, 0o600)

    def test_passive_continuous_contract_is_enforced_by_actual_helpers(self):
        for role, kwargs in [("monitor", {"passive": False}), ("heartbeat", {"extra": ["--once"]})]:
            with self.subTest(role=role):
                process, receipt, _ = self.start(role, **kwargs)
                self.assertNotEqual(process.wait(timeout=8), 0)
                self.assertFalse(receipt.exists())

    def test_kernel_argv_and_receipt_identity_refuse_replacement_or_mentions(self):
        self.pair()
        file = self.receipts / "monitor.json"
        original = json.loads(file.read_text())
        controls = [{"pid": os.getpid(), **helpers._process(os.getpid())},
                    {"lstart": "different"}, {"uid": original["uid"] + 1},
                    {"script": str(self.repo / "src/other.py")}, {"passive": False},
                    {"workspace": str(self.base)}, {"socket": str(self.base / "other.sock")},
                    {"session": "another-session"}, {"output": str(self.base / "other.json")}]
        for delta in controls:
            with self.subTest(delta=delta):
                file.write_text(json.dumps({**original, **delta}))
                with self.assertRaises((ValueError, OSError)):
                    self.validate()
        file.write_text(json.dumps(original))
        with mock.patch.object(helpers, "proc_argv_vector", return_value=None):
            with self.assertRaises(ValueError):
                self.validate()
        with mock.patch.object(helpers, "proc_argv_vector", return_value=[sys.executable, "-c", str(self.repo / "src/core-input-watch.py")]):
            with self.assertRaises(ValueError):
                self.validate()

    def test_actual_wrong_socket_and_heartbeat_environment_are_rejected(self):
        p, _, _ = self.start("monitor", socket=self.base / "wrong.sock")
        self.start("heartbeat")
        with self.assertRaises(ValueError):
            self.validate()
        p.terminate(); p.wait(timeout=5)
        (self.receipts / "monitor.json").unlink()
        self.start("monitor")
        hb = self.processes[1]; hb.terminate(); hb.wait(timeout=5)
        (self.receipts / "heartbeat.json").unlink()
        self.start("heartbeat", env={**self.env, "SUTANDO_TMUX_SESSION": "wrong"})
        with self.assertRaises(ValueError):
            self.validate()

    def test_private_receipt_file_and_directory_are_required(self):
        self.pair()
        file = self.receipts / "monitor.json"
        data = file.read_bytes()
        file.chmod(0o644)
        with self.assertRaises(ValueError): self.validate()
        file.chmod(0o600)
        self.receipts.chmod(0o755)
        try:
            with self.assertRaises(ValueError): self.validate()
        finally:
            self.receipts.chmod(0o700)
        file.unlink(); file.symlink_to(self.receipts / "heartbeat.json")
        with self.assertRaises(OSError): self.validate()
        file.unlink(); file.write_bytes(data); file.chmod(0o600)
        with self.assertRaises(ValueError): self.validate(workspace=self.base / "other")

    def test_absent_and_dead_helpers_fail_before_core_launch(self):
        result = self.launch()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.core_mark.exists())
        processes = self.pair()
        processes[1].terminate(); processes[1].wait(timeout=5)
        result = self.launch()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.core_mark.exists())
        self.assertIsNone(processes[0].poll())

    def test_valid_launch_and_restart_do_not_manage_owned_helpers(self):
        processes = self.pair()
        identities = self.validate()
        with self.helper_tripwire() as started:
            for extra in [(), ("--restart",)]:
                result = self.launch(*extra)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(self.core_mark.exists())
                self.assertEqual(self.validate(), identities)
                self.assertTrue(all(p.poll() is None for p in processes))
                time.sleep(0.3)
                self.assertFalse(started.exists(), started.read_text() if started.exists() else "")

    def test_concurrent_writer_of_host_global_helper_logs_does_not_fail_launch(self):
        stat, read_bytes, ticks = Path.stat, Path.read_bytes, iter(range(1, 10**6))
        logs = {f"/tmp/core-{n}.log" for n in ("input-watch", "heartbeat")}

        def fake_stat(path, *a, **kw):
            if str(path) in logs:
                return mock.Mock(st_mtime_ns=next(ticks), st_mode=0o100600)
            return stat(path, *a, **kw)

        def fake_read(path):
            return str(next(ticks)).encode() if str(path) in logs else read_bytes(path)

        with mock.patch.object(Path, "stat", fake_stat), mock.patch.object(Path, "read_bytes", fake_read):
            self.test_valid_launch_and_restart_do_not_manage_owned_helpers()

    def test_no_schedule_reconcile_keeps_real_helpers_and_skips_installers(self):
        processes = self.pair()
        identities = self.validate()
        installer_log = self.base / "unexpected-installer.log"
        scripts = [self.repo / "skills" / rel for rel in (
            "schedule-crons/scripts/reconcile_launchd.py",
            "schedule-crons/scripts/codex-scheduler.py",
            "proactive-loop/scripts/codex-auto-reset-timer.py",
        )]
        uname = self.bin / "uname"
        original = uname.read_bytes()
        try:
            uname.write_text('#!/bin/bash\nprintf "Darwin\\n"\n')
            for script in scripts:
                script.parent.mkdir(parents=True, exist_ok=True)
                script.write_text("import os, pathlib\n"
                                  "pathlib.Path(os.environ['TEST_INSTALLER_LOG']).touch()\n")
            for extra in [(), ("--restart",)]:
                result = self.launch(*extra, "--no-schedule-reconcile",
                                     env={**self.env, "TEST_INSTALLER_LOG": str(installer_log)})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue(self.core_mark.exists())
                self.assertFalse(installer_log.exists())
                self.assertEqual(self.validate(), identities)
                self.assertTrue(all(p.poll() is None for p in processes))
        finally:
            uname.write_bytes(original)
            for script in scripts:
                script.unlink(missing_ok=True)

    def test_helper_exit_after_preflight_is_rejected_before_creating_core(self):
        for index in (0, 1):
            with self.subTest(helper=index):
                processes = self.pair()
                result = self.launch(env={**self.env, "TEST_DIE_DURING_AUTH": str(processes[index].pid)})
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(self.core_mark.exists())
                self.assertIsNone(processes[1 - index].poll())
                self.tearDown()
                self.processes = []
                for path in self.receipts.glob("*.json"):
                    path.unlink()

    def test_helper_exit_during_core_launch_fails_postcheck_without_replacement(self):
        processes = self.pair()
        original = (self.receipts / "monitor.json").read_bytes()
        result = self.launch(env={**self.env, "TEST_DIE_DURING_LAUNCH": str(processes[0].pid)})
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.core_mark.exists())
        self.assertEqual((self.receipts / "monitor.json").read_bytes(), original)
        self.assertIsNone(processes[1].poll())

    def test_later_valid_replacement_cannot_change_initial_identity(self):
        self.pair()
        command = [sys.executable, str(self.repo / "src/external_core_helpers.py"),
                   str(self.receipts), "--socket", str(self.socket), "--session", self.session]
        first = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=8)
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.receipts / "monitor.json").unlink()
        replacement, receipt, output = self.start("monitor")
        self.assertTrue(receipt.exists(), output.read_text())
        self.assertEqual(self.validate()["monitor"]["pid"], replacement.pid)
        changed = subprocess.run(command + ["--expected", first.stdout.strip()],
                                 env=self.env, capture_output=True, text=True, timeout=8)
        self.assertNotEqual(changed.returncode, 0)

    def test_fifo_receipt_is_rejected_without_waiting_for_a_writer(self):
        fifo = self.receipts / "monitor.json"
        os.mkfifo(fifo, 0o600)
        code = "import sys; sys.path.insert(0, sys.argv[1]); from external_core_helpers import _read; _read(sys.argv[2])"
        try:
            result = subprocess.run([sys.executable, "-c", code, str(ROOT / "src"), str(fifo)],
                                    capture_output=True, text=True, timeout=2)
        except subprocess.TimeoutExpired:
            self.fail("a FIFO receipt blocked before type validation")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("helper receipt must be a bounded owner-private file", result.stderr)

    def test_readonly_verifier_pins_identity_and_sanitizes_failure(self):
        self.pair()
        argv = ["verify", str(self.receipts), "--socket", str(self.socket), "--session", self.session]
        import workspace_default
        with mock.patch.object(helpers, "__file__", str(self.repo / "src/external_core_helpers.py")), \
                mock.patch.object(workspace_default, "resolve_workspace", return_value=self.workspace):
            out = io.StringIO()
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
                self.assertEqual(helpers.main(), 0)
            fingerprint = out.getvalue().strip()
            self.assertEqual(len(fingerprint), 64)
            with mock.patch.object(sys, "argv", argv + ["--expected", fingerprint]), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(helpers.main(), 0)
            for extra in (["--expected", "changed"], []):
                if not extra:
                    (self.receipts / "monitor.json").write_text('{"SECRET": true}')
                err = io.StringIO()
                with mock.patch.object(sys, "argv", argv + extra), contextlib.redirect_stderr(err):
                    self.assertEqual(helpers.main(), 1)
                self.assertEqual(err.getvalue(), "external core helpers are missing, changed, or not passive\n")

    def test_atomic_publication_retains_previous_receipt_on_write_failure(self):
        args = (self.receipts, "monitor", self.repo / "src/core-input-watch.py",
                self.workspace, self.socket, self.session)
        helpers.publish(*args, passive=True, output=self.workspace / "state/core-supervisor.json")
        file = self.receipts / "monitor.json"
        record = json.loads(file.read_text())
        self.assertEqual(record["pid"], os.getpid())
        self.assertEqual(record["lstart"], helpers._process(os.getpid())["lstart"])
        self.assertEqual(record["output"], str(self.workspace / "state/core-supervisor.json"))
        original = file.read_bytes()
        with mock.patch.object(helpers.os, "replace", side_effect=OSError("private path")):
            with self.assertRaises(OSError): helpers.publish(*args, passive=True)
        self.assertEqual(file.read_bytes(), original)
        self.assertFalse(list(self.receipts.glob(".monitor-*")))
        with self.assertRaises(ValueError): helpers.publish(*args, passive=False)
        with self.assertRaises(ValueError): helpers.publish(self.receipts, "invented", *args[2:], passive=True)

    def test_unobservable_process_and_malformed_receipts_fail_closed(self):
        for pid in (True, 1, "2"):
            with self.assertRaises(ValueError): helpers._process(pid)
        for output in ("", f"{os.getuid()} Mon Sep 30 12:00:00 2026 Z",
                       f"{os.getuid() + 1} Mon Sep 30 12:00:00 2026 S"):
            with mock.patch.object(helpers.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output)):
                with self.assertRaises(ValueError): helpers._process(99)
        self.pair()
        file = self.receipts / "monitor.json"
        for data in ("[]", "x" * 8193):
            file.write_text(data)
            with self.assertRaises(ValueError): self.validate()
        with mock.patch.object(helpers, "proc_argv_vector", return_value=["node", "private"]):
            with self.assertRaises(ValueError): helpers._script_argv(99, Path("private"))
        self.assertEqual(helpers._option(["--socket=value"], "--socket"), "value")
        for args in (["--socket"], [], ["--socket=a", "--socket", "b"]):
            with self.assertRaises(ValueError): helpers._option(args, "--socket")

    def test_receipt_cannot_override_changed_live_policy_or_identity(self):
        processes = self.pair()
        originals = {p.pid: helpers.proc_argv_vector(p.pid) for p in processes}
        identities = self.validate()
        for role, process in zip(helpers.SCRIPTS, processes):
            original = originals[process.pid]
            script = str(self.repo / "src" / helpers.SCRIPTS[role])
            cases = [("other checkout", [str(self.base / helpers.SCRIPTS[role]) if a == script else a
                                         for a in original], "selected checkout script"),
                     ("receipt directory", [str(self.base) if a == str(self.receipts) else a
                                            for a in original], "receipt invocation mismatch")]
            cases += [(flag, original + [flag], "not continuous")
                      for flag in ("--once", "--stop", "--mark-stopped")]
            if role == "monitor":
                cases += [(flag, [a for a in original if a != flag], "monitor is not passive")
                          for flag in ("--no-auto-answer", "--no-chat-escalation")]
                for flag in ("--socket", "--session", "--out"):
                    altered = original.copy()
                    altered[altered.index(flag) + 1] = str(self.base / "changed")
                    cases.append((flag, altered, "monitor invocation configuration mismatch"))
            for label, altered, reason in cases:
                with self.subTest(role=role, field=label), mock.patch.object(
                    helpers, "proc_argv_vector",
                    side_effect=lambda pid: altered if pid == process.pid else originals[pid],
                ):
                    with self.assertRaisesRegex(ValueError, reason): self.validate()
        self.assertEqual(self.validate(), identities)

    def test_each_helper_identity_is_rechecked_after_its_argv(self):
        processes = self.pair()
        identities = {p.pid: helpers._process(p.pid) for p in processes}
        read_argv = helpers.proc_argv_vector
        for process in processes:
            observed = set()

            def argv(pid):
                result = read_argv(pid)
                observed.add(pid)
                return result

            def observe(pid):
                identity = identities[pid]
                return ({**identity, "lstart": "changed"}
                        if pid == process.pid and pid in observed else identity)

            with self.subTest(pid=process.pid), \
                    mock.patch.object(helpers, "_process", side_effect=observe), \
                    mock.patch.object(helpers, "proc_argv_vector", side_effect=argv):
                with self.assertRaisesRegex(ValueError, "helper changed during validation"):
                    self.validate()
        self.assertEqual(self.validate(), {role: identities[p.pid]
                                          for role, p in zip(helpers.SCRIPTS, processes)})

    def test_separately_valid_roles_cannot_share_one_process(self):
        processes = self.pair()
        originals = [helpers.proc_argv_vector(p.pid) for p in processes]
        file = self.receipts / "heartbeat.json"
        record = json.loads(file.read_text())
        file.write_text(json.dumps({**record, **helpers._process(processes[0].pid)}))
        with mock.patch.object(helpers, "proc_argv_vector", side_effect=originals):
            with self.assertRaisesRegex(ValueError, "helpers must be separate processes"):
                self.validate()

    def test_receipt_directory_must_resolve_below_workspace_state(self):
        self.assertEqual(helpers._directory(self.receipts, self.workspace), self.receipts)
        outside = self.base / "outside-state"
        outside.mkdir(mode=0o700)
        link = self.workspace / "state" / "outside-link"
        link.symlink_to(outside, target_is_directory=True)
        try:
            for directory in (outside, link):
                with self.subTest(directory=directory):
                    with self.assertRaisesRegex(ValueError, "below workspace/state"):
                        helpers._directory(directory, self.workspace)
        finally:
            link.unlink()
            outside.rmdir()

    def test_valid_json_receipt_still_requires_bounded_size_and_owner(self):
        file = self.receipts / "monitor.json"
        record = {"version": 1, "padding": ""}
        file.write_text(json.dumps(record))
        file.chmod(0o600)
        self.assertEqual(helpers._read(file), record)
        file.write_text(json.dumps({**record, "padding": "x" * 8192}))
        with self.assertRaisesRegex(ValueError, "bounded owner-private file"):
            helpers._read(file)
        file.write_text(json.dumps(record))
        foreign = list(file.stat())
        foreign[4] = os.getuid() + 1
        with mock.patch.object(helpers.os, "fstat", return_value=os.stat_result(foreign)):
            with self.assertRaisesRegex(ValueError, "bounded owner-private file"):
                helpers._read(file)
        self.assertEqual(helpers._read(file), record)

    def test_helper_hooks_publish_actual_passive_arguments_before_work(self):
        import workspace_default
        import core_heartbeat as heartbeat
        class Published(Exception): pass
        monitor = runpy.run_path(str(ROOT / "src/core-input-watch.py"))["main"]
        argv = ["monitor", "--socket", str(self.socket), "--session", self.session,
                "--out", str(self.workspace / "state/core-supervisor.json"),
                "--no-auto-answer", "--no-chat-escalation", "--helper-receipt-dir", str(self.receipts)]
        with mock.patch.object(helpers, "publish", side_effect=Published) as publish, \
                mock.patch.object(workspace_default, "resolve_workspace", return_value=self.workspace), \
                mock.patch.object(sys, "argv", argv):
            with self.assertRaises(Published): monitor()
            self.assertEqual(publish.call_args.args[:2], (str(self.receipts), "monitor"))
            self.assertEqual(publish.call_args.args[3:], (self.workspace, str(self.socket), self.session))
            self.assertEqual(publish.call_args.kwargs, {"passive": True, "output": str(self.workspace / "state/core-supervisor.json")})
        with mock.patch.object(helpers, "publish", side_effect=Published) as publish, \
                mock.patch.object(heartbeat, "WORKSPACE", self.workspace), \
                mock.patch.object(heartbeat, "_socket_path", return_value=str(self.socket)), \
                mock.patch.object(heartbeat, "_observed_session", return_value=self.session):
            with self.assertRaises(Published): heartbeat.main(["--helper-receipt-dir", str(self.receipts)])
            self.assertEqual(publish.call_args.args[:2], (str(self.receipts), "heartbeat"))
            self.assertEqual(publish.call_args.args[3:], (self.workspace, str(self.socket), self.session))
            self.assertEqual(publish.call_args.kwargs, {"passive": True})

    def test_other_runtime_refuses_external_mode_before_its_launcher(self):
        result = subprocess.run(["bash", str(self.repo / "src/agent/start-cli.sh"), "--runtime", "claude",
                                 "--external-helpers", str(self.receipts)], env=self.env,
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 2)
        self.assertIn("supported only for Codex", result.stderr)


if __name__ == "__main__":
    unittest.main()
