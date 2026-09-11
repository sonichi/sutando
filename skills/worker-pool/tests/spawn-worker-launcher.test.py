#!/usr/bin/env python3
"""Creating a worker produces all four parts, or none.

The design's positive invariant: every task worker is associated with a task
delivery mechanism. A folder nobody reads is a name in a roster, so a partial
spawn is worse than a refused one.

tmux is injected, so these run with no tmux server and never touch a real
socket. That is the point — the launcher's ORDERING and REFUSALS are the
behaviour under test, not tmux itself.

Run: python3 skills/worker-pool/tests/spawn-worker-launcher.test.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import spawn_worker as sw  # noqa: E402
import worker_identity as wi  # noqa: E402


class FakeTmux:
    """Records argv + env; answers has-session from a set of names it knows.

    The core's launcher is faked as well: like the real one, it creates the
    session named by SUTANDO_TMUX_SESSION and exits non-zero when it cannot."""
    def __init__(self, existing=(), fail_on=None, runtime="claude"):
        self.calls, self.existing, self.fail_on = [], set(existing), fail_on
        self.envs, self.runtime = [], runtime

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.envs.append(dict(kw.get("env") or {}))
        if argv[0] == "bash" and argv[1].endswith("sutando-config.sh"):
            return subprocess.CompletedProcess(argv, 0, self.runtime + "\n", "")
        if argv[0] == "bash" and argv[1].endswith("start-cli.sh"):
            if self.fail_on == "launcher":
                return subprocess.CompletedProcess(argv, 1, "", "did not come up within ~5s")
            self.existing.add((kw.get("env") or {}).get("SUTANDO_TMUX_SESSION", ""))
            return subprocess.CompletedProcess(argv, 0, "Started detached.", "")
        sub = argv[3] if len(argv) > 3 else ""
        if sub == "has-session":
            name = argv[-1].lstrip("=")
            return subprocess.CompletedProcess(argv, 0 if name in self.existing else 1, "", "")
        if self.fail_on and sub == self.fail_on:
            return subprocess.CompletedProcess(argv, 1, "", f"{sub} exploded")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def launches(self):
        return [e for a, e in zip(self.calls, self.envs)
                if a[0] == "bash" and a[1].endswith("start-cli.sh")]


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
        self.assertEqual(p["env"]["SUTANDO_TASKS_DIR"], p["delivery_dir"])
        self.assertEqual(p["env"]["SUTANDO_WORKSPACE_DIR"], str(self.ws))
        self.assertIn(p["worker_id"], p["env"]["SUTANDO_TASKS_DIR"])

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

    def test_a_launcher_failure_surfaces_rather_than_half_creating(self):
        with self.assertRaises(sw.SpawnRefused) as e:
            sw.spawn(self.ws, REPO, runner=FakeTmux(fail_on="launcher"),
                     require_sentinel=False)
        self.assertIn("did not come up", str(e.exception))


class TestSpawn(Base):
    def test_all_four_parts_exist(self):
        t = FakeTmux()
        got = sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        w = got["worker_id"]
        self.assertTrue((self.ws / "deliveries" / w).is_dir(), "delivery folder")
        self.assertEqual(len(wi.sessions(self.ws, w)), 1, "lineage")
        self.assertEqual(len(wi.incarnations(self.ws, w)), 1, "run history")
        self.assertTrue(sw.session_exists(got["tmux"]["session_name"], runner=t), "tmux")

    def test_the_runtime_is_the_cores_launcher_under_worker_env(self):
        """No bare watcher in the pane: the agent starts its own, as the core
        does, so the session can both receive work and answer it."""
        t = FakeTmux()
        got = sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        envs = t.launches()
        self.assertEqual(len(envs), 1)
        env = envs[0]
        self.assertEqual(env["SUTANDO_TMUX_SESSION"], got["tmux"]["session_name"])
        self.assertEqual(env["SUTANDO_INSTANCE_ID"], got["worker_id"])
        self.assertEqual(env["SUTANDO_TASKS_DIR"], got["delivery_dir"])
        self.assertEqual(env["SUTANDO_WORKSPACE_DIR"], str(self.ws))
        self.assertEqual(env["SUTANDO_CLAUDE_SESSION_ID"], got["runtime_session_id"])
        self.assertFalse(any("send-keys" in c for c in t.calls))

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


class TestRuntimeStartFailure(Base):
    def test_a_failed_launch_surfaces(self):
        """A worker with no runtime is the one state the invariant forbids —
        a folder nobody reads — so it must raise rather than return."""
        with self.assertRaises(sw.SpawnRefused) as e:
            sw.spawn(self.ws, REPO, runner=FakeTmux(fail_on="launcher"),
                     require_sentinel=False)
        self.assertIn("launcher failed", str(e.exception))

    def test_the_recorded_session_id_is_the_one_the_runtime_is_told(self):
        """Lineage is only truthful if the id in the record IS the CLI session."""
        t = FakeTmux()
        got = sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        sessions = wi.sessions(self.ws, got["worker_id"])
        self.assertEqual(sessions[0]["session_id"], t.launches()[0]["SUTANDO_CLAUDE_SESSION_ID"])

    def test_the_worker_runs_the_runtime_the_core_is_configured_for(self):
        t = FakeTmux(runtime="claude")
        got = sw.spawn(self.ws, REPO, runner=t, require_sentinel=False)
        self.assertEqual(got["runtime"], "claude")
        argv = t.calls[-1]
        self.assertEqual(argv[argv.index("--runtime") + 1], "claude")

    def test_a_configured_runtime_without_worker_mode_is_refused(self):
        """Worker mode is a property of the ADAPTER. Spawning under one that
        has none reports a worker the launcher cannot actually isolate."""
        with self.assertRaises(sw.SpawnRefused):
            sw.spawn(self.ws, REPO, runner=FakeTmux(runtime="codex"),
                     require_sentinel=False)


class TestSentinelProbe(Base):
    def test_true_on_a_checkout_that_has_it(self):
        self.assertTrue(sw.per_instance_sentinel_supported(REPO))

    def test_false_on_a_checkout_without_it(self):
        fake = self.ws / "repo" / "src"
        fake.mkdir(parents=True)
        (fake / "util_paths.py").write_text("# no sentinel helper here\n")
        self.assertFalse(sw.per_instance_sentinel_supported(self.ws / "repo"))

    def test_false_on_a_checkout_that_only_SPELLS_it(self):
        """The probe reads a resolved path, not the source: one shared sentinel
        is refused however the function that returns it is named."""
        fake = self.ws / "shared" / "src"
        fake.mkdir(parents=True)
        (fake / "util_paths.py").write_text(
            "import sys\n"
            "def watcher_sentinel_path(state_dir, instance=None, agent=None):\n"
            "    return state_dir + '/watch-tasks-stream.pid'\n"
            "print(watcher_sentinel_path(sys.argv[2]))\n")
        self.assertFalse(sw.per_instance_sentinel_supported(self.ws / "shared"))

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
        self.assertEqual(t.launches()[0]["SUTANDO_TMUX_SOCKET"], "/tmp/other.sock")
        for call in (c for c in t.calls if c[0] == "tmux"):
            self.assertEqual(call[2], "/tmp/other.sock", call)

    def test_an_explicit_socket_still_wins(self):
        t = FakeTmux()
        sw.spawn(self.ws, REPO, runner=t, require_sentinel=False, socket="/tmp/x.sock")
        self.assertEqual(t.launches()[0]["SUTANDO_TMUX_SOCKET"], "/tmp/x.sock")
        self.assertTrue(all(c[2] == "/tmp/x.sock" for c in t.calls if c[0] == "tmux"))


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


class TestWorkerIsolation(Base):
    """What keeps N workers apart while they run one launcher."""

    def test_the_watcher_declares_its_own_instance(self):
        """Without it the worker resolves the core's (agent, instance) key and
        stamps the core's watcher sentinel, which #3875 alone does not stop."""
        t = FakeTmux()
        r = sw.spawn(self.ws, str(REPO), runner=t, require_sentinel=False)
        self.assertEqual(t.launches()[0]["SUTANDO_INSTANCE_ID"], r["worker_id"])

    def test_the_launcher_comes_from_the_repo_not_the_working_directory(self):
        """A relative path resolves against the session's cwd, so `cwd` would
        choose which code the worker runs, not just where it runs."""
        p = sw.plan(self.ws, "/anchor/repo", cwd="/some/other/checkout")
        self.assertEqual(p["cwd"], "/some/other/checkout")
        self.assertEqual(p["launcher_argv"],
                         ["bash", "/anchor/repo/src/agent/start-cli.sh", "--runtime", "claude"])
        self.assertEqual(p["env"]["SUTANDO_CLAUDE_WORKING_DIR"], "/some/other/checkout")

    def test_two_workers_declare_different_instances_and_sessions(self):
        t = FakeTmux()
        sw.spawn(self.ws, str(REPO), runner=t, require_sentinel=False)
        sw.spawn(self.ws, str(REPO), runner=t, require_sentinel=False)
        a, b = t.launches()
        for k in ("SUTANDO_INSTANCE_ID", "SUTANDO_TMUX_SESSION",
                  "SUTANDO_TASKS_DIR", "SUTANDO_CLAUDE_SESSION_ID"):
            self.assertNotEqual(a[k], b[k], k)

    def test_the_plan_uses_the_id_it_is_given(self):
        """The identity record is the authority; plan() must not mint a second
        id that is then thrown away."""
        p = sw.plan(self.ws, REPO, worker_id="a" * 32)
        self.assertEqual(p["worker_id"], "a" * 32)
        self.assertTrue(p["delivery_dir"].endswith("a" * 32))


