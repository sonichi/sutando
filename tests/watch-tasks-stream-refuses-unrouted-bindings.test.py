#!/usr/bin/env python3
"""Unrouted startup is refused by the provider, without gating worker inboxes."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))
sys.path.insert(0, str(REPO / "tests/fixtures"))
sys.path.insert(0, str(REPO / "tests/_helpers"))
import skill_manifest_capability as smc
import routing_requirement as requirement
from clean_watcher_env import clean_env
from process_group import popen_bound

W = "a" * 32
KEY = "SUTANDO_TASK_ROUTING_REQUIREMENT"


class TestCapability(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.root = Path(self.t.name)
        self.patch = patch.dict(os.environ, clean_env(), clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def skill(self, root=None, command="./probe", code="exit 0"):
        folder = (root or self.root) / "skills/provider"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "manifest.json").write_text(json.dumps({"config": {KEY: command}}))
        target = folder / "probe"
        target.write_text("#!/bin/sh\n" + code + "\n")
        target.chmod(0o755)
        return target

    def check(self):
        return smc.main(["--check", str(self.root), KEY, str(self.root)])

    def test_absent_provider_permits_single_core(self):
        self.assertEqual(self.check(), 0)

    def test_provider_decides_and_failure_refuses(self):
        self.skill()
        self.assertEqual(self.check(), 0)
        self.skill(code="echo workers-declared; exit 1")
        self.assertEqual(self.check(), 1)
        self.skill(code="exit 9")
        self.assertEqual(self.check(), 1)

    def test_installed_but_missing_nonexecutable_or_escaping_probe_refuses(self):
        target = self.skill()
        target.unlink()
        self.assertEqual(self.check(), 1)
        self.skill().chmod(0o644)
        self.assertEqual(self.check(), 1)
        self.skill(command="../../outside")
        self.assertEqual(self.check(), 1)

    def test_timeout_refuses(self):
        self.skill()
        with patch.object(smc.subprocess, "run", side_effect=subprocess.TimeoutExpired("probe", 10)):
            self.assertEqual(self.check(), 1)

    def test_canonical_private_alias_wins_and_roots_are_deduplicated(self):
        target = self.skill()
        os.environ.update(SUTANDO_MEMORY_DIR=str(self.root), SUTANDO_PRIVATE_DIR=str(self.root / "ignored"))
        self.skill(self.root / "ignored")
        self.assertEqual(smc.providers(self.root, KEY), [("provider", target.resolve())])
        os.environ["SUTANDO_PRIVATE_DIR"] = str(self.root)
        self.assertEqual(len(smc.providers(self.root, KEY)), 1)

    def test_distinct_providers_refuse(self):
        self.skill()
        other = self.root / "private"
        self.skill(other)
        os.environ["SUTANDO_MEMORY_DIR"] = str(other)
        self.assertEqual(self.check(), 1)

    def test_nonproviders_are_skipped_and_usage_is_rejected(self):
        target = self.skill()
        manifest = target.parent / "manifest.json"
        for data in ("{bad", "[]", '{}', '{"enabled": false}', '{"config": {}}'):
            manifest.write_text(data)
            self.assertEqual(self.check(), 0)
        self.assertEqual(smc.main([]), 2)

    def test_private_alias_is_used_without_canonical(self):
        private = self.root / "private"
        target = self.skill(private)
        os.environ["SUTANDO_PRIVATE_DIR"] = str(private)
        self.assertEqual(smc.providers(self.root, KEY), [("provider", target.resolve())])

    def test_skill_error_and_usage_refuse(self):
        self.assertEqual(requirement.main([]), 2)
        with patch.object(requirement, "load_bindings", side_effect=requirement.RosterError("bad")):
            self.assertEqual(requirement.main([str(self.root)]), 1)

    def test_skill_uses_canonical_binding_loader(self):
        with patch.object(requirement, "load_bindings", return_value={"room": W}) as loader:
            self.assertEqual(requirement.main([str(self.root)]), 1)
            loader.assert_called_once_with(str(self.root))
        for bindings in ({}, {"room": "core"}, {"room": ["core"]}):
            with patch.object(requirement, "load_bindings", return_value=bindings):
                self.assertEqual(requirement.main([str(self.root)]), 0)


class WatcherHarness:
    """The real script, own workspace and TMPDIR; only recorded pids are killed.

    A mixin, not a base test case: inheriting the cases too would re-run every
    one of them per suite, and each start is a real process with a real timeout.
    """

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.ws = Path(self._t.name) / "ws"
        for d in ("tasks", "results", "state"):
            (self.ws / d).mkdir(parents=True)
        self.tmp = Path(self._t.name) / "tmp"
        self.tmp.mkdir()
        (self.ws / "tasks" / "task-seed1.txt").write_text("id: task-seed1\ntask: seed\n")
        self.procs = []
        self.addCleanup(self._kill_all)

    def _kill_all(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)

    def _bare_checkout(self):
        """A checkout whose skills/ declares nothing -- an install without the
        pool. `src`/`scripts` are symlinked and `cd`+`pwd` is logical, so the
        watcher's own __REPO_ROOT is this tree and its manifest scan finds none."""
        root = Path(self._t.name) / "bare-repo"
        root.mkdir(exist_ok=True)
        (root / "skills").mkdir(exist_ok=True)
        for d in ("src", "scripts"):
            link = root / d
            if not link.exists():
                link.symlink_to(REPO / d)
        return root

    def _start(self, inbox=None, checkout="installed", **env_extra):
        """`checkout` selects which tree the watcher runs from: "installed" (this
        repo, where a skill declares the handler capability) or "bare" (a tree
        with no skill declaring it -- the refusal cases)."""
        env = clean_env()
        env.update({"TMPDIR": str(self.tmp), "SUTANDO_WORKSPACE_DIR": str(self.ws)})
        env.update(env_extra)
        root = self._bare_checkout() if checkout == "bare" else REPO
        inbox = Path(inbox) if inbox else (self.ws / "tasks")
        self.out = self.tmp / f"out{len(self.procs)}"
        self.err = self.tmp / f"err{len(self.procs)}"
        with open(self.out, "w") as o, open(self.err, "w") as e:
            p = popen_bound(["bash", f"{root}/src/watch-tasks-stream.sh", str(inbox), "--role", "session", "--inbox", str(inbox)],
                                 cwd=str(self.tmp), env=env, stdout=o, stderr=e,
                                 text=True)
        p._out, p._err = self.out, self.err
        self.procs.append(p)
        return p

    def _bind(self, target=W):
        (self.ws / "state" / "bindings.json").write_text(json.dumps({"bindings": {"!r:x": target}}))

    def _assert_refused(self, p):
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.fail("watcher did not exit; it should have refused")
        err = p._err.read_text()
        self.assertEqual(p.returncode, 78, err)
        self.assertIn("REFUSING to start", err)
        self.assertIn("SUTANDO_TASK_EVENT_HANDLER=", err)
        self.assertIn("SUTANDO_ALLOW_UNROUTED_BINDINGS=1", err)
        return err

    def _assert_started(self, p):
        deadline = time.time() + 20
        while time.time() < deadline:
            if "TASK_FILE:" in p._out.read_text():
                self.assertNotIn("REFUSING to start", p._err.read_text())
                return
            if p.poll() is not None:
                break
            time.sleep(0.2)
        self.fail(f"watcher never swept the seeded task (rc={p.poll()}): "
                  f"{p._err.read_text()[-400:]}")


