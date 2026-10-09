#!/usr/bin/env python3
"""`start-cli.sh --witness <name>` boots a throwaway core beside production, and
`--witness-stop <name>` removes only what it made.

Real launcher, real tmux, real workspace resolution (a copied checkout with the
tracked sutando.config.json), a stub `claude`. A fake production workspace and a
decoy "production" tmux server stand in for the live core; the real default
socket is never touched.
Run: python3 tests/start-cli-witness-mode.test.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMUX = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
PGREP_STUB = ('[ "$*" = "-ax claude" ] || exit 1\n'
              '[ -s "$HOME/claude.pid" ] && echo "$(cat "$HOME/claude.pid") claude"\n')
CLAUDE_STUB = ('printf "%s\\n" "$@" > "$HOME/claude.argv"\n'
               'echo $$ > "$HOME/claude.pid"\nsleep 120\n')
SINGLETONS = ("state/shutdown.sentinel", "state/cores/testhost.alive", "state/core-supervisor.json",
              "state/session-starts.log", "logs/restart-attempts.log", "state/core-status.json")


def snapshot(root: Path) -> dict:
    """Every file and directory under root with its mtime and size."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            st = p.lstat()
            out[str(p.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    return out


def seed_production(ws: Path) -> None:
    an_hour_ago = time.time() - 3600
    for rel in SINGLETONS:
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"production": true}\n')
    # A live production relay loop: a launch that ignores the witness must not start a second.
    (ws / "state/core-supervisor-relay-loop.pid").write_text(str(os.getpid()))
    for dirpath, dirnames, filenames in os.walk(ws):
        for n in dirnames + filenames + [""]:
            os.utime(Path(dirpath) / n, (an_hour_ago, an_hour_ago))


class Harness:
    def __init__(self, linked: bool = True) -> None:
        if not TMUX:
            raise unittest.SkipTest("tmux not found")
        self.td = Path(tempfile.mkdtemp(prefix="wit"))
        self.root = self.td / "checkout"
        shutil.copytree(REPO / "src", self.root / "src", symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "node_modules"))
        shutil.copytree(REPO / "scripts", self.root / "scripts", symlinks=True)
        shutil.copy2(REPO / "sutando.config.json", self.root / "sutando.config.json")
        if linked:
            (self.root / ".git").write_text(f"gitdir: {self.td}/main/.git/worktrees/checkout\n")
        else:
            (self.root / ".git").mkdir()
        # Where a failed override would land: this checkout's own default workspace.
        self.checkout_ws = self.root / "workspace"
        seed_production(self.checkout_ws)
        # The production workspace a worker caller's env points at.
        self.prod_ws = self.td / "prod-ws"
        seed_production(self.prod_ws)
        self.tmp = self.td / "t"
        self.tmp.mkdir()
        bind = self.td / "bin"
        bind.mkdir()
        self.home = self.td / "home"
        self.home.mkdir()
        self.ccd = self.td / "ccd"
        self.ccd.mkdir()
        for stub, body in (("claude", CLAUDE_STUB), ("pgrep", PGREP_STUB),
                           ("lsof", "exit 1\n"), ("launchctl", "exit 1\n")):
            (bind / stub).write_text("#!/bin/bash\n" + body)
            (bind / stub).chmod(0o755)
        self.decoy_sock = self.td / "decoy.sock"
        subprocess.run([TMUX, "-S", str(self.decoy_sock), "new-session", "-d", "-s", "sutando-core", "sleep 300"],
                       check=True)
        self.env = {"PATH": f"{bind}:{Path(TMUX).parent}:/usr/bin:/bin:/usr/sbin",
                    "HOME": str(self.home), "TMPDIR": str(self.tmp),
                    # A worker caller's routing env: none of it may reach the witness.
                    "SUTANDO_TMUX_SOCKET": str(self.decoy_sock), "SUTANDO_TMUX_SESSION": "sutando-core",
                    "SUTANDO_WORKSPACE_DIR": str(self.prod_ws), "SUTANDO_TASKS_DIR": str(self.prod_ws / "tasks"),
                    "SUTANDO_RESULTS_DIR": str(self.prod_ws / "results"),
                    "SUTANDO_MEMORY_DIR": str(self.prod_ws / "memory"), "SUTANDO_INSTANCE_ID": "f" * 32,
                    "SUTANDO_CORE_SESSION": "1", "CLAUDE_CONFIG_DIR": str(self.ccd)}
        self.before = {"checkout": snapshot(self.checkout_ws), "prod": snapshot(self.prod_ws)}

    def witness(self, name: str):
        base = Path(os.path.realpath(self.tmp)) / "sutando-witness" / name
        return base, base / "tmux.sock", f"witness-{name}-core", base / "workspace"

    def run(self, *args, entry="src/agent/claude/cli/start-cli.sh", extra_env=None):
        return subprocess.run(["/bin/bash", str(self.root / entry), *args],
                              env={**self.env, **(extra_env or {})}, capture_output=True, text=True, timeout=90)

    def tm(self, sock, *a):
        return subprocess.run([TMUX, "-S", str(sock), *a], capture_output=True, text=True)

    def production_untouched(self, case: unittest.TestCase):
        case.assertEqual(self.before["checkout"], snapshot(self.checkout_ws),
                         "the checkout's own workspace changed")
        case.assertEqual(self.before["prod"], snapshot(self.prod_ws), "the production workspace changed")
        case.assertEqual(self.tm(self.decoy_sock, "has-session", "-t", "=sutando-core").returncode, 0,
                         "the decoy production session is gone")

    def close(self):
        for name in ("t1", "t2"):
            if self.witness(name)[0].exists():
                self.run("--witness-stop", name)
        self.tm(self.decoy_sock, "kill-server")
        shutil.rmtree(self.td, ignore_errors=True)


