#!/usr/bin/env python3
"""health-check's cli-wedge probe: absent socket / unreadable pane are readings
of nothing (ok), a static pane with work outstanding warns, a retry loop warns,
and idle never does. The tmux and heartbeat seams are replaced with fakes."""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(hc)
except SystemExit:
    pass

IDLE = "❯ \n⏵⏵ bypass permissions on · 1 monitor\n"


class CliWedgeProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        ws = Path(self.tmp.name)
        (ws / "state").mkdir()
        (ws / "tasks").mkdir()
        self.ws = ws
        self._saved = (hc.WORKSPACE_DIR, hc._resolve_tmux_bin, hc._resolve_launch_env)
        # The check reads time.time(); the fake clock advances 60 s per beat so runs last.
        self._saved_time = hc.time
        import time as _time
        self._t = [_time.time()]
        def _now():
            self._t[0] += 60.0
            return self._t[0]
        hc.time = SimpleNamespace(time=_now)
        hc.WORKSPACE_DIR = ws
        # A REAL fresh heartbeat under an accepted local label; socket and session both come from it.
        (ws / "state" / "cores").mkdir()
        self._saved_labels = hc._local_host_labels
        hc._local_host_labels = lambda *a, **k: {"canon", "alias"}
        self.write_alive("canon", "/tmp/fake.sock", "sutando-core")
        self.frames = [IDLE]
        # A stand-in tmux BINARY drives the real capture/identity: next frame per capture,
        # a real-looking identity, and — like tmux with two windows — no bare `=sutando-core`.
        self.frames_file = ws / "frames.txt"
        self.idx = ws / "frames.idx"
        self.tmux = ws / "tmux"
        self.tmux.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, pathlib\n"
            "a = sys.argv\n"
            "t = a[a.index('-t') + 1] if '-t' in a else ''\n"
            "import os\n"
            "sess = os.environ.get('FAKE_SESSION', 'sutando-core')\n"
            "if 'list-windows' in a:\n"
            "    ok = t == '=' + sess\n"
            "    print('1\\n2') if ok else sys.stderr.write('no such session\\n'); sys.exit(0 if ok else 1)\n"
            "if t != '=' + sess + ':1':\n"  # base-index 1: the core window is 1, and a bare =name is no pane
            "    sys.stderr.write(\"can't find pane: \" + t + \"\\n\"); sys.exit(1)\n"
            "if 'display-message' in a:\n"
            "    fmt = a[-1]\n"
            "    print(os.environ.get('FAKE_PANE_ID', '%987654') + ':' + os.environ.get('FAKE_PANE_PID', '4242')) if 'pane_id' in fmt else print('4242:1788000000'); sys.exit(0)\n"
            f"ff = pathlib.Path({str(self.frames_file)!r}); idx = pathlib.Path({str(self.idx)!r})\n"
            "frames = ff.read_text().split('\\n===\\n')\n"
            "i = int(idx.read_text()) if idx.exists() else 0\n"
            "idx.write_text(str(i + 1))\n"
            "sys.stdout.write(frames[min(i, len(frames) - 1)])\n"
        )
        self.tmux.chmod(0o755)
        hc._resolve_tmux_bin = lambda *a, **k: str(self.tmux)
        hc._resolve_launch_env = lambda: dict(os.environ)

    def write_alive(self, label, socket, session):
        import time as _time
        p = self.ws / "state" / "cores" / f"{label}.alive"
        p.write_text(json.dumps({"host": label, "socket": socket, "session": session, "status": "running", "schema_version": 3}))
        os.utime(p, (self._t[0] + 30, self._t[0] + 30))  # fresh on the fake clock
        return p

    def _serve(self):
        self.frames_file.write_text("\n===\n".join(self.frames))
        if self.idx.exists():
            self.idx.unlink()

    def check(self):
        if not self.frames_file.exists():
            self._serve()
        # A live core beats between passes: keep every heartbeat file fresh on the fake clock.
        for p in (self.ws / "state" / "cores").glob("*.alive"):
            os.utime(p, (self._t[0] + 30, self._t[0] + 30))
        return hc.check_cli_wedge()

    def tearDown(self):
        hc.WORKSPACE_DIR, hc._resolve_tmux_bin, hc._resolve_launch_env = self._saved
        hc._local_host_labels = self._saved_labels
        hc.time = self._saved_time
        self.tmp.cleanup()

    def test_missing_detector_module_is_a_detail_not_a_failure(self):
        saved = sys.modules.get("cli_wedge")
        sys.modules["cli_wedge"] = None  # makes `import cli_wedge` raise
        try:
            c = self.check()
        finally:
            if saved is None:
                sys.modules.pop("cli_wedge", None)
            else:
                sys.modules["cli_wedge"] = saved
        self.assertEqual(c["status"], "ok")
        self.assertIn("detector unavailable", c["detail"])

    def test_unwritable_window_is_no_reading_not_a_crash(self):
        (self.ws / "state" / "cli-wedge" / "window.jsonl").mkdir(parents=True)  # path occupied: the write must fail
        c = self.check()
        self.assertEqual(c["status"], "ok")
        self.assertIn("no reading", c["detail"])

    def test_malformed_window_lines_do_not_crash_the_check(self):
        (self.ws / "state" / "cli-wedge").mkdir()
        (self.ws / "state" / "cli-wedge" / "window.jsonl").write_text("[]\n" + json.dumps({"ts": "x", "state": "s"}) + "\n{bad\n")
        c = self.check()
        self.assertIn(c["status"], ("ok", "warn"))
        self.assertIn(c["name"], ("cli-wedge",))

    def test_no_local_core_is_ok_and_says_so(self):
        (self.ws / "state" / "cores" / "canon.alive").unlink()
        c = self.check()
        self.assertEqual((c["name"], c["status"]), ("cli-wedge", "ok"))
        self.assertIn("no local core pane", c["detail"])

    def test_unreadable_pane_is_ok_not_a_verdict(self):
        hc._resolve_tmux_bin = lambda *a, **k: "/nonexistent/tmux"
        c = self.check()
        self.assertEqual(c["status"], "ok")
        self.assertIn("not a verdict", c["detail"])

    def test_idle_pane_never_warns(self):
        for _ in range(3):
            c = self.check()
        self.assertEqual(c["status"], "ok")
        self.assertIn("idle", c["detail"])
        self.assertEqual(c["evidence"]["sample_count"], 3)

    def test_static_pane_with_work_outstanding_warns(self):
        (self.ws / "state" / "core-status.json").write_text(json.dumps({"status": "running", "ts": self._t[0] + 60.0}))
        for _ in range(3):
            c = self.check()
        self.assertEqual(c["status"], "warn")
        self.assertIn("static-with-work", c["detail"])
        self.assertTrue(c["evidence"]["work_outstanding"])
        self.assertIn("reads the pane, not the process", c["detail"])

    def test_retry_loop_warns_even_when_the_pane_moves(self):
        self.frames = [f"Connection error. Retrying in {3 * (i % 3)}s (attempt {i}/10) 04:2{i % 10}:11\n" for i in range(12)]
        (self.ws / "tasks" / "task-1.txt").write_text("task: x\n")
        for _ in range(12):
            c = self.check()
        self.assertEqual(c["status"], "warn")
        self.assertIn("retry-loop", c["detail"])
        self.assertIn("retrying", c["evidence"]["matched_patterns"])

    def test_stale_running_status_is_not_work(self):
        # graceful-restart's contract: "running" stamped long ago is a crashed core, not work.
        (self.ws / "state" / "core-status.json").write_text(json.dumps({"status": "running", "ts": 1.0}))
        for _ in range(3):
            c = self.check()
        self.assertEqual(c["status"], "ok")
        self.assertIn("idle", c["detail"])
        self.assertFalse(c["evidence"]["work_outstanding"])

    def test_target_is_the_heartbeat_session_and_its_lowest_window_not_window_0(self):
        # The fake server has base-index 1 (windows 1 and 2) and rejects both a bare `=name`
        # and `:0`; the check must resolve `=sutando-core:1` from list-windows and read.
        for _ in range(3):
            c = self.check()
        self.assertNotIn("not readable", c["detail"])
        self.assertEqual(c["evidence"]["sample_count"], 3)

    def test_socket_and_session_come_from_the_same_record_even_under_an_alternate_label(self):
        # The socket came from the multi-label resolver and the session from the canonical-label
        # reader, so an ALIAS heartbeat gave the socket but not its session; one record feeds both.
        (self.ws / "state" / "cores" / "canon.alive").unlink()
        self.write_alive("alias", "/tmp/fake.sock", "custom-core")
        with patch.dict(os.environ, {"FAKE_SESSION": "custom-core"}):
            for _ in range(2):
                c = self.check()
        self.assertNotIn("not readable", c["detail"])
        self.assertEqual(c["evidence"]["sample_count"], 2)
        # two fresh records: the NEWER one supplies both fields, never a mix
        newer = self.write_alive("canon", "/tmp/newer.sock", "newer-core")
        os.utime(newer, (self._t[0] + 35, self._t[0] + 35))
        rec = hc._local_core_record()
        self.assertEqual((rec["socket"], rec["session"]), ("/tmp/newer.sock", "newer-core"))

    def test_sampling_from_inside_the_watched_pane_is_skipped(self):
        # The stand-in names THIS process as the pane shell: a core sampling its own pane must not.
        self.frames = [IDLE] * 4
        with patch.dict(os.environ, {"FAKE_PANE_PID": str(os.getpid()), "TMUX_PANE": "%1"}):
            c = self.check()
        self.assertEqual(c["status"], "ok")
        self.assertTrue(c["detail"].startswith("skipped — sampled from inside the pane it watches"), c["detail"])
        self.assertFalse((self.ws / "state" / "cli-wedge" / "window.jsonl").exists(), "a skipped sample must not be recorded")

    def test_tmux_pane_naming_the_target_is_skipped(self):
        with patch.dict(os.environ, {"TMUX_PANE": "%987654"}):
            c = self.check()
        self.assertTrue(c["detail"].startswith("skipped — sampled from inside the pane it watches"), c["detail"])

    def test_an_outside_caller_still_samples(self):
        with patch.dict(os.environ, {"TMUX_PANE": "%1"}):
            c = self.check()
        self.assertFalse(c["detail"].startswith("skipped"), c["detail"])
        self.assertTrue((self.ws / "state" / "cli-wedge" / "window.jsonl").exists())

    def test_working_pane_is_ok(self):
        self.frames = [f"● wrote {chr(97 + i)}.ts\n" for i in range(12)]
        for _ in range(12):
            c = self.check()
        self.assertEqual(c["status"], "ok")
        self.assertIn("working", c["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
