"""install --replace-install: an updated app's new environment takes a running agent's job over.

The session an agent runs was started from the environment it was connected with. Every install from
another copy of the package was refused ("... of another install"), so an app update could never move a
connected agent onto the ag2-sparrow it ships: all eight local agents of a test machine were refused. Run:
    python3 packages/ag2-sparrow/tests/test_install_replaces_other_install.py
"""
import argparse
import contextlib
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ag2_sparrow import connect_common as cm, holder_lock, task_io  # noqa: E402

claude, codex, agy, openclaw = (importlib.import_module("ag2_sparrow.adapters." + name) for name in (
    "claude_code.cli", "codex.connect_cli", "antigravity.cli", "openclaw.cli"))

ROOT = "/agents/helper"
OLD = "ag2-sparrow@/app/sparrow/venvs/0.11.0-aaaa/lib/python3/site-packages/ag2_sparrow"
NEW = "ag2-sparrow@/app/sparrow/venvs/0.12.0-bbbb/lib/python3/site-packages/ag2_sparrow"


class ReplaceInstall(unittest.TestCase):
    def setUp(self):
        self.addCleanup(cm.take_over, argparse.Namespace())

    def facts(self, shape, adapter=None, install=OLD, live=True, recorded=ROOT, job=4242):
        """The machine as _managed_here reads it: `adapter`'s session of `install` (pid 4242) holds the root, the
        job records `recorded`, and the manager runs the job as pid `job`."""
        rec = {"live": live, "adapter": adapter or shape.name, "pid": 4242, "install_id": install}
        stack = contextlib.ExitStack()
        for name, value in (("owned_config", ({"ROOT": recorded}, "")), ("lock_holder", rec), ("install_id", NEW),
                            ("manager_pid", job), ("_parent_pid", 1), ("_spawning_launcher", None)):
            stack.enter_context(mock.patch.object(cm, name, return_value=value))
        return stack

    def held(self, shape, root=ROOT, **facts):
        with self.facts(shape, **facts):
            return cm._managed_here(shape, root)

    def test_a_session_of_another_install_is_refused_unless_the_install_says_replace(self):
        shape = claude.LOCAL
        self.assertIn("of another install", self.held(shape))
        cm.take_over(argparse.Namespace(replace_install=True))
        self.assertEqual(self.held(shape), "", "the job's own session, started from the environment before")
        # The next invocation without the option refuses again.
        cm.take_over(argparse.Namespace())
        self.assertIn("of another install", self.held(shape))

    def test_replace_takes_over_only_this_adapters_live_session_on_the_jobs_own_root(self):
        shape = claude.LOCAL
        cm.take_over(argparse.Namespace(replace_install=True))
        self.assertIn("is held by codex-local pid 4242", self.held(shape, adapter="codex-local"))
        self.assertIn("a holder with no live record", self.held(shape, live=False))
        self.assertIn("is active for /agents/other", self.held(shape, recorded="/agents/other"))
        self.assertEqual(self.held(shape, install=NEW), "", "its own install's session, as before")

    def test_a_holder_the_manager_does_not_run_is_refused_before_anything_is_stopped(self):
        # Stopping the job would not free a root held by a session started by hand: the stop would only be an outage.
        shape = claude.LOCAL
        cm.take_over(argparse.Namespace(replace_install=True))
        for job in (777, None):
            with self.subTest(job=job):
                self.assertIn("is not the process", self.held(shape, job=job))
                self.assertIn("nothing was stopped", self.held(shape, job=job))
                with self.facts(shape, job=job), contextlib.redirect_stderr(io.StringIO()), \
                        mock.patch.object(cm, "owned_text", return_value=("unit", "")), \
                        mock.patch.object(cm, "render_supervisor", return_value=("unit", "")), \
                        mock.patch.object(cm, "lock_held", return_value="held"), \
                        mock.patch.object(cm, "manager_state", return_value="active"), \
                        mock.patch.object(cm, "stop_supervisor") as stop, mock.patch.object(cm, "_write") as write:
                    self.assertNotEqual(cm._install_locked(shape, {}, ROOT), 0)
                stop.assert_not_called()
                write.assert_not_called()
        # The job's own launcher, or its child, is taken over.
        with self.facts(shape, job=1):
            self.assertEqual(cm._managed_here(shape, ROOT), "")

    def test_every_local_adapters_install_takes_the_option_and_reads_it(self):
        for mod in (claude, codex, agy, openclaw):
            with self.subTest(adapter=mod.COMMAND):
                # Parsed (argparse would exit on an unknown option) and handed to the shared reader first.
                with mock.patch.object(cm, "take_over", side_effect=RuntimeError("read")) as read:
                    with self.assertRaises(RuntimeError):
                        mod.main(["install", "--agent", "@helper:s.example", "--replace-install"])
                self.assertIs(read.call_args.args[0].replace_install, True)