class WitnessLaunch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.run_ = cls.h.run("--witness", "t1")
        cls.root, cls.sock, cls.session, cls.ws = cls.h.witness("t1")

    @classmethod
    def tearDownClass(cls):
        cls.h.close()

    def test_launch_succeeds(self):
        self.assertEqual(self.run_.returncode, 0, self.run_.stdout + self.run_.stderr)

    def test_core_runs_in_its_own_session_on_its_own_socket(self):
        sessions = self.h.tm(self.sock, "list-sessions", "-F", "#{session_name}").stdout.split()
        self.assertIn(self.session, sessions)
        self.assertIn(self.session + "-watcher", sessions)
        self.assertNotIn("sutando-core", sessions)

    def test_production_singletons_untouched(self):
        self.h.production_untouched(self)
        self.assertTrue((self.h.prod_ws / "state/shutdown.sentinel").exists(), "production sentinel cleared")

    def test_notifier_is_aimed_at_the_witness_inbox_and_session(self):
        env = self.h.tm(self.sock, "show-environment", "-t", f"={self.session}-watcher").stdout
        for want in (f"SUTANDO_TASKS_DIR={self.ws}/tasks", f"SUTANDO_RESULTS_DIR={self.ws}/results",
                     f"SUTANDO_WORKSPACE_DIR={self.ws}", f"SUTANDO_TMUX_SOCKET={self.sock}",
                     f"SUTANDO_TMUX_SESSION={self.session}"):
            self.assertIn(want, env)
        self.assertNotIn(str(self.h.prod_ws), env)

    def test_caller_routing_env_does_not_reach_the_witness(self):
        glob = self.h.tm(self.sock, "show-environment", "-g").stdout
        for leaked in ("SUTANDO_MEMORY_DIR", "SUTANDO_INSTANCE_ID", str(self.h.prod_ws)):
            self.assertNotIn(leaked, glob)

    def test_workspace_override_resolves_to_the_witness(self):
        got = subprocess.run(["bash", str(self.h.root / "scripts/sutando-config.sh"), "workspace"],
                             env={"PATH": self.h.env["PATH"], "HOME": str(self.h.home)},
                             capture_output=True, text=True).stdout
        self.assertEqual(got, str(self.ws))

    def test_auth_is_the_callers_config_dir_by_reference(self):
        glob = self.h.tm(self.sock, "show-environment", "-g", "CLAUDE_CONFIG_DIR").stdout.strip()
        self.assertEqual(glob, f"CLAUDE_CONFIG_DIR={self.h.ccd}")
        self.assertEqual(sorted(p.name for p in self.h.ccd.iterdir()), [".claude.json"])

    def test_no_owner_surfaces_and_no_startup_ceremony(self):
        argv = (self.h.home / "claude.argv").read_text().split("\n")
        self.assertIn(self.session, argv)
        self.assertNotIn("--remote-control", argv)
        self.assertNotIn("--chrome", argv)
        self.assertNotIn("/startup", argv)

    def test_heartbeat_writer_logs_inside_the_witness(self):
        self.assertTrue((self.ws / "logs/core-heartbeat.log").exists(), "heartbeat log went to the shared default")

    def test_monitor_and_relay_never_started(self):
        self.assertFalse((self.ws / "state/core-supervisor.json").exists())
        self.assertFalse((self.ws / "state/core-supervisor-relay-loop.pid").exists())

    def test_second_launch_of_the_same_name_refuses(self):
        run = self.h.run("--witness", "t1")
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        self.assertIn("already exists", run.stderr)


