#!/usr/bin/env python3
"""A watcher started without the routing handler while bindings.json names a
worker answers every bound room from this core, silently (2026-09-11 and
2026-09-12: two restarts, eleven and then eight owner messages answered by
the wrong seat). Nothing sets the handler durably, so the watcher arms the
provider an installed skill's manifest declares when a worker is declared, and
refuses loudly -- naming the fix, keeping an explicit opt-out -- only when no
skill provides it. An empty or core-only declaration changes nothing for the
no-worker user."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import pool_bindings_declared as pbd  # noqa: E402
import skill_manifest_capability as smc  # noqa: E402

W = "a" * 32


class TestHelper(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.state = Path(self._t.name)

    def _write(self, text):
        (self.state / "bindings.json").write_text(text)

    def test_absent_is_none(self):
        self.assertEqual(pbd.declared(self.state)[0], False)

    def test_empty_declaration_is_none(self):
        self._write(json.dumps({"bindings": {}}))
        self.assertEqual(pbd.declared(self.state)[0], False)

    def test_core_only_is_none(self):
        self._write(json.dumps({"bindings": {"!r:x": "core", "!s:x": ["core"]}}))
        self.assertEqual(pbd.declared(self.state)[0], False)

    def test_a_worker_binding_is_declared(self):
        self._write(json.dumps({"bindings": {"!r:x": W}}))
        yes, reason = pbd.declared(self.state)
        self.assertTrue(yes)
        self.assertIn("1 room(s)", reason)

    def test_unreadable_and_misshaped_fail_closed(self):
        self._write("{not json")
        self.assertTrue(pbd.declared(self.state)[0])
        self._write(json.dumps({"bindings": ["x"]}))
        self.assertTrue(pbd.declared(self.state)[0])
        self._write(json.dumps({"rooms": {}}))
        self.assertTrue(pbd.declared(self.state)[0])

    def test_cli_exit_codes(self):
        self.assertEqual(pbd.main([str(self.state)]), 0)
        self._write(json.dumps({"bindings": {"!r:x": W}}))
        self.assertEqual(pbd.main([str(self.state)]), 1)
        self.assertEqual(pbd.main([]), 2)


KEY = "SUTANDO_TASK_EVENT_HANDLER_SCRIPT"


class TestCapability(unittest.TestCase):
    """Core asks for a capability; a skill's manifest answers. No skill is named
    in core, so this is the whole coupling and it is worth pinning."""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.repo = Path(self._t.name)
        (self.repo / "skills").mkdir()
        # The private skills dir is part of the loader's scan; an inherited one
        # would make these counts depend on the host.
        for var in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR"):
            if var in os.environ:
                old = os.environ.pop(var)
                self.addCleanup(os.environ.__setitem__, var, old)

    def _skill(self, name, command="./run.py", make=True, executable=True, **extra):
        d = self.repo / "skills" / name
        (d / "scripts").mkdir(parents=True, exist_ok=True)
        if make:
            t = d / command.lstrip("./")
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text("#!/bin/sh\nexit 0\n")
            if executable:
                t.chmod(0o755)
        (d / "manifest.json").write_text(json.dumps(
            {"name": name, "version": "1.0.0", "owner": "t", "stability": "experimental",
             "config": {KEY: command}, **extra}))
        return d

    def test_no_skill_provides_it(self):
        self._skill("other", command="./run.py")
        (self.repo / "skills" / "other" / "manifest.json").write_text(json.dumps(
            {"name": "other", "version": "1.0.0", "owner": "t", "stability": "experimental"}))
        target, reason = smc.resolve(self.repo, KEY)
        self.assertIsNone(target)
        self.assertIn("no installed skill provides", reason)

    def test_one_provider_resolves_inside_its_own_skill(self):
        d = self._skill("provider")
        target, reason = smc.resolve(self.repo, KEY)
        # .resolve(): the containment check resolves, and /var is a symlink here.
        self.assertEqual(target, (d / "run.py").resolve())
        self.assertIn("provider", reason)

    def test_two_providers_are_ambiguity_not_a_choice(self):
        self._skill("one")
        self._skill("two")
        target, reason = smc.resolve(self.repo, KEY)
        self.assertIsNone(target)
        self.assertIn("refusing to choose", reason)

    def test_a_path_escaping_the_skill_is_not_a_provider(self):
        self._skill("escaper", command="../../bin/sh", make=False)
        self.assertIsNone(smc.resolve(self.repo, KEY)[0])
        self._skill("absolute", command="/bin/sh", make=False)
        self.assertIsNone(smc.resolve(self.repo, KEY)[0])

    def test_a_non_executable_declaration_is_not_a_provider(self):
        self._skill("limp", executable=False)
        self.assertIsNone(smc.resolve(self.repo, KEY)[0])

    def test_a_disabled_skill_provides_nothing(self):
        self._skill("off", enabled=False)
        self.assertIsNone(smc.resolve(self.repo, KEY)[0])

    def test_the_shipped_manifest_provides_it(self):
        # The real repo, so a manifest rename or a lost execute bit fails here
        # rather than only in the watcher case below.
        target, reason = smc.resolve(REPO, KEY)
        self.assertIsNotNone(target, reason)
        self.assertTrue(os.access(target, os.X_OK))

    def test_cli_exit_codes(self):
        self.assertEqual(smc.main([str(self.repo), KEY]), 1)
        self._skill("provider")
        self.assertEqual(smc.main([str(self.repo), KEY]), 0)
        self.assertEqual(smc.main([str(self.repo)]), 2)


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
        # Seeded before every start: the gate runs ahead of the sweep, so this
        # changes nothing for a refusal and gives a start something to announce.
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
        env = {k: v for k, v in os.environ.items()
               if k not in ("SUTANDO_TASK_EVENT_HANDLER", "SUTANDO_ALLOW_UNROUTED_BINDINGS",
                            "SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR")}
        env.update({"TMPDIR": str(self.tmp), "SUTANDO_WORKSPACE_DIR": str(self.ws)})
        env.update(env_extra)
        root = self._bare_checkout() if checkout == "bare" else REPO
        inbox = Path(inbox) if inbox else (self.ws / "tasks")
        # Files, not pipes: a started watcher never exits, so its output has to be
        # readable while it still runs, and a refusal's stderr after it is gone.
        self.out = self.tmp / f"out{len(self.procs)}"
        self.err = self.tmp / f"err{len(self.procs)}"
        with open(self.out, "w") as o, open(self.err, "w") as e:
            p = subprocess.Popen(["bash", f"{root}/src/watch-tasks-stream.sh", str(inbox)],
                                 cwd=str(root), env=env, stdout=o, stderr=e,
                                 text=True, start_new_session=True)
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
        self.assertIn("SUTANDO_TASK_EVENT_HANDLER_SCRIPT", err)
        self.assertIn("SUTANDO_ALLOW_UNROUTED_BINDINGS=1", err)
        return err

    def _assert_started(self, p):
        # Proven by the work the gate lets through, not by still being alive:
        # liveness also needs fswatch, which CI lacks. The sweep precedes it.
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
    def test_bindings_and_no_handler_refuse_and_name_the_fix(self):
        # `checkout="bare"` in every refusal case: with a provider installed a
        # declared worker is ROUTED, so refusal needs a tree that provides none.
        self._bind()
        err = self._assert_refused(self._start(checkout="bare"))
        self.assertIn("is unset", err)

    def test_a_non_executable_handler_is_named_as_such(self):
        # Run from the INSTALLED checkout: a caller who named a handler and got
        # it wrong is told so, never silently replaced by the provider.
        self._bind()
        stub = self.ws / "handler.py"
        stub.write_text("#!/bin/sh\nexit 3\n")   # not chmod +x
        err = self._assert_refused(
            self._start(SUTANDO_TASK_EVENT_HANDLER=str(stub)))
        self.assertIn("not executable", err)

    def test_a_corrupt_declaration_refuses_too(self):
        (self.ws / "state" / "bindings.json").write_text("{not json")
        self._assert_refused(self._start(checkout="bare"))

    def test_no_declaration_starts(self):
        self._assert_started(self._start())

    def test_core_only_declaration_starts(self):
        self._bind("core")
        self._assert_started(self._start())

    def test_the_opt_out_starts_unrouted_on_purpose(self):
        self._bind()
        self._assert_started(self._start(SUTANDO_ALLOW_UNROUTED_BINDINGS="1"))

    def test_an_executable_handler_starts(self):
        self._bind()
        stub = self.ws / "handler.sh"
        stub.write_text("#!/bin/sh\nexit 3\n")
        stub.chmod(0o755)
        self._assert_started(self._start(SUTANDO_TASK_EVENT_HANDLER=str(stub)))

    def _delivery_inbox(self):
        """A worker's inbox: <ws>/deliveries/<id>, seeded so a start can announce."""
        d = self.ws / "deliveries" / W
        d.mkdir(parents=True)
        (d / "task-seed1.txt").write_text("id: task-seed1\ntask: seed\n")
        return d

    def test_a_worker_delivery_watcher_starts_under_the_same_declaration(self):
        # A worker carries no routing handler, so a healthy multi-worker host
        # must neither trip this gate nor be handed the core's router.
        self._bind()
        p = self._start(inbox=self._delivery_inbox())
        self._assert_started(p)
        self.assertNotIn("routing through", p._err.read_text())

    def test_an_unresolvable_core_inbox_refuses_rather_than_reading_as_worker(self):
        # "Cannot tell" must not share a branch with "definitely a worker".
        elsewhere = Path(self._t.name) / "no-tasks-ws"
        (elsewhere / "state").mkdir(parents=True)
        # Under the DECLARED workspace, or an absent declaration decides the start.
        (elsewhere / "state" / "bindings.json").write_text(
            json.dumps({"bindings": {"!r:x": W}}))
        self.assertFalse((elsewhere / "tasks").exists())
        self._assert_refused(self._start(checkout="bare",
                                         SUTANDO_WORKSPACE_DIR=str(elsewhere)))

    def test_the_core_intake_still_refuses_under_that_same_setup(self):
        # Positive control: identical declaration and env, only the inbox differs,
        # or the start above is satisfied by a gate that fires nowhere.
        self._bind()
        self._delivery_inbox()
        err = self._assert_refused(self._start(checkout="bare"))
        self.assertIn("is unset", err)