class AfterTheHostWasRenamed(unittest.TestCase):
    """The state an updated app found: the agent's own job, up through a network change, holds its own folder
    under the name the Mac had when it started; install --agent for that agent runs from the newer environment."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(os.path.realpath(tmp.name))
        self.root = self.home / "agents" / "helper"
        self.root.mkdir(parents=True)
        for target, name, value in ((holder_lock, "_home", lambda: str(self.home)), (holder_lock, "_HOST_NAME_DRIFTS", True)):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(cm.take_over, argparse.Namespace())
        cm.take_over(argparse.Namespace(replace_install=True))

    def session(self, adapter, host="mac.lan"):
        """A live process recorded as `adapter`'s holder of the root by the 0.11.0 environment, under `host`."""
        proc = subprocess.Popen(["sleep", "60"])
        self.addCleanup(proc.wait, 30)
        self.addCleanup(proc.kill)
        start = holder_lock.start_time(proc.pid)
        rec = {"schema": 2, "adapter": adapter, "pid": proc.pid, "start": start, "host": host, "install_id": OLD,
               "lock": {"dev": 1, "ino": 2}, "acquired_at": 1.5, "owner_pid": proc.pid, "owner_start": start,
               "consumer": None, "tombstone": None}
        directory = Path(holder_lock.lock_dir(str(self.root)))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / holder_lock.HOLDER_NAME).write_text(json.dumps(rec))
        return proc

    def held(self, shape, job):
        """_managed_here over the real record: the job records the root and the manager runs it as pid `job`."""
        with contextlib.ExitStack() as stack:
            for name, value in (("owned_config", ({"ROOT": str(self.root)}, "")), ("install_id", NEW), ("manager_pid", job),
                                ("_parent_pid", 1), ("_spawning_launcher", None)):
                stack.enter_context(mock.patch.object(cm, name, return_value=value))
            return cm._managed_here(shape, str(self.root))

    def test_the_agents_own_job_is_taken_over_on_every_local_adapter(self):
        self.assertNotEqual(holder_lock._host(), "mac.lan")
        for mod in (agy, claude, codex):
            with self.subTest(adapter=mod.COMMAND):
                proc = self.session(mod.LOCAL.name)
                self.assertIs(task_io.lock_holder(str(self.root))["live"], True)
                self.assertEqual(self.held(mod.LOCAL, job=proc.pid), "")

    def test_a_different_holder_is_still_refused(self):
        shape = agy.LOCAL
        proc = self.session(shape.name)
        self.assertIn("is not the process", self.held(shape, job=proc.pid + 1), "a session the job does not run")
        self.session(codex.LOCAL.name)
        self.assertIn(f"is held by {codex.LOCAL.name} pid", self.held(shape, job=proc.pid), "another adapter's session")
        proc = self.session(shape.name)
        proc.kill()
        proc.wait(30)
        self.assertIn("a holder with no live record", self.held(shape, job=proc.pid), "a dead session's record")

    def test_where_a_name_identifies_the_machine_another_hosts_record_is_refused(self):
        shape = agy.LOCAL
        proc = self.session(shape.name)
        with mock.patch.object(holder_lock, "_HOST_NAME_DRIFTS", False):
            self.assertIn("a holder with no live record", self.held(shape, job=proc.pid))


if __name__ == "__main__":
    unittest.main()
