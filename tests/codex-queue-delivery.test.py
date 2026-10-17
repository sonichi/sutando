#!/usr/bin/env python3
"""src/delivery/codex_queue.py: the Codex core's delivery-mode policy and the `codex queue` call.

`codex` and the observer are fakes; no real Codex, node or tmux is run.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src" / "delivery"))
import codex_queue  # noqa: E402


class Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Recorder:
    def __init__(self, result):
        self.result, self.calls = result, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class ModeTests(unittest.TestCase):
    """The mode is read from merged config in a throwaway repo root."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "sutando.config.json").write_text(json.dumps(
            {"core": {"runtime": "codex"}, "workspace": {"path": str(self.root / "ws")}}))

    def _mode(self, local=None):
        if local is not None:
            (self.root / "sutando.config.local.json").write_text(json.dumps(local))
        return codex_queue.delivery(self.root)

    def test_default_is_send_keys(self):
        self.assertEqual(self._mode(), {"delivery": "send-keys", "thread": ""})

    def test_opt_in_selects_queue_and_thread(self):
        got = self._mode({"core": {"codex": {"delivery": "queue", "thread": "t-1"}}})
        self.assertEqual(got, {"delivery": "queue", "thread": "t-1"})

    def test_unknown_mode_falls_back_to_send_keys(self):
        self.assertEqual(self._mode({"core": {"codex": {"delivery": "paste"}}})["delivery"], "send-keys")