class TestDefaultedHandler(WatcherHarness, unittest.TestCase):
    """Nothing sets SUTANDO_TASK_EVENT_HANDLER durably -- not `src/startup.sh`,
    not `src/restart.sh`, and the documented Claude-core arming path passes the
    watcher a bare command with no env. So the refusal above, on its own, makes
    a bound host arm no watcher at all. The default is what makes the refusal
    the exception (a genuinely missing router) rather than the normal path.

    It is keyed on the declaration, per Chi Wang 2026-09-17: a user who does not
    use workers does not need the variable wired; after a worker is created, it
    becomes needed.
    """

    ROOM = "!bound:example.test"

    def _roster(self, target=W):
        """The COMPILED roster the router delivers against -- a different file
        from the owner-authored bindings.json the gate reads."""
        (self.ws / "results" / "archive").mkdir(parents=True, exist_ok=True)
        (self.ws / "state" / "roster.json").write_text(json.dumps(
            {"version": 1, "workers": {target: {"state": "live"}},
             "bindings": {self.ROOM: target}}))

    def _bound_task(self, name="task-bound"):
        (self.ws / "tasks" / f"{name}.txt").write_text(
            f"id: {name}\nchannel_id: {self.ROOM}\nsource: ag2space\n"
            "access_tier: owner\ntask: body\n")
        # The unbound seed would be emitted either way; drop it so stdout speaks
        # only about the bound task.
        (self.ws / "tasks" / "task-seed1.txt").unlink()

    def _fswatch_path(self):
        """A stub fswatch so the sweep is not racing the real one's absence."""
        b = self.tmp / "bin"
        b.mkdir(exist_ok=True)
        feed = self.tmp / "feed"
        feed.write_text("")
        (b / "fswatch").write_text(f"#!/bin/sh\nexec tail -n +1 -f {feed}\n")
        (b / "fswatch").chmod(0o755)
        return f"{b}:{os.environ['PATH']}"

    def _delivered(self):
        d = self.ws / "deliveries"
        return sorted(str(q.relative_to(self.ws)) for q in d.rglob("*")
                      if q.is_file()) if d.exists() else []

    def _settle(self, p, want_delivery):
        deadline = time.time() + 30
        while time.time() < deadline:
            if want_delivery and self._delivered():
                return
            if not want_delivery and "TASK_FILE:" in p._out.read_text():
                return
            if p.poll() is not None:
                break
            time.sleep(0.3)
        self.fail(f"never settled (rc={p.poll()}) delivered={self._delivered()} "
                  f"out={p._out.read_text()[-300:]} err={p._err.read_text()[-600:]}")

    def test_a_declared_worker_defaults_the_handler_and_the_task_is_delivered(self):
        self._bind()
        self._roster()
        self._bound_task()
        p = self._start(PATH=self._fswatch_path())
        self._settle(p, want_delivery=True)
        err = p._err.read_text()
        self.assertIn("routing through", err)
        self.assertIn("SUTANDO_TASK_EVENT_HANDLER_SCRIPT", err)
        self.assertNotIn("REFUSING to start", err)
        # The point of the default: the bound task goes to the worker's folder
        # instead of being answered by this core.
        self.assertTrue(any(W in d for d in self._delivered()), self._delivered())
        self.assertNotIn("task-bound", p._out.read_text())

    def test_no_worker_declared_leaves_the_handler_unset(self):
        # Chi's no-worker user. The roster names a worker so a fire here WOULD
        # show as a delivery; the declaration the gate reads names none.
        self._bind("core")
        self._roster()
        self._bound_task()
        p = self._start(PATH=self._fswatch_path())
        self._settle(p, want_delivery=False)
        self.assertNotIn("routing through", p._err.read_text())
        self.assertEqual(self._delivered(), [])
        self.assertIn("task-bound", p._out.read_text())

    def test_the_callers_handler_is_never_overridden(self):
        self._bind()
        self._roster()
        self._bound_task()
        stub = self.ws / "handler.sh"
        stub.write_text(f"#!/bin/sh\n: > {self.ws}/stub-ran\nexit 3\n")
        stub.chmod(0o755)
        p = self._start(SUTANDO_TASK_EVENT_HANDLER=str(stub),
                        PATH=self._fswatch_path())
        self._settle(p, want_delivery=False)
        self.assertNotIn("routing through", p._err.read_text())
        self.assertTrue((self.ws / "stub-ran").exists(), "the caller's handler never ran")
        self.assertEqual(self._delivered(), [])

    def test_a_declared_worker_with_no_provider_installed_still_refuses(self):
        # The case the guard must still catch: core must not invent a path when
        # the declaration names a worker and nothing provides the capability.
        self._bind()
        err = self._assert_refused(self._start(checkout="bare"))
        self.assertIn("no installed skill provides", err)


if __name__ == "__main__":
    unittest.main()
