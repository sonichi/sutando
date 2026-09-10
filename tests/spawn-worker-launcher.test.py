#!/usr/bin/env python3
"""Creating a worker produces all four parts, or none.

The design's positive invariant: every task worker is associated with a task
delivery mechanism. A folder nobody reads is a name in a roster, so a partial
spawn is worse than a refused one.

tmux is injected, so these run with no tmux server and never touch a real
socket. That is the point — the launcher's ORDERING and REFUSALS are the
behaviour under test, not tmux itself.

Run: python3 tests/spawn-worker-launcher.test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import spawn_worker as sw  # noqa: E402
import worker_identity as wi  # noqa: E402


class FakeTmux:
    """Records argv; answers has-session from a set of names it knows."""
    def __init__(self, existing=(), fail_on=None):
        self.calls, self.existing, self.fail_on = [], set(existing), fail_on

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        sub = argv[3] if len(argv) > 3 else ""
        if sub == "has-session":
            name = argv[-1].lstrip("=")
            return subprocess.CompletedProcess(argv, 0 if name in self.existing else 1, "", "")
        if self.fail_on and sub == self.fail_on:
            return subprocess.CompletedProcess(argv, 1, "", f"{sub} exploded")
        if sub == "new-session":
            self.existing.add(argv[argv.index("-s") + 1])
            # Real tmux prints the new pane's id under -P -F. Returning "" here
            # is what let a broken send-keys target pass as correct.
            return subprocess.CompletedProcess(argv, 0, "%9", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class Base(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name)


class TestPlan(Base):
    def test_plan_creates_nothing(self):
        p = sw.plan(self.ws, REPO)
        self.assertFalse(Path(p["delivery_dir"]).exists())
        self.assertFalse((self.ws / "state" / "workers").exists())

    def test_the_tmux_name_comes_from_the_id(self):
        p = sw.plan(self.ws, REPO)
        self.assertEqual(p["tmux"]["session_name"], f"sutando-worker-{p['worker_id']}")

    def test_the_watcher_is_pointed_at_the_workers_own_folder(self):
        p = sw.plan(self.ws, REPO)
        self.assertEqual(p["watcher_argv"][-1], p["delivery_dir"])
        self.assertIn(p["worker_id"], p["watcher_argv"][-1])

    def test_working_directory_is_separate_from_the_delivery_directory(self):
        p = sw.plan(self.ws, REPO, cwd="/dev/proj")
        self.assertEqual(p["cwd"], "/dev/proj")
        self.assertNotIn("/dev/proj", p["delivery_dir"])


class TestRefusals(Base):
    def test_refuses_when_the_sentinel_is_not_per_instance(self):
        """Spawning there corrupts the CORE's stamp, not the worker's."""
        with self.assertRaises(sw.SpawnRefused) as e:
            sw.spawn(self.ws, "/nonexistent-checkout", runner=FakeTmux())
        self.assertIn("sentinel", str(e.exception).lower())

    def test_a_refusal_creates_nothing(self):
        with self.assertRaises(sw.SpawnRefused):
            sw.spawn(self.ws, "/nonexistent-checkout", runner=FakeTmux())
        self.assertFalse((self.ws / "deliveries").exists())
        self.assertFalse((self.ws / "state" / "workers").exists())

    def test_refuses_a_tmux_name_already_taken(self):
        """Ids are minted, so a real collision is vanishing — but if the name is
        taken, adopting the existing session would hand this worker someone
        else's pane."""
        probe = sw.plan(self.ws, REPO)
        t = FakeTmux(existing=[probe["tmux"]["session_name"]])
        import worker_identity as _wi
        orig = _wi.new_worker_id
        _wi.new_worker_id = lambda: probe["worker_id"]      # force the collision
        try:
            with self.assertRaises(sw.SpawnRefused) as e:
                sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
            self.assertIn("already exists", str(e.exception))
        finally:
            _wi.new_worker_id = orig

    def test_a_tmux_failure_surfaces_rather_than_half_creating(self):
        with self.assertRaises(sw.SpawnRefused) as e:
            sw.spawn(self.ws, REPO, runner=FakeTmux(fail_on="new-session"),
                     require_sentinel=False)
        self.assertIn("new-session", str(e.exception))