class TestBootstrapSeam(Base):
    """The gate `/startup --worker` runs lives in THIS skill, so the core reaches
    it through env the spawner sets — never by naming a skill path."""

    def test_the_plan_names_this_skills_gate(self):
        gate = Path(sw.plan(self.ws, REPO)["env"]["SUTANDO_WORKER_BOOTSTRAP"])
        self.assertTrue(gate.is_file(), gate)
        self.assertEqual(gate.parent, Path(sw.__file__).resolve().parent)

    def test_the_named_gate_runs_and_decides(self):
        """Named, not just spelled: run what the plan points at and require a
        decision, so a rename that leaves a stale string is a failure."""
        gate = sw.plan(self.ws, REPO)["env"]["SUTANDO_WORKER_BOOTSTRAP"]
        r = subprocess.run([sys.executable, gate, "--instance", "", "--inbox", ""],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual((r.stdout or "").splitlines()[0], "unknown")

    def test_the_core_startup_skill_names_the_env_not_a_path(self):
        """The core boots with this skill absent, so its own `/startup` carries
        no path into it. Prose may name the skill; a runnable path may not."""
        skill = (REPO / "skills" / "startup" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("$SUTANDO_WORKER_BOOTSTRAP", skill)
        self.assertNotIn("skills/worker-pool", skill)
        # The scan is live: the core launcher's own suite reaches this skill by
        # path on purpose, and the same needle finds it there.
        control = REPO / "tests" / "start-cli-worker-env-forwarded.test.sh"
        self.assertIn("skills/worker-pool", control.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=0)