class SendTests(unittest.TestCase):
    MESSAGE = 'Sutando task ready: task-1.txt. Read "$(touch /x)"; `id` & echo $HOME'

    def test_argv_carries_the_message_as_one_entry_without_a_shell(self):
        run = Recorder(Done(0, "Queued message m for thread t."))
        self.assertEqual(codex_queue.send("t", self.MESSAGE, run=run)[0], 0)
        argv, kwargs = run.calls[0]
        self.assertEqual(argv, ["codex", "queue", "--thread", "t", "--message", self.MESSAGE])
        self.assertNotIn("shell", kwargs)

    def test_nonzero_exit_is_a_failure_with_its_reason(self):
        code, reason = codex_queue.send("t", "m", run=Recorder(Done(1, "", "no rollout found for thread id t")))
        self.assertEqual(code, codex_queue.QUEUE_FAILED)
        self.assertIn("no rollout found", reason)

    def test_a_codex_that_never_started_is_a_failure(self):
        code, reason = codex_queue.send("t", "m", run=Recorder(FileNotFoundError("codex")))
        self.assertEqual(code, codex_queue.QUEUE_FAILED)
        self.assertIn("did not start", reason)

    def test_a_timeout_is_unknown_never_a_failure(self):
        code, reason = codex_queue.send("t", "m", run=Recorder(subprocess.TimeoutExpired("codex", 30)))
        self.assertEqual(code, codex_queue.QUEUE_UNKNOWN)
        self.assertIn("may have queued", reason)

    def test_an_acknowledgement_then_nonzero_exit_is_unknown(self):
        for out, err in (("Queued message m1 for thread t.", "warning"), ("", "Queued message m1 for thread t.")):
            run = Recorder(Done(1, out, err))
            self.assertEqual(codex_queue.send("t", "m", run=run)[0], codex_queue.QUEUE_UNKNOWN)

    def test_a_codex_killed_by_a_signal_is_unknown(self):
        self.assertEqual(codex_queue.send("t", "m", run=Recorder(Done(-9)))[0], codex_queue.QUEUE_UNKNOWN)

    def test_a_slow_fake_codex_past_the_timeout_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "codex"
            fake.write_text("#!/bin/bash\nsleep 3\n")
            fake.chmod(0o755)
            orig_timeout, orig_path = codex_queue.TIMEOUT_S, os.environ["PATH"]
            codex_queue.TIMEOUT_S, os.environ["PATH"] = 1, f"{d}:{orig_path}"
            try:
                self.assertEqual(codex_queue.send("t", "m")[0], codex_queue.QUEUE_UNKNOWN)
            finally:
                codex_queue.TIMEOUT_S, os.environ["PATH"] = orig_timeout, orig_path

    def test_fake_codex_receives_the_exact_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "argv"
            fake = Path(d) / "codex"
            fake.write_text(f'#!/bin/bash\nfor a in "$@"; do printf "%s\\0" "$a"; done > "{log}"\n')
            fake.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if not k.startswith("SUTANDO_")}
            env["PATH"] = f"{d}:/usr/bin:/bin"
            r = subprocess.run([sys.executable, str(REPO / "src/delivery/codex_queue.py"), "send",
                                "--thread", "t", "--message", self.MESSAGE], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(log.read_text().split("\0")[:-1], ["queue", "--thread", "t", "--message", self.MESSAGE])
            self.assertFalse(Path("/x").exists())


class ThreadTests(unittest.TestCase):
    def _resolve(self, run, configured=""):
        orig_delivery, orig_which = codex_queue.delivery, codex_queue.shutil.which
        codex_queue.delivery = lambda repo_root=None: {"delivery": "queue", "thread": configured}
        codex_queue.shutil.which = lambda name: "/fake/node"
        try:
            return codex_queue.resolve_thread("/engine", "/sock", "sutando-core", run=run)
        finally:
            codex_queue.delivery, codex_queue.shutil.which = orig_delivery, orig_which

    def test_discovery_wins_even_when_a_thread_is_configured(self):
        # A stale pin must never shadow a live discovery result, or a task sent right
        # after /new or /resume would silently go to the old thread.
        run = Recorder(Done(0, "live-thread\n"))
        self.assertEqual(self._resolve(run, configured="stale-pin"), (0, "live-thread"))
        self.assertEqual(len(run.calls), 1)

    def test_configured_thread_is_the_fallback_when_discovery_fails(self):
        run = Recorder(Done(3, "", "no core pane"))
        self.assertEqual(self._resolve(run, configured="named"), (0, "named"))
        self.assertEqual(len(run.calls), 1)

    def test_a_found_thread_with_no_turn_never_uses_the_pin(self):
        run = Recorder(Done(4, "", "thread has no persisted turn"))
        self.assertEqual(self._resolve(run, configured="stale-pin"), (codex_queue.NO_TURN, "thread has no persisted turn"))

    def test_discovery_asks_the_observer(self):
        run = Recorder(Done(0, "0199-abc\n"))
        self.assertEqual(self._resolve(run), (0, "0199-abc"))
        argv = run.calls[0][0]
        self.assertEqual(argv[0], "/fake/node")
        self.assertTrue(argv[1].endswith("src/agent/codex/cli/codex-observer.mjs"))
        self.assertEqual(argv[2:], ["--core-thread", "--engine", "/engine", "--tmux-socket", "/sock",
                                    "--session", "sutando-core"])

    def test_no_persisted_turn_and_no_thread_keep_their_codes(self):
        self.assertEqual(self._resolve(Recorder(Done(4, "", "thread has no persisted turn")))[0], codex_queue.NO_TURN)
        self.assertEqual(self._resolve(Recorder(Done(3, "", "no core pane")))[0], codex_queue.NO_THREAD)
        self.assertEqual(self._resolve(Recorder(Done(1, "", "boom")))[0], 1)

    def test_lookup_without_node_or_with_a_failing_observer_is_code_1(self):
        orig_delivery, orig_which = codex_queue.delivery, codex_queue.shutil.which
        codex_queue.delivery = lambda repo_root=None: {"delivery": "queue", "thread": ""}
        codex_queue.shutil.which = lambda name: None
        try:
            code, reason = codex_queue.resolve_thread("/e", "/s", "c", run=Recorder(Done(0, "x")))
        finally:
            codex_queue.delivery, codex_queue.shutil.which = orig_delivery, orig_which
        self.assertEqual((code, "node not on PATH" in reason), (1, True))
        for exc in (OSError("gone"), subprocess.TimeoutExpired("node", 30)):
            code, reason = self._resolve(Recorder(exc))
            self.assertEqual((code, "thread lookup failed" in reason), (1, True))


class CliTests(unittest.TestCase):
    """main() in-process, with the policy functions stubbed."""

    def _main(self, argv, **stubs):
        saved = {k: getattr(codex_queue, k) for k in stubs}
        for k, v in stubs.items():
            setattr(codex_queue, k, v)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = codex_queue.main(argv)
        finally:
            for k, v in saved.items():
                setattr(codex_queue, k, v)
        return code, out.getvalue(), err.getvalue()

    def test_mode_prints_the_resolved_mode(self):
        got = self._main(["mode"], delivery=lambda repo_root=None: {"delivery": "queue", "thread": ""})
        self.assertEqual(got, (0, "queue", ""))

    def test_thread_prints_the_id_or_reports_the_code(self):
        calls = []
        ok = self._main(["thread", "--socket", "/s", "--session", "c"],
                        resolve_thread=lambda e, so, se: calls.append((e, so, se)) or (0, "t1"))
        self.assertEqual(ok, (0, "t1", ""))
        self.assertEqual(calls, [(str(codex_queue.REPO), "/s", "c")])
        no_turn = self._main(["thread", "--engine", "/e", "--socket", "/s", "--session", "c"],
                             resolve_thread=lambda e, so, se: (4, "thread has no persisted turn"))
        self.assertEqual(no_turn, (4, "", "thread has no persisted turn"))

    def test_send_returns_the_send_code(self):
        got = self._main(["send", "--thread", "t", "--message", "m"], send=lambda t, m: (6, "maybe queued"))
        self.assertEqual(got, (6, "", "maybe queued"))

    def test_empty_thread_or_message_is_refused_before_running_codex(self):
        run = Recorder(Done(0))
        self.assertEqual(codex_queue.send("", "m", run=run)[0], codex_queue.QUEUE_FAILED)
        self.assertEqual(codex_queue.send("t", "", run=run)[0], codex_queue.QUEUE_FAILED)
        self.assertEqual(run.calls, [])


class LockFdTests(unittest.TestCase):
    def _send_kwargs(self, env_value):
        old = os.environ.get("SUTANDO_NOTIFIER_LOCK_FD")
        try:
            if env_value is None:
                os.environ.pop("SUTANDO_NOTIFIER_LOCK_FD", None)
            else:
                os.environ["SUTANDO_NOTIFIER_LOCK_FD"] = env_value
            run = Recorder(Done(0, "ok"))
            codex_queue.send("t", "m", run=run)
            return run.calls[0][1]
        finally:
            if old is None:
                os.environ.pop("SUTANDO_NOTIFIER_LOCK_FD", None)
            else:
                os.environ["SUTANDO_NOTIFIER_LOCK_FD"] = old

    def test_codex_queue_is_handed_the_inherited_lock_fd(self):
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        self.assertEqual(self._send_kwargs(str(r))["pass_fds"], (r,))

    def test_no_lock_fd_is_passed_when_none_is_inherited(self):
        r, w = os.pipe()
        os.close(r)
        os.close(w)
        for value in (None, "", "junk", str(r)):
            self.assertEqual(self._send_kwargs(value)["pass_fds"], (), value)

    def test_a_codex_queue_outliving_the_notifier_holds_off_a_successor(self):
        # The notifier and codex_queue.py are killed; the `codex queue` child they left must keep the lock.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", "--", str(tmp)]))
        started, release, lock = tmp / "started", tmp / "release", str(tmp / "notifier.lock")
        (tmp / "bin").mkdir()
        fake = tmp / "bin" / "codex"
        fake.write_text(f'#!/bin/bash\necho $$ > "{started}"\n'
                        f'while [ ! -e "{release}" ]; do sleep 0.05; done\necho "Queued message"\n')
        fake.chmod(0o755)
        env = {"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "HOME": str(tmp)}
        helper = REPO / "src" / "delivery" / "notifier_lock.py"
        queue = REPO / "src" / "delivery" / "codex_queue.py"
        first = subprocess.Popen(
            [sys.executable, str(helper), "--lock", lock, "--", "bash", "-c",
             f'"{sys.executable}" "{queue}" send --thread t --message m'],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 15
        while not started.exists():
            self.assertLess(time.monotonic(), deadline, "codex queue never started")
            time.sleep(0.02)
        codex_pid = int(started.read_text())
        py_pid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(codex_pid)], capture_output=True,
                                    text=True).stdout.strip())
        self.addCleanup(lambda: release.touch())
        for pid in (first.pid, py_pid):
            os.kill(pid, 9)
        first.wait(timeout=30)
        successor = subprocess.run([sys.executable, str(helper), "--lock", lock, "--timeout", "1", "--", "true"],
                                   env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(successor.returncode, 75, "a successor ran while the old codex queue held the lock")
        release.touch()
        successor = subprocess.run([sys.executable, str(helper), "--lock", lock, "--timeout", "15", "--", "true"],
                                   env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(successor.returncode, 0, successor.stderr)


if __name__ == "__main__":
    unittest.main()