class TestSpawn(Base):
    def test_all_four_parts_exist(self):
        t = FakeTmux()
        got = sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        w = got["worker_id"]
        self.assertTrue((self.ws / "deliveries" / w).is_dir(), "delivery folder")
        self.assertEqual(len(wi.sessions(self.ws, w)), 1, "lineage")
        self.assertEqual(len(wi.incarnations(self.ws, w)), 1, "run history")
        self.assertTrue(sw.session_exists(got["tmux"]["session_name"], runner=t), "tmux")

    def test_the_watcher_is_started_inside_that_session(self):
        t = FakeTmux()
        got = sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        keys = [c for c in t.calls if "send-keys" in c]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0][keys[0].index("-t") + 1], "%9")
        self.assertIn("watch-tasks-stream.sh", " ".join(keys[0]))
        self.assertIn(got["worker_id"], " ".join(keys[0]))

    def test_the_delivery_folder_matches_the_identity_record(self):
        """A record naming one id and a folder named another is a silent orphan."""
        got = sw.spawn(self.ws, REPO, runner=FakeTmux(), require_sentinel=False)
        self.assertEqual(Path(got["delivery_dir"]).name, got["worker_id"])
        self.assertEqual(wi.current(self.ws, got["worker_id"])["incarnation_id"],
                         got["incarnation_id"])

    def test_the_incarnation_carries_the_tmux_locator(self):
        got = sw.spawn(self.ws, REPO, runner=FakeTmux(), require_sentinel=False)
        inc = wi.incarnations(self.ws, got["worker_id"])[0]
        self.assertEqual(inc["tmux"]["session_name"], got["tmux"]["session_name"])

    def test_two_workers_do_not_collide(self):
        t = FakeTmux()
        a = sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        b = sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        self.assertNotEqual(a["worker_id"], b["worker_id"])
        self.assertNotEqual(a["delivery_dir"], b["delivery_dir"])
        self.assertNotEqual(a["tmux"]["session_name"], b["tmux"]["session_name"])

    def test_session_lookup_is_exact_not_prefix(self):
        """Without `=`, tmux prefix-matches and a short id hits another worker."""
        t = FakeTmux()
        sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        has = [c for c in t.calls if "has-session" in c][0]
        self.assertTrue(has[-1].startswith("="), has)

    def test_the_launcher_never_supervises_the_session(self):
        """launchd supervises the WATCHER; a restart is not a resume."""
        t = FakeTmux()
        sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        joined = " ".join(" ".join(c) for c in t.calls)
        for forbidden in ("respawn", "kill-session", "restart"):
            self.assertNotIn(forbidden, joined)


class TestWatcherStartFailure(Base):
    def test_a_failed_send_keys_surfaces(self):
        """The session exists but nothing reads the folder — the one state the
        invariant forbids, so it must raise rather than return."""
        with self.assertRaises(sw.SpawnRefused) as e:
            sw.spawn(self.ws, REPO, runner=FakeTmux(fail_on="send-keys"),
                     require_sentinel=False)
        self.assertIn("watcher failed", str(e.exception))


class TestSentinelProbe(Base):
    def test_true_on_a_checkout_that_has_it(self):
        self.assertTrue(sw.per_instance_sentinel_supported(REPO))

    def test_false_on_a_checkout_without_it(self):
        fake = self.ws / "repo" / "src"
        fake.mkdir(parents=True)
        (fake / "util_paths.py").write_text("# no sentinel helper here\n")
        self.assertFalse(sw.per_instance_sentinel_supported(self.ws / "repo"))

    def test_false_when_the_file_is_absent(self):
        self.assertFalse(sw.per_instance_sentinel_supported(self.ws / "nope"))


