#!/usr/bin/env python3
"""sparrowd follows the agent-bridge declarations as they come and go, in both of its modes.

The package shell reconciles its supervised set by name (a new name starts, a dropped name stops,
an unchanged name is left running); the launcher feeds it the declarations on every scan, prints
each skip reason once, and never lists a declaration another process already runs. `--agent-bridges`
is the mode the launchd wrapper keeps alive beside the gateway bridge: the declarations only.
Run: python3 tests/sparrowd-agent-bridge-rescan.test.py"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
for _p in (str(REPO / "src"), str(REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ag2_sparrow.sparrowd import Supervisor, WorkerSpec, run  # noqa: E402
from ag2_sparrow.adapters.claude_code import cli  # noqa: E402

SLEEPER = [sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"]
STUBBORN = [sys.executable, "-c", "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                                  "while True: time.sleep(0.2)"]


def wait_for(pred, timeout=8.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return False


def alive(pid) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pid_of(sup, name):
    return sup.status()["workers"][name]["pid"]


class Reconcile(unittest.TestCase):
    def test_a_new_name_starts_a_dropped_name_stops_and_the_rest_keep_their_pid(self):
        sup = Supervisor([WorkerSpec("a", SLEEPER)])
        sup.start()
        self.addCleanup(sup.stop, 2)
        self.assertTrue(wait_for(lambda: pid_of(sup, "a")))
        a_pid = pid_of(sup, "a")
        self.assertEqual(sup.reconcile([WorkerSpec("a", SLEEPER), WorkerSpec("b", SLEEPER)]), (["b"], []))
        self.assertTrue(wait_for(lambda: pid_of(sup, "b")), "the added name is started")
        b_pid = pid_of(sup, "b")
        self.assertEqual(sup.reconcile([WorkerSpec("a", SLEEPER), WorkerSpec("b", SLEEPER)]), ([], []))
        self.assertEqual((pid_of(sup, "a"), pid_of(sup, "b")), (a_pid, b_pid), "an unchanged set restarts nothing")
        self.assertEqual(sup.reconcile([WorkerSpec("a", SLEEPER)]), ([], ["b"]))
        self.assertTrue(wait_for(lambda: not alive(b_pid)), "the dropped worker's process is gone")
        self.assertNotIn("b", sup.status()["workers"])
        self.assertEqual(pid_of(sup, "a"), a_pid, "the kept worker was not touched")
        self.assertEqual(sup.reconcile([]), ([], ["a"]))
        self.assertTrue(wait_for(lambda: not alive(a_pid)))
        self.assertEqual(sup.status()["workers"], {})

    def test_a_removed_worker_that_ignores_sigterm_is_killed_past_the_grace(self):
        sup = Supervisor([WorkerSpec("s", STUBBORN)], remove_grace_s=0.3)
        sup.start()
        self.addCleanup(sup.stop, 2)
        self.assertTrue(wait_for(lambda: pid_of(sup, "s")))
        pid = pid_of(sup, "s")
        time.sleep(0.3)  # the handler is installed before the loop; give it that
        sup.reconcile([])
        self.assertTrue(wait_for(lambda: not alive(pid), 5), "SIGKILL after the grace, no orphan")

    def test_a_removed_worker_in_backoff_never_respawns(self):
        crasher = [sys.executable, "-c", "raise SystemExit(3)"]
        sleeps = []
        sup = Supervisor([WorkerSpec("c", crasher)], backoff_initial_s=0.5,
                         sleep=lambda s: (sleeps.append(s), time.sleep(0.05)))
        sup.start()
        self.addCleanup(sup.stop, 2)
        self.assertTrue(wait_for(lambda: len(sleeps) >= 1))
        sup.reconcile([])
        restarts = sup.status()["workers"]
        self.assertEqual(restarts, {}, "gone from the table at once")
        n = len(sleeps)
        time.sleep(0.6)
        self.assertLessEqual(len(sleeps), n + 1, "at most the pause in flight; no further restart")

    def test_run_reconciles_on_every_rescan(self):
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        passes = [[], [WorkerSpec("late", SLEEPER)], [WorkerSpec("late", SLEEPER)], []]
        seen = []
        sock = td / "sparrowd.sock"

        def rescan():
            want = passes.pop(0) if passes else []
            seen.append([s.name for s in want])
            return want

        def stop_when_done():
            wait_for(lambda: sock.exists() and len(seen) >= 4, 10)
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock))
            c.sendall(b'{"op": "stop"}\n')
            c.makefile("r").readline()
            c.close()
        th = threading.Thread(target=stop_when_done, daemon=True)
        th.start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = run([], td, rescan=rescan, rescan_s=0.1)
        th.join(5)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(len(seen), 4)
        log = out.getvalue()
        self.assertEqual(log.count("worker late: added"), 1, "one worker for the declaration while it existed")
        self.assertEqual(log.count("worker late: removed"), 1, "stopped once it was gone")

    def test_a_failing_rescan_keeps_the_running_workers(self):
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        sock = td / "sparrowd.sock"
        calls = []

        def rescan():
            calls.append(1)
            raise RuntimeError("vault down")

        def stop_when_done():
            wait_for(lambda: sock.exists() and len(calls) >= 2, 10)
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(sock))
            c.sendall(b'{"op": "stop"}\n')
            c.makefile("r").readline()
            c.close()
        threading.Thread(target=stop_when_done, daemon=True).start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = run([WorkerSpec("w", SLEEPER)], td, rescan=rescan, rescan_s=0.1)
        self.assertEqual(rc, 0)
        self.assertIn("rescan failed (RuntimeError: vault down)", out.getvalue())
        self.assertNotIn("worker w: removed", out.getvalue())


AGENT = "@probe.agent:ag2.space"
SEG = "probe.agent_ag2.space"


def load_sparrowd():
    spec = importlib.util.spec_from_file_location("sparrowd_rescan_under_test", REPO / "src" / "sparrowd.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Launcher(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.sd = load_sparrowd()
        self.keys = []
        fake_vault = types.SimpleNamespace(list_vault_keys=lambda: list(self.keys))
        for patch in (mock.patch.object(self.sd, "resolve_workspace", return_value=self.ws),
                      mock.patch.dict(sys.modules, {"vault_intercept": fake_vault})):
            patch.start()
            self.addCleanup(patch.stop)

    def declare(self, agent=AGENT, seg=SEG):
        root = self.ws / "agents" / seg
        cfg = {"ROOT": str(root), "BRIDGE": "sparrowd", "AGENT_ID": agent, "TOKEN_VAULT_KEY": cli.vault_key(seg)}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.wire_bridge(cfg), "")
        self.keys.append(cli.vault_key(seg))
        return cli.bridge_home(root) / cli.DECLARATION

    def main(self, argv, external=None):
        """main with `run` captured; the external-supervisor stand-in stays in place for the rescans."""
        err = io.StringIO()
        ext = mock.patch.object(self.sd, "external_supervisor", side_effect=external or (lambda m: None))
        ext.start()
        self.addCleanup(ext.stop)
        with mock.patch.object(self.sd, "run", return_value=0) as run_, contextlib.redirect_stderr(err):
            rc = self.sd.main(argv)
        self.assertEqual(rc, 0)
        args, kw = run_.call_args
        return args[0], args[1], kw["rescan"], kw["rescan_s"], err

    def test_agent_bridges_mode_supervises_only_the_declarations_and_follows_them(self):
        specs, state, rescan, rescan_s, err = self.main(["--agent-bridges", "--rescan-s", "0.5"])
        self.assertEqual((specs, state, rescan_s), ([], self.ws / "state" / "sparrowd", 0.5),
                         "nothing to run yet; the gateway bridge and the skill loops are not this mode's")
        decl = self.declare()
        with contextlib.redirect_stderr(err):
            want = rescan()
        self.assertEqual([s.name for s in want], [cli.worker_name(SEG)], "written after start: picked up on a pass")
        self.assertEqual(want[0].argv[1:], [str(self.sd.AGENT_BRIDGE), str(decl)], "the one wrapper runs it")
        decl.unlink()
        with contextlib.redirect_stderr(err):
            self.assertEqual(rescan(), [], "removed by uninstall: gone on the next pass")
        self.assertEqual(err.getvalue(), "")

    def test_full_mode_keeps_the_gateway_bridge_first_and_rescans_only_the_declarations(self):
        self.declare()
        specs, _, rescan, rescan_s, _ = self.main([])
        self.assertEqual(rescan_s, 30.0)
        names = [s.name for s in specs]
        self.assertEqual((names[0], names.count(cli.worker_name(SEG))), ("remote-gateway-bridge", 1))
        with contextlib.redirect_stderr(io.StringIO()):
            again = [s.name for s in rescan()]
        self.assertEqual(again, names, "the static workers are listed on every pass, so never dropped")

    def test_a_skip_reason_is_printed_once_until_it_changes(self):
        decl = self.declare()
        self.keys.clear()
        _, _, rescan, _, err = self.main(["--agent-bridges"])
        why = f"the vault has no {cli.vault_key(SEG)}"
        self.assertEqual(err.getvalue().count(why), 1)
        with contextlib.redirect_stderr(err):
            rescan()
            rescan()
        self.assertEqual(err.getvalue().count(why), 1, "the same reason is not repeated every pass")
        decl.write_text("{torn")
        with contextlib.redirect_stderr(err):
            rescan()
        self.assertEqual(err.getvalue().count("unreadable or malformed"), 1, "a new reason is printed")
        decl.unlink()
        with contextlib.redirect_stderr(err):
            self.assertEqual(rescan(), [])

    def test_a_declaration_another_process_runs_is_skipped_and_an_own_worker_is_not_rechecked(self):
        decl = self.declare()
        checked = []

        def external(marker):
            checked.append(marker)
            return "pid 4242 (1 python3 sparrow_agent_bridge.py …)" if len(checked) == 1 else None
        specs, _, rescan, _, err = self.main(["--agent-bridges"], external=external)
        self.assertEqual(specs, [], "live outside this daemon: never a second bridge for the agent")
        self.assertIn(f"{decl}: already supervised outside sparrowd: pid 4242", err.getvalue())
        self.assertEqual(checked, [str(decl)])
        with contextlib.redirect_stderr(err):
            want = rescan()
        self.assertEqual([s.name for s in want], [cli.worker_name(SEG)], "picked up once that process is gone")
        self.assertEqual(len(checked), 2)
        with contextlib.redirect_stderr(err):
            rescan()
        self.assertEqual(len(checked), 2, "its live process is now this daemon's own worker: not re-checked")

    def test_full_mode_still_refuses_a_gateway_bridge_supervised_elsewhere(self):
        err = io.StringIO()
        with mock.patch.object(self.sd, "external_supervisor", side_effect=lambda m: "pid 7 (1 bash wrapper)"), \
                mock.patch.object(self.sd, "run", return_value=0) as run_, contextlib.redirect_stderr(err):
            self.assertEqual(self.sd.main([]), 2)
        run_.assert_not_called()
        self.assertIn("refusing to start — remote-gateway-bridge already supervised outside sparrowd", err.getvalue())


if __name__ == "__main__":
    unittest.main()
