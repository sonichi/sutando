#!/usr/bin/env python3
"""health-check reports a standing pane-key fence (an uncertain send) on the core's socket."""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class PaneKeyFence(unittest.TestCase):
    def test_no_lock_and_an_active_send_are_ok(self):
        with tempfile.TemporaryDirectory() as d:
            sock = str(Path(d) / "s")
            self.assertEqual(hc.check_pane_key_fence(sock)["status"], "ok")
            with hc.tmux_pane_keys.prepare_guard(sock).open("ab") as mutex:
                self.assertEqual(hc.tmux_pane_keys.acquire_guard(sock, mutex.fileno()), 0)
                self.assertEqual(hc.check_pane_key_fence(sock)["status"], "ok")

    def test_an_uncertain_send_fails_and_names_the_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            sock = str(Path(d) / "s")
            lock = Path(sock + ".pane-keys-lock")
            lock.mkdir()
            (lock / "uncertain").touch()
            out = hc.check_pane_key_fence(sock)
            self.assertEqual((out["name"], out["status"]), ("pane-key-fence", "fail"))
            self.assertIn(sock, out["detail"])
            self.assertIn("docs/codex-core.md", out["detail"])

    def test_a_fenced_worker_socket_beside_the_core_socket_fails_too(self):
        with tempfile.TemporaryDirectory() as d:
            core = str(Path(d) / "core.sock")
            worker_lock = Path(d) / "worker-7.sock.pane-keys-lock"
            worker_lock.mkdir()
            (worker_lock / "uncertain").touch()
            out = hc.check_pane_key_fence(core)
            self.assertEqual(out["status"], "fail")
            self.assertIn(str(Path(d) / "worker-7.sock"), out["detail"])

    def test_the_default_socket_is_this_hosts_not_a_fresher_peers(self):
        with tempfile.TemporaryDirectory() as d:
            ws, run = Path(d) / "ws", Path(d) / "run"
            cores = ws / "state" / "cores"
            cores.mkdir(parents=True)
            (run / "local").mkdir(parents=True)
            (run / "peer").mkdir()
            local, peer = str(run / "local" / "local.sock"), str(run / "peer" / "peer.sock")
            for label, sock, age in ((sorted(hc._local_host_labels())[0], local, 5), ("peer-host", peer, 1)):
                alive = cores / f"{label}.alive"
                alive.write_text(json.dumps({"host": label, "socket": sock, "started_at": 1.0}))
                os.utime(alive, (time.time() - age,) * 2)
            Path(local + ".pane-keys-lock").mkdir()
            Path(local + ".pane-keys-lock", "uncertain").touch()
            with mock.patch.object(hc, "WORKSPACE_DIR", ws):
                self.assertEqual(hc._live_core_socket(ws), peer)
                out = hc.check_pane_key_fence()
            self.assertEqual(out["status"], "fail", out)
            self.assertIn(local, out["detail"])

    def test_the_check_is_registered(self):
        self.assertIn("checks.append(check_pane_key_fence())", (REPO / "src" / "health-check.py").read_text())


if __name__ == "__main__":
    unittest.main()