class WitnessTeardown(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        run = self.h.run("--witness", "t2")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.root, self.sock, self.session, self.ws = self.h.witness("t2")

    def tearDown(self):
        self.h.close()

    def test_stop_removes_only_witness_resources(self):
        run = self.h.run("--witness-stop", "t2")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertNotEqual(self.h.tm(self.sock, "list-sessions").returncode, 0, "witness tmux server survived")
        self.assertFalse(self.root.exists(), "witness root not removed")
        self.assertFalse((self.h.root / "sutando.config.local.json").exists(), "override left behind")
        self.h.production_untouched(self)

    def test_stop_leaves_a_config_it_did_not_write(self):
        cfg = self.h.root / "sutando.config.local.json"
        cfg.write_text('{"workspace": {"path": "/somewhere/else"}}\n')
        run = self.h.run("--witness-stop", "t2")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertEqual(cfg.read_text(), '{"workspace": {"path": "/somewhere/else"}}\n')

    def test_stop_from_another_checkout_refuses(self):
        other = Harness()
        try:
            run = other.run("--witness-stop", "t2", extra_env={"TMPDIR": str(self.h.tmp)})
            self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
            self.assertEqual(self.h.tm(self.sock, "has-session", "-t", f"={self.session}").returncode, 0)
        finally:
            other.close()


class WitnessRefusals(unittest.TestCase):
    def setUp(self):
        self.h = Harness()

    def tearDown(self):
        self.h.close()

    def assertRefusedCleanly(self, run, needle):
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        self.assertIn(needle, run.stderr)
        self.assertFalse((self.h.root / "sutando.config.local.json").exists())
        self.assertFalse((Path(os.path.realpath(self.h.tmp)) / "sutando-witness").exists())
        self.assertFalse((self.h.home / "claude.pid").exists(), "a claude was launched")
        self.h.production_untouched(self)

    def test_main_checkout_refuses(self):
        h = Harness(linked=False)
        try:
            run = h.run("--witness", "t1")
            self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
            self.assertIn("not a linked git worktree", run.stderr)
            self.assertFalse((h.root / "sutando.config.local.json").exists())
        finally:
            h.close()

    def test_existing_local_config_refuses(self):
        cfg = self.h.root / "sutando.config.local.json"
        cfg.write_text("{}\n")
        run = self.h.run("--witness", "t1")
        self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
        self.assertEqual(cfg.read_text(), "{}\n")

    def test_live_core_in_checkout_workspace_refuses(self):
        alive = self.h.checkout_ws / "state/cores/testhost.alive"
        os.utime(alive, None)
        self.h.before["checkout"] = snapshot(self.h.checkout_ws)
        self.assertRefusedCleanly(self.h.run("--witness", "t1"), "live core")

    def test_witness_flag_after_another_flag_refuses(self):
        self.assertRefusedCleanly(self.h.run("--restart", "--witness", "t1"), "must be the first argument")

    def test_dispatcher_routes_witness_past_the_production_reap(self):
        run = self.h.run("--restart", "--witness", "t1", entry="src/agent/start-cli.sh")
        self.assertRefusedCleanly(run, "must be the first argument")

    def test_dispatcher_refuses_witness_for_codex(self):
        run = self.h.run("--runtime", "codex", "--witness", "t1", entry="src/agent/start-cli.sh")
        self.assertRefusedCleanly(run, "only for the claude runtime")

    def test_equals_form_refuses(self):
        self.assertRefusedCleanly(self.h.run("--witness=t1"), "no '='")

    def test_extra_arguments_refuse(self):
        self.assertRefusedCleanly(self.h.run("--witness", "t1", "--visible"), "no other arguments")

    def test_bad_names_refuse(self):
        for name in ("", "../x", "Sutando", "a" * 21, "-x"):
            with self.subTest(name=name):
                self.assertRefusedCleanly(self.h.run("--witness", name), "name")

    def test_stop_of_unknown_witness_refuses(self):
        self.assertRefusedCleanly(self.h.run("--witness-stop", "t1"), "no witness")


class ProductionTargetGuard(unittest.TestCase):
    """The guard both stages call, fed the values a misderivation could produce."""

    def guard(self, *args, ambient=()):
        script = ('REPO="$1"; shift; . "$REPO/src/agent/claude/cli/witness-mode.sh"; '
                  'witness_assert_not_production "$@"')
        return subprocess.run(["/bin/bash", "-c", script, "guard", str(REPO), *args, *ambient],
                              capture_output=True, text=True)

    def test_default_socket_refused(self):
        self.assertEqual(self.guard("/tmp/sutando-tmux.sock", "witness-x-core").returncode, 2)

    def test_default_socket_refused_in_its_physical_spelling(self):
        # Only macOS aliases /tmp to /private/tmp; elsewhere that spelling is a different path.
        if os.path.realpath("/private/tmp") != os.path.realpath("/tmp"):
            self.skipTest("this host does not alias /private/tmp to /tmp")
        self.assertEqual(self.guard("/private/tmp/sutando-tmux.sock", "witness-x-core").returncode, 2)

    def test_default_session_refused(self):
        self.assertEqual(self.guard("/x/w.sock", "sutando-core").returncode, 2)

    def test_callers_own_socket_or_session_refused(self):
        self.assertEqual(self.guard("/x/w.sock", "witness-x-core", ambient=("/x/w.sock", "")).returncode, 2)
        self.assertEqual(self.guard("/x/w.sock", "witness-x-core", ambient=("", "witness-x-core")).returncode, 2)

    def test_non_witness_session_refused(self):
        self.assertEqual(self.guard("/x/w.sock", "sutando-worker-abc").returncode, 2)

    def test_witness_target_allowed(self):
        run = self.guard("/x/w.sock", "witness-x-core", ambient=("/tmp/sutando-tmux.sock", "sutando-core"))
        self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == "__main__":
    unittest.main()