class TestRunner(Base):
    def test_the_real_runner_captures_output(self):
        r = sw._run(["echo", "hello"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("hello", r.stdout)


class TestSocketResolution(Base):
    def test_the_socket_is_read_at_call_time_not_import_time(self):
        """A module-level default binds at import, so a caller that sets the env
        afterwards silently targets the WRONG tmux server — which is how a test
        worker lands on the live core's socket."""
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"SUTANDO_TMUX_SOCKET": "/tmp/other.sock"}):
            self.assertEqual(sw.default_socket(), "/tmp/other.sock")
            p = sw.plan(self.ws, REPO)
            self.assertEqual(p["tmux"]["socket"], "/tmp/other.sock")

    def test_spawn_uses_the_env_socket(self):
        import os
        from unittest.mock import patch
        t = FakeTmux()
        with patch.dict(os.environ, {"SUTANDO_TMUX_SOCKET": "/tmp/other.sock"}):
            sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        for call in t.calls:
            self.assertEqual(call[2], "/tmp/other.sock", call)

    def test_an_explicit_socket_still_wins(self):
        t = FakeTmux()
        sw.spawn(self.ws, REPO, runner=t, require_sentinel=False, socket="/tmp/x.sock")
        self.assertTrue(all(c[2] == "/tmp/x.sock" for c in t.calls))


class TestCli(Base):
    def test_dry_run_prints_a_plan_and_creates_nothing(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = sw.main(["--workspace", str(self.ws), "--repo", str(REPO), "--dry-run"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertIn("worker_id", out)
        self.assertFalse((self.ws / "deliveries").exists())

    def test_a_refusal_exits_2_and_says_why(self):
        import io
        from contextlib import redirect_stderr
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = sw.main(["--workspace", str(self.ws), "--repo", str(self.ws / "nope")])
        self.assertEqual(rc, 2)
        self.assertIn("refused", buf.getvalue())


class TestWatcherAddressing(Base):
    """Two things a fake tmux cannot fail on unless it is faithful."""

    def test_send_keys_targets_the_pane_id_not_the_session_name(self):
        """`=name` makes tmux resolve an exact PANE name, so it cannot find a
        session and the watcher never starts."""
        t = FakeTmux()
        sw.spawn(self.ws, str(REPO), runner=t, require_sentinel=False)
        new = [c for c in t.calls if "new-session" in c][0]
        send = [c for c in t.calls if "send-keys" in c][0]
        self.assertIn("-P", new)
        self.assertIn("#{pane_id}", new)
        self.assertEqual(send[send.index("-t") + 1], "%9")

    def test_the_watcher_declares_its_own_instance(self):
        """Without it the worker resolves the core's (agent, instance) key and
        stamps the core's watcher sentinel, which #3875 alone does not stop."""
        r = sw.spawn(self.ws, str(REPO), runner=FakeTmux(), require_sentinel=False)
        argv = r["watcher_argv"]
        self.assertEqual(argv[0], "env")
        self.assertEqual(argv[1], f"SUTANDO_INSTANCE_ID={r['worker_id']}")

    def test_the_watcher_comes_from_the_repo_not_the_working_directory(self):
        """WATCHER relative resolves against the session's cwd, so `cwd` would
        choose which code the worker runs, not just where it runs."""
        p = sw.plan(self.ws, "/anchor/repo", cwd="/some/other/checkout")
        self.assertEqual(p["cwd"], "/some/other/checkout")
        self.assertEqual(p["watcher_argv"][3], "/anchor/repo/src/watch-tasks-stream.sh")

    def test_two_workers_declare_different_instances(self):
        a = sw.spawn(self.ws, str(REPO), runner=FakeTmux(), require_sentinel=False)
        b = sw.spawn(self.ws, str(REPO), runner=FakeTmux(), require_sentinel=False)
        self.assertNotEqual(a["watcher_argv"][1], b["watcher_argv"][1])


if __name__ == "__main__":
    unittest.main(verbosity=0)
