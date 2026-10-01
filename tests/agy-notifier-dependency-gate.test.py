#!/usr/bin/env python3
"""Blocker #4 (sonichi#4303 review): start-cli.sh's ensure_task_notifier
checked only that `agy` and `tmux` exist, never that `fswatch` — a hard
dependency of task-notifier.sh's main loop (via watch-tasks-stream.sh) — is
on PATH, and never verified the watcher tmux session it just started was
still alive a moment later.

Without fswatch, watch-tasks-stream.sh's `fswatch | while read` pipe never
gets a producer; the notifier's own event-read loop then hits EOF and exits;
since task-notifier.sh IS the watcher pane's foreground command, tmux tears
the whole session down when it exits — all within about a second, silently,
while start-cli.sh still exits 0.

Uses REAL tmux (skipped where unavailable) because the failure IS tmux's own
session-dies-when-its-command-exits semantics; a stubbed tmux (as in
agy-task-notifier.test.py's StartCliNotifierWiringTest, which only proves the
watcher session gets STARTED, not that it stays alive) cannot reproduce it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "src/agent/agy/cli/start-cli.sh"
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from clean_watcher_env import clean_env  # noqa: E402


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


@unittest.skipUnless(_tmux_available(), "tmux not installed on this host")
class NotifierDependencyGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.socket = self.root / "fake.sock"
        self.session = "sutando-agy-depgate-test"
        self._write_fake_agy()
        self.addCleanup(self._kill_server)

    def _write_fake_agy(self):
        path = self.bin / "agy"
        path.write_text(
            "#!/bin/bash\n"
            'if [ "${1:-}" = --version ]; then echo 9.9.9-fake; exit 0; fi\n'
            'if [ "${1:-}" = models ]; then echo fake-model; exit 0; fi\n'
            "exec sleep 300\n"
        )
        path.chmod(0o755)

    def _hermetic_path(self, exclude=()):
        """A PATH whose only real directory is a mirror of every executable on
        the host PATH minus `exclude`, so a tool cannot leak in from wherever a
        given host or CI image happens to install it (Homebrew's bin, /usr/bin)."""
        shadow = Path(tempfile.mkdtemp(prefix="shadow-", dir=self.root))
        seen = set(exclude)
        dirs = os.environ.get("PATH", "").split(os.pathsep) + ["/usr/bin", "/bin"]
        for d in dirs:
            try:
                entries = os.listdir(d)
            except OSError:
                continue
            for name in entries:
                real = Path(d) / name
                if name in seen or not os.access(real, os.X_OK) or real.is_dir():
                    continue
                seen.add(name)
                (shadow / name).symlink_to(real)
        return f"{self.bin}:{shadow}"

    def _kill_server(self):
        subprocess.run(["tmux", "-S", str(self.socket), "kill-server"],
                        capture_output=True, timeout=10)

    def _env(self, path, extra=None):
        env = clean_env()
        env.update({
            "PATH": path,
            "SUTANDO_AGY_TMUX_SOCKET": str(self.socket),
            "SUTANDO_AGY_TMUX_SESSION": self.session,
            "SUTANDO_AGY_ONBOARDING_PATH": str(self.root / "onboarding.json"),
            "SUTANDO_TASKS_DIR": str(self.root / "tasks"),
            "SUTANDO_RESULTS_DIR": str(self.root / "results"),
            "SUTANDO_WORKSPACE_DIR": str(self.root),
            "HOME": str(self.root),
        })
        if extra:
            env.update(extra)
        return env

    def _has_session(self, name):
        r = subprocess.run(
            ["tmux", "-S", str(self.socket), "has-session", "-t", f"={name}"],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0

    def _assert_watcher_session_gone(self, why):
        # The session dies when the watcher's own readiness timeout ends it; on a loaded host that
        # can trail the launcher's return by a few seconds, so wait for it rather than sample once.
        for _ in range(100):
            if not self._has_session(f"{self.session}-watcher"):
                return
            time.sleep(0.1)
        self.fail(why)

    def _run_launcher(self, path, extra=None):
        return subprocess.run(
            ["/bin/bash", str(LAUNCHER)], env=self._env(path, extra), cwd=str(self.root),
            capture_output=True, text=True, timeout=60,
        )

    def test_missing_fswatch_is_reported_and_no_watcher_is_left_running(self):
        # Mirror the host PATH minus fswatch: a CI image that installs it under
        # /usr/bin must not turn this negative arm into a positive one.
        path = self._hermetic_path(exclude={"fswatch"})
        result = self._run_launcher(path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fswatch", (result.stdout + result.stderr).lower())
        self._assert_watcher_session_gone("no watcher session should be left running (or unreported-dead) without fswatch")

    def test_present_fswatch_watcher_survives_the_liveness_check(self):
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        path = self._hermetic_path()
        result = self._run_launcher(path)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("fswatch not found", result.stdout + result.stderr)
        self.assertNotIn("did not report ready", result.stdout + result.stderr)
        self.assertTrue(
            self._has_session(f"{self.session}-watcher"),
            "watcher session should be running and reported alive with fswatch present",
        )

    def test_installed_but_silent_fswatch_is_reported_not_claimed_ready(self):
        # An fswatch that stays alive but never emits: the watcher fails its
        # readiness round trip and the session dies later than a 2s poll sees.
        fake = self.bin / "fswatch"
        fake.write_text("#!/bin/bash\nexec sleep 300\n")
        fake.chmod(0o755)
        path = self._hermetic_path(exclude={"fswatch"})
        result = self._run_launcher(path, {"SUTANDO_WATCHER_READY_TIMEOUT": "3"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("did not report ready", result.stdout + result.stderr)
        self._assert_watcher_session_gone("a watcher that failed readiness must not be left as a live-looking session")


    def _sentinel_path(self):
        r = subprocess.run(["/bin/bash", str(REPO / "src/agent/agy/cli/task-notifier.sh"), "--sentinel-path"],
                           env=self._env(self._hermetic_path()), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return Path(r.stdout.strip())

    def _bystander(self, argv_shape=False):
        """A live process; with argv_shape it looks like a watcher on this exact inbox."""
        if argv_shape:
            stub = self.root / "watch-tasks-stream.sh"
            stub.write_text("#!/bin/bash\nsleep 300\n")  # no exec: bash keeps the watcher's argv
            stub.chmod(0o755)
            proc = subprocess.Popen(["/bin/bash", str(stub), str(self.root / "tasks"), "--role", "session",
                                     "--inbox", str(self.root / "tasks")])
        else:
            proc = subprocess.Popen(["sleep", "300"])
        self.addCleanup(proc.kill)
        return proc.pid

    def _launch_not_ready(self, make_payload, make_receipt=None, receipt_age=0.0, sentinel_age=None):
        # This launch's watcher never becomes ready (silent fswatch): only the preseeded sentinel and
        # receipt can speak. Bystanders are made last, the sentinel dated into the launch, on purpose.
        fake = self.bin / "fswatch"
        fake.write_text("#!/bin/bash\nexec sleep 300\n")
        fake.chmod(0o755)
        sentinel = self._sentinel_path()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        path = self._hermetic_path(exclude={"fswatch"})  # built before the bystanders: it takes ~1s
        rpath = None
        if make_receipt is not None:
            body = make_receipt()
            nonce = next((l.split("=", 1)[1] for l in body.splitlines() if l.startswith("nonce=")), "x")
            rpath = Path(f"{sentinel}.launch.{nonce or 'x'}")
            rpath.write_text(body)
            if receipt_age:
                os.utime(rpath, (time.time() - receipt_age, time.time() - receipt_age))
        sentinel.write_text(make_payload())
        stamp = time.time() - sentinel_age if sentinel_age is not None else time.time() + 3
        os.utime(sentinel, (stamp, stamp))
        result = self._run_launcher(path, {"SUTANDO_WATCHER_READY_TIMEOUT": "3"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("did not report ready", result.stdout + result.stderr)
        self._assert_watcher_session_gone("a watcher that never became ready was left as a live-looking session")
        return sentinel, rpath

    def _receipt(self, nonce, inbox=None, watcher=None, notifier=None):
        inbox = str(self.root / "tasks") if inbox is None else inbox
        watcher = self._bystander(argv_shape=True) if watcher is None else watcher
        notifier = self._bystander() if notifier is None else notifier
        return f"nonce={nonce}\ninbox={inbox}\nwatcher={watcher}\nnotifier={notifier}\n"

    def _dead_pid(self):
        gone = subprocess.Popen(["true"]); gone.wait()
        return gone.pid

    def _live_nonce(self):
        """The nonce of the launch in progress, read from its watcher session's environment."""
        for _ in range(200):
            r = subprocess.run(["tmux", "-S", str(self.socket), "show-environment", "-t", f"={self.session}-watcher",
                                "SUTANDO_AGY_LAUNCH_NONCE"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and "=" in r.stdout:
                return r.stdout.strip().split("=", 1)[1]
            time.sleep(0.05)
        self.fail("the watcher session never appeared with a launch nonce")

    def _launch_planting_receipt(self, make_receipt, expect_ready, receipt_age=0.0):
        # The receipt is planted under THIS launch's nonce while the launcher polls, so the
        # launcher reaches its parse/match branch; the watcher itself never becomes ready.
        fake = self.bin / "fswatch"
        fake.write_text("#!/bin/bash\nexec sleep 300\n")
        fake.chmod(0o755)
        sentinel = self._sentinel_path()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        path = self._hermetic_path(exclude={"fswatch"})
        launcher = subprocess.Popen(["/bin/bash", str(LAUNCHER)], env=self._env(path, {"SUTANDO_WATCHER_READY_TIMEOUT": "3"}),
                                    cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        nonce = self._live_nonce()
        rpath = Path(f"{sentinel}.launch.{nonce}")
        rpath.write_text(make_receipt(nonce))
        if receipt_age:
            os.utime(rpath, (time.time() - receipt_age, time.time() - receipt_age))
        # The predicate the launcher polls, asked directly on the planted content: this is the
        # parse/match verdict itself, independent of when the owner's exit cleans the path up.
        probe = subprocess.run(["/bin/bash", str(REPO / "src/agent/agy/cli/task-notifier.sh"), "--launch-ready", nonce],
                               env=self._env(path), capture_output=True, text=True, timeout=30)
        # A refusal is exactly rc 1 and no crash; interpreter warnings an ambient PYTHON* knob
        # turns on are not a crash, so the signal is a traceback, not silence.
        self.assertEqual(probe.returncode, 0 if expect_ready else 1,
                         f"--launch-ready decided {probe.returncode} on the planted receipt: {probe.stderr}")
        self.assertNotIn("Traceback", probe.stderr, f"--launch-ready crashed: {probe.stderr}")
        out = launcher.communicate(timeout=60)[0]
        self.assertEqual(launcher.returncode, 0, out)
        if expect_ready:
            self.assertNotIn("did not report ready", out)
        else:
            self.assertIn("did not report ready", out)
        subprocess.run(["tmux", "-S", str(self.socket), "kill-session", "-t", f"={self.session}-watcher"],
                       capture_output=True, timeout=10)

    def test_a_well_formed_receipt_under_the_live_nonce_is_accepted(self):
        # Positive control for the planted-receipt cases: the launcher does reach and accept
        # a matching receipt, so the refusals below are the parse/match branch, not a miss.
        self._launch_planting_receipt(lambda n: self._receipt(n), expect_ready=True)

    def test_same_second_prior_generation_is_not_readiness(self):
        # A watcher-shaped process on this exact inbox, its pid in a sentinel dated into the
        # launch: only a receipt carrying THIS launch's nonce counts.
        self._launch_not_ready(lambda: f"{self._bystander(argv_shape=True)}\n")

    def test_concurrent_foreign_generation_receipt_is_not_readiness(self):
        # Another launch's receipt: live pids, this inbox, a nonce this launcher never made.
        self._launch_not_ready(lambda: f"{self._bystander(argv_shape=True)}\n",
                               make_receipt=lambda: self._receipt("0" * 32))

    def test_stale_receipt_with_dead_pids_is_not_readiness(self):
        self._launch_planting_receipt(lambda n: self._receipt(n, watcher=self._dead_pid(), notifier=self._dead_pid()),
                                      expect_ready=False, receipt_age=3600)

    def test_malformed_and_partial_receipts_are_not_readiness(self):
        self._launch_planting_receipt(lambda n: "not a receipt\n", expect_ready=False)
        self._launch_planting_receipt(lambda n: f"nonce={n}\n", expect_ready=False)

    def test_receipt_with_non_positive_owner_pids_is_not_readiness(self):
        # kill(0, 0) and kill(-1, 0) succeed (a group, a set), so these must fail the parse, not the
        # probe -- one field at a time, its sibling a live pid, so each half of the guard is the decider.
        self._launch_planting_receipt(lambda n: self._receipt(n, watcher=0), expect_ready=False)
        self._launch_planting_receipt(lambda n: self._receipt(n, notifier=-1), expect_ready=False)
        self._launch_planting_receipt(lambda n: self._receipt(n, watcher=-1), expect_ready=False)
        self._launch_planting_receipt(lambda n: self._receipt(n, notifier=0), expect_ready=False)
        self._launch_planting_receipt(lambda n: self._receipt(n, watcher=10 ** 30), expect_ready=False)

    def test_receipt_for_an_adjacent_inbox_is_not_readiness(self):
        self._launch_planting_receipt(lambda n: self._receipt(n, inbox=str(self.root / "tasks-other")), expect_ready=False)

    def test_stale_sentinel_naming_a_live_unrelated_pid_is_not_readiness(self):
        self._launch_not_ready(lambda: f"{self._bystander()}\n", sentinel_age=3600)

    def test_stale_sentinel_naming_a_dead_pid_is_not_readiness(self):
        self._launch_not_ready(lambda: f"{self._dead_pid()}\n", sentinel_age=3600)

    def test_malformed_sentinel_payload_is_not_readiness(self):
        self._launch_not_ready(lambda: "not-a-pid\n", sentinel_age=3600)

    def test_ready_launch_publishes_a_receipt_for_its_own_watcher_and_removes_it_on_exit(self):
        if shutil.which("fswatch") is None:
            self.skipTest("fswatch not installed on this host")
        result = self._run_launcher(self._hermetic_path())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("did not report ready", result.stdout + result.stderr)
        sentinel = self._sentinel_path()
        receipts = list(sentinel.parent.glob(f"{sentinel.name}.launch.*"))
        self.assertEqual(len(receipts), 1, f"a ready launch must leave exactly its receipt: {receipts}")
        receipt = dict(l.split("=", 1) for l in receipts[0].read_text().splitlines())
        self.assertEqual(receipt["watcher"], sentinel.read_text().strip(),
                         "the receipt must name the pid that owns the ready sentinel")
        self.assertEqual(receipt["inbox"], str(self.root / "tasks"))
        self.assertEqual(len(receipt["nonce"]), 32)
        self.assertTrue(receipts[0].name.endswith(receipt["nonce"]), "the receipt path carries its own nonce")
        self.assertEqual(Path(f"{sentinel}.token").read_text().strip(), receipt["nonce"],
                         "the watcher's ready event must have written this launch's token")
        # A concurrent generation's receipt sits beside it; this owner's exit must not touch it.
        other = Path(f"{sentinel}.launch.{'b' * 32}")
        other.write_text(self._receipt("b" * 32))
        subprocess.run(["tmux", "-S", str(self.socket), "kill-session", "-t", f"={self.session}-watcher"],
                       capture_output=True, timeout=10)
        for _ in range(50):
            if not receipts[0].exists():
                break
            time.sleep(0.1)
        self.assertFalse(receipts[0].exists(), "the owner's exit must remove its own receipt")
        self.assertTrue(other.exists(), "the owner's exit removed another generation's receipt")

    def _watcher_root_pid(self, inbox):
        """This launch's watcher: the matching row whose parent is not itself a match
        (a $(...) subshell of the watcher carries the same argv)."""
        rows = subprocess.run(["ps", "-Ao", "pid=,ppid=,command="], capture_output=True, text=True).stdout
        matches = {}
        for row in rows.splitlines():
            parts = row.split(None, 2)
            if len(parts) == 3 and "watch-tasks-stream.sh" in parts[2] and f"--inbox {inbox}" in parts[2]:
                matches[parts[0]] = parts[1]
        roots = [pid for pid, ppid in matches.items() if ppid not in matches]
        return roots[0] if len(roots) == 1 else None

    def _reused_pid_sentinel_is_not_readiness(self, stamp_offset):
        # After the fork the sentinel holds exactly the child's pid, dated before the launch
        # (crash-left) or after it (clock stepped back): neither carries this launch's token.
        fake = self.bin / "fswatch"
        fake.write_text("#!/bin/bash\nexec sleep 300\n")
        fake.chmod(0o755)
        sentinel = self._sentinel_path()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        path = self._hermetic_path(exclude={"fswatch"})
        inbox = str(self.root / "tasks")
        launcher = subprocess.Popen(["/bin/bash", str(LAUNCHER)], env=self._env(path, {"SUTANDO_WATCHER_READY_TIMEOUT": "3"}),
                                    cwd=str(self.root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        child = None
        for _ in range(100):
            child = self._watcher_root_pid(inbox)
            if child:
                break
            time.sleep(0.05)
        self.assertIsNotNone(child, "this launch's watcher child never appeared")
        sentinel.write_text(f"{child}\n")
        os.utime(sentinel, (time.time() + stamp_offset, time.time() + stamp_offset))
        out = launcher.communicate(timeout=60)[0]
        self.assertEqual(launcher.returncode, 0, out)
        self.assertIn("did not report ready", out)
        self.assertEqual(list(sentinel.parent.glob(f"{sentinel.name}.launch.*")), [],
                         "a receipt was signed over a sentinel the child never wrote")

    def test_crash_left_sentinel_holding_the_reused_child_pid_is_not_readiness(self):
        self._reused_pid_sentinel_is_not_readiness(-3600)

    def test_future_dated_sentinel_holding_the_reused_child_pid_is_not_readiness(self):
        self._reused_pid_sentinel_is_not_readiness(+3600)

    def test_the_sweep_keeps_this_launch_receipt_and_removes_dead_generations(self):
        # The startup sweep, run on its own so the own receipt is provably present when it looks:
        # the own file must survive, a dead generation's and a malformed-owner one must not.
        sentinel = self._sentinel_path()
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        own_nonce, dead_nonce, zero_nonce, live_nonce = "a" * 32, "b" * 32, "c" * 32, "d" * 32
        own = Path(f"{sentinel}.launch.{own_nonce}")
        own.write_text(self._receipt(own_nonce, watcher=self._dead_pid(), notifier=self._dead_pid()))
        dead = Path(f"{sentinel}.launch.{dead_nonce}")
        dead.write_text(self._receipt(dead_nonce, watcher=self._dead_pid(), notifier=self._dead_pid()))
        zero = Path(f"{sentinel}.launch.{zero_nonce}")
        zero.write_text(self._receipt(zero_nonce, notifier=0))
        live = Path(f"{sentinel}.launch.{live_nonce}")
        live.write_text(self._receipt(live_nonce))
        temp = Path(f"{sentinel}.launch.{dead_nonce}.XXXXXX")
        temp.write_text("half-written\n")
        r = subprocess.run(["/bin/bash", str(REPO / "src/agent/agy/cli/task-notifier.sh"), "--sweep-receipts", own_nonce],
                           env=self._env(self._hermetic_path()), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(own.exists(), "the sweep removed this launch's own receipt")
        self.assertFalse(dead.exists(), "a dead generation's receipt survived the sweep")
        self.assertFalse(zero.exists(), "a receipt whose notifier is not a positive pid survived the sweep")
        self.assertTrue(live.exists(), "a live generation's receipt was swept")
        self.assertTrue(temp.exists(), "a temp file mid-publish was swept")


if __name__ == "__main__":
    unittest.main()