class TestWatcher(WatcherHarness, unittest.TestCase):
    def test_bound_core_refuses_without_handler(self):
        self._bind()
        self._assert_refused(self._start())

    def test_corrupt_binding_refuses(self):
        (self.ws / "state/bindings.json").write_text("{bad json")
        self._assert_refused(self._start())

    def test_absent_or_core_only_binding_starts(self):
        self._assert_started(self._start())

    def test_core_only_binding_starts(self):
        self._bind("core")
        self._assert_started(self._start())

    def test_no_skill_installed_has_no_pool_prerequisite(self):
        self._assert_started(self._start(checkout="bare"))

    def test_opt_out_starts(self):
        self._bind()
        self._assert_started(self._start(SUTANDO_ALLOW_UNROUTED_BINDINGS="1"))

    def test_nonexecutable_and_directory_overrides_refuse(self):
        self._bind()
        target = self.ws / "handler"
        target.write_text("#!/bin/sh\nexit 3\n")
        self._assert_refused(self._start(SUTANDO_TASK_EVENT_HANDLER=str(target)))
        self._assert_refused(self._start(SUTANDO_TASK_EVENT_HANDLER=str(self.ws)))

    def test_directory_config_refuses(self):
        self._bind()
        (self.ws / "state/task-event-handler.json").write_text(json.dumps({"handler": str(self.ws)}))
        self._assert_refused(self._start())

    def test_worker_delivery_bypasses_core_prerequisite(self):
        self._bind()
        inbox = self.ws / "deliveries" / W
        inbox.mkdir(parents=True)
        (inbox / "task-seed1.txt").write_text("id: task-seed1\ntask: seed\n")
        self._assert_started(self._start(inbox=inbox))

    def test_live_config_routes_without_environment_or_repository_cwd(self):
        self._bind()
        target = self.ws / "handler"
        marker = self.ws / "routed"
        target.write_text(f'#!/bin/sh\n[ "$1" = "--probe" ] && exit 0\ntouch "{marker}"\nexit 0\n')
        target.chmod(0o755)
        (self.ws / "state/task-event-handler.json").write_text(json.dumps({"handler": str(target)}))
        p = self._start()
        deadline = time.monotonic() + 25
        while not marker.exists() and time.monotonic() < deadline and p.poll() is None:
            time.sleep(0.1)
        self.assertTrue(marker.exists(), p._err.read_text())
        self.assertNotIn("TASK_FILE:", p._out.read_text())


if __name__ == "__main__":
    unittest.main()
