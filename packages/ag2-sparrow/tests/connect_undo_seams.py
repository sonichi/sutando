"""The undo of a start at its exact seams, for both managers (systemd, and launchd's real branch with
its shim), mixed into each adapter's ConnectCode suite. A suite provides `local` (its shape), `seam_fake()`
(its bridge-routing manager fake), `connect(backend, fake)`, `backend()`, `install_seat(fake)` (install
--token-env-file over a seeded seat), `agent_lock_holder()` (another operation's agent lock), `root`, `home`.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import signal
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from _helpers.hermetic_env import isolate_os_environ  # noqa: E402

isolate_os_environ()

from ag2_sparrow import claude_local_records as records, connect_bridge as cb, connect_common as cm  # noqa: E402
from ag2_sparrow import service_manager as sm, task_io  # noqa: E402

MANAGERS = (("systemd", False), ("launchd", True))
FOREIGN_PLIST = b"<?xml version='1.0'?>\n<!-- another writer's plist -->\n<plist><dict/></plist>\n"
STOPS = {"stop", "bootout", "disable"}
STARTS = {"restart", "start", "bootstrap"}


def after_start(fake) -> set:
    """The manager verbs from this attempt's start on (install stops a job of its own before it starts)."""
    verbs = [fake.runner.verb(c) for c in fake.runner.calls]
    first = next((i for i, v in enumerate(verbs) if v in STARTS), len(verbs))
    return set(verbs[first:])


def bridge_verbs(fake) -> set:
    return {c[1] if c[0] == "launchctl" else next(a for a in c[1:] if not a.startswith("--")) for c in fake.calls}


def cb_cfg(local):
    """The config the install wrote, for unwiring its bridge in a test's own cleanup ({} when there is none)."""
    try:
        return cm.read_config(cm.config_path(local))
    except OSError:
        return {}


def _until(predicate, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.05)
    return predicate()


class UndoSeams:
    tmux_tree = False       # a suite whose launcher hosts the holder in a tmux pane runs that real tree

    def _real(self, mac, tag=""):
        fake = self.seam_fake()
        fake.runner.real, fake.runner.shim = True, mac
        fake.runner.pause, fake.runner.acted = "", str(self.home / f"acted-{tag}-{int(mac)}")
        fake.runner.keep_alive, fake.runner.throttle = True, 0.0    # a run that ends is restarted at once
        fake.runner.tmux = self.tmux_tree
        return fake

    def _done(self, fake):
        """The test's own cleanup: every run it spawned gone, so one manager cannot leak into the next."""
        r = fake.runner
        gen = records.load_supervisor(str(self.root)).get("generation") or {}
        if gen.get("server_pid") and task_io.verify_lineage(gen["server_pid"], gen.get("server_start")) is True:
            os.killpg(gen["server_pid"], signal.SIGKILL)       # a tree's server is no descendant of its launcher
        for p in r.spawned:
            try:
                os.killpg(p.pid, signal.SIGKILL)    # the group too: a shim's child can outlive the shim
            except (ProcessLookupError, PermissionError):
                pass
            if p.poll() is None:
                p.kill()
            p.wait(timeout=30)
        r.session = None

    def _reaped(self, fake, sig=None):
        """The old run, exited after a signal (repeated: one sent before its handler is lost), reaped as its
        manager would, before the manager starts another."""
        old, fake.runner.session = fake.runner.session, None
        deadline = time.monotonic() + 30
        while old.poll() is None and sig is not None and time.monotonic() < deadline:
            os.kill(old.pid, sig)
            time.sleep(0.2)
        old.wait(timeout=30)
        for pipe in (old.stdin, old.stdout):
            pipe.close()

    def _fail(self, how, fake, refused):
        """`how` fails after its start: connect's bridge refused, or install's bridge write raising."""
        if how == "connect":
            with mock.patch.object(cb, "wire_bridge", return_value="the bridge was refused"):
                return self.connect(self.backend(), fake)
        with mock.patch.object(cb, "wire_bridge", side_effect=OSError("bridge write failed")):
            try:
                self.install_seat(fake)
            except OSError:
                refused.append("raised")
        return None, "", ""

    def test_seam_an_external_start_between_the_probe_and_the_start_call_is_never_ours(self):
        for name, mac in MANAGERS:
            with self.subTest(name):
                fake, real, external = self._real(mac, "probe"), cm.start_supervisor, []

                def external_first(shape, *a, **kw):
                    if shape.name == self.local.name and not external:
                        fake.runner.start_session(by_manager=True)  # after the probe, before this start
                        external.append(fake.runner.session)
                    return real(shape, *a, **kw)
                with mock.patch.object(cm, "is_macos", return_value=mac), \
                        mock.patch.object(cm, "start_supervisor", side_effect=external_first), \
                        mock.patch.object(cm, "_signal_exact", wraps=cm._signal_exact) as signalled:
                    rc, out, err = self._fail("connect", fake, [])
                verbs = after_start(fake)
                try:
                    self.assertNotEqual(rc, 0, out + err)
                    self.assertIn("was already running when this operation asked for it, so it is not this "
                                  "operation's", err)
                    self.assertIsNone(external[0].poll(), "the external run is alive")
                    self.assertIs(fake.runner.session, external[0], "and still the manager's")
                    self.assertFalse(STOPS & verbs, "nothing stopped it by name")
                    signalled.assert_not_called()
                finally:
                    self._done(fake)

    def test_seam_busy_cleanup_never_acts_on_a_holder_snapshot(self):
        for how in ("connect", "install"):
            for name, mac in MANAGERS:
                with self.subTest(how=how, manager=name):
                    fake = self._real(mac, f"busy-{how}")
                    r = fake.runner
                    r.pause = str(self.home / f"resume-{how}-{int(mac)}")   # the launcher waits before the root
                    real_role, other = cm.lock_holder_role, []

                    def snapshot_then_changed(root):
                        rec = real_role(root)
                        if not other:
                            Path(r.pause).touch()   # the launcher takes the root and lets the agent lock go
                            _until(lambda: task_io.lock_held(str(self.root)) == "held"
                                   and not cm.agent_lock_busy(str(self.root)))
                            other.append(self.agent_lock_holder())
                        return rec
                    with mock.patch.object(cm, "is_macos", return_value=mac), \
                            mock.patch.object(cm, "AGENT_LOCK_WAIT_S", 0.5), \
                            mock.patch.object(cm, "lock_holder_role", side_effect=snapshot_then_changed), \
                            mock.patch.object(cm, "_signal_exact", wraps=cm._signal_exact) as signalled:
                        job = []
                        if how == "connect":
                            rc, out, err = self.connect(self.backend(), fake)
                        else:
                            rc, out, err = self.install_seat(fake)
                        job.append(r.pid())
                    verbs = after_start(fake)
                    try:
                        self.assertTrue(other, "the holder changed after the snapshot")
                        self.assertIsNone(other[0].poll(), "another operation holds the agent lock")
                        self.assertNotEqual(rc, 0, out + err)
                        self.assertIn("so nothing was stopped", err)
                        self.assertFalse(STOPS & verbs, "nothing was stopped under another operation")
                        signalled.assert_not_called()
                        self.assertIsNotNone(job[0], "the job is left, as reported")
                        self.assertTrue(r.alive())
                    finally:
                        for holder in other:
                            holder.stdin.close()
                            holder.wait(timeout=30)
                        self._done(fake)

    def test_seam_a_successor_between_the_identity_check_and_the_signal_is_never_signalled(self):
        for how in ("connect", "install"):
            for name, mac in MANAGERS:
                with self.subTest(how=how, manager=name):
                    fake, real, successor = self._real(mac, f"check-{how}"), cm._signal_exact, []

                    def replaced_first(pid, start, sig):
                        if not successor:
                            fake.runner.start_session(by_manager=True)   # the manager replaced the run
                            successor.append(fake.runner.session)
                        return real(pid, start, sig)
                    refused = []
                    with mock.patch.object(cm, "is_macos", return_value=mac), \
                            mock.patch.object(cm, "_signal_exact", side_effect=replaced_first):
                        rc, out, err = self._fail(how, fake, refused)
                    verbs = after_start(fake)
                    try:
                        self.assertTrue(rc or refused, out + err)
                        self.assertTrue(successor, "the undo reached the signal")
                        self.assertIsNone(successor[0].poll(), "the successor is alive")
                        self.assertIs(fake.runner.session, successor[0])
                        self.assertFalse(STOPS & set(verbs), "nothing stopped the job by name")
                        if how == "connect":
                            self.assertIn("has exited; nothing was signalled", err)
                    finally:
                        self._done(fake)

    def test_seam_a_successor_after_the_signal_is_never_stopped(self):
        for how in ("connect", "install"):
            for name, mac in MANAGERS:
                with self.subTest(how=how, manager=name):
                    fake, real, successor = self._real(mac, f"after-{how}"), cm._signal_exact, []

                    def signalled_then_replaced(pid, start, sig):
                        got = real(pid, start, sig)
                        if not successor:
                            self._reaped(fake, sig)
                            fake.runner.start_session(by_manager=True)   # a run it did not start, at once
                            successor.append(fake.runner.session)
                        return got
                    refused = []
                    with mock.patch.object(cm, "is_macos", return_value=mac), \
                            mock.patch.object(cm, "_signal_exact", side_effect=signalled_then_replaced):
                        rc, out, err = self._fail(how, fake, refused)
                    verbs = after_start(fake)
                    try:
                        self.assertTrue(rc or refused, out + err)
                        self.assertTrue(successor)
                        self.assertIsNone(successor[0].poll(), "the successor is alive")
                        self.assertFalse(STOPS & set(verbs), "the job was not stopped by name over it")
                        if how == "connect":
                            self.assertIn("retired, but the job runs again as pid", err)
                    finally:
                        self._done(fake)

    def test_seam_a_successor_at_the_final_manager_stop_is_never_stopped(self):
        for how in ("connect", "install"):
            for name, mac in MANAGERS:
                with self.subTest(how=how, manager=name):
                    fake, real, successor, ours = self._real(mac, f"final-{how}"), cm.stop_supervisor, [], []

                    def replaced_first(shape, *a, **kw):
                        started = STARTS & {fake.runner.verb(c) for c in fake.runner.calls}
                        if shape.name == self.local.name and started and not successor:
                            ours.append(fake.runner.session)
                            fake.runner.start_session(by_manager=True)  # another actor's start, past the lock
                            successor.append(fake.runner.session)
                        return real(shape, *a, **kw)
                    refused = []
                    with mock.patch.object(cm, "is_macos", return_value=mac), \
                            mock.patch.object(cm, "stop_supervisor", side_effect=replaced_first):
                        rc, out, err = self._fail(how, fake, refused)
                    try:
                        self.assertTrue(rc or refused, out + err)
                        for run in successor:
                            self.assertIsNone(run.poll(), "a start this operation did not make is never stopped")
                        self.assertTrue(all(p.poll() is not None for p in fake.runner.spawned if p not in successor),
                                        "this operation's own run is gone")
                    finally:
                        self._done(fake)

    def test_the_undo_stops_exactly_its_own_run_on_both_managers(self):
        for how in ("connect", "install"):
            for name, mac in MANAGERS:
                with self.subTest(how=how, manager=name):
                    fake = self._real(mac, f"own-{how}")
                    refused = []
                    with mock.patch.object(cm, "is_macos", return_value=mac), \
                            mock.patch.object(cm, "_signal_exact", wraps=cm._signal_exact) as signalled:
                        rc, out, err = self._fail(how, fake, refused)
                    verbs = after_start(fake)
                    try:
                        self.assertTrue(rc or refused, out + err)
                        self.assertEqual({c.args[2] for c in signalled.call_args_list}, {cm.RETIRE_SIGNAL})
                        self.assertTrue(all(p.poll() is not None for p in fake.runner.spawned), "its run is gone")
                        self.assertEqual(fake.runner.respawns, 0, "and retired: its manager does not start it again")
                        self.assertFalse(STOPS & set(verbs), "no step named the job to its manager")
                        self.assertFalse(Path(fake.runner.acted).exists(), "it never acted on the root")
                    finally:
                        self._done(fake)

    def test_after_settling_stop_started_stops_its_own_run_and_leaves_a_changed_one(self):
        for changed in (False, True):
            for name, mac in MANAGERS:
                with self.subTest(manager=name, changed=changed), mock.patch.dict(cm._STARTED, clear=True):
                    fake, successor = self._real(mac, f"settled-{int(changed)}"), []
                    try:
                        with mock.patch.object(cm, "is_macos", return_value=mac):
                            rc, out, err = self.connect(self.backend(), fake)
                            self.assertEqual(rc, 0, out + err)
                            self.assertIn(self.local.name, cm._STARTED, "the settled start is recorded")
                            ours, before = fake.runner.session, len(fake.runner.calls)
                            if changed:
                                fake.runner.start_session(by_manager=True)   # the manager's run is no longer ours
                                successor.append(fake.runner.session)
                            with cm.agent_lock(str(self.root)), contextlib.redirect_stdout(io.StringIO()), \
                                    mock.patch.object(sm, "_run", side_effect=fake):
                                line = cm.stop_started(self.local, str(self.root))
                        verbs = {fake.runner.verb(c) for c in fake.runner.calls[before:]}
                        if changed:
                            self.assertIn("is no longer the job this connect started", line)
                            self.assertIsNone(successor[0].poll(), "the changed run is left")
                            self.assertFalse(STOPS & verbs)
                        else:
                            self.assertEqual(line, "")
                            self.assertIsNotNone(ours.wait(timeout=30), "its own run retired")
                            self.assertEqual(fake.runner.respawns, 0)
                            self.assertFalse(STOPS & verbs, "no step named the job to its manager")
                            self.assertNotIn(self.local.name, cm._STARTED)
                    finally:
                        self._done(fake)
                        with mock.patch.object(cm, "is_macos", return_value=mac), mock.patch.object(sm, "_run", side_effect=fake), \
                                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                            cm.uninstall(self.local)

    def test_a_run_that_ended_on_its_own_and_was_restarted_is_left_with_the_command(self):
        for how in ("connect", "install"):
            for name, mac in MANAGERS:
                for due in (True, False):
                    with self.subTest(how=how, manager=name, restart_due=due):
                        fake, ended = self._real(mac, f"ended-{how}-{int(due)}"), []
                        r = fake.runner
                        r.throttle = 0.0 if due else 600.0

                        def run_ended_then_refused(*a, **kw):
                            ended.append(r.session.pid)
                            os.kill(r.session.pid, signal.SIGTERM)  # the run ends; its manager answers per policy
                            r.session.wait(timeout=30)
                            if how == "install":
                                raise OSError("bridge write failed")
                            return "the bridge was refused"
                        with mock.patch.object(cm, "is_macos", return_value=mac), \
                                mock.patch.object(cb, "wire_bridge", side_effect=run_ended_then_refused), \
                                mock.patch.object(cm, "_signal_exact", wraps=cm._signal_exact) as signalled:
                            try:
                                rc, out, err = self.connect(self.backend(), fake) if how == "connect" else \
                                    self.install_seat(fake)
                            except OSError:
                                rc, out, err = 1, "", ""
                            by_hand = cm.stop_command(self.local)
                        try:
                            self.assertTrue(ended, "the run ended before the undo")
                            self.assertEqual(r.respawns, 1 if due else 0, "restarted only once its policy allows")
                            self.assertNotEqual(rc, 0, out + err)
                            if how == "connect":
                                self.assertIn("is no longer the job this connect started", err)
                                self.assertIn(by_hand, err, "what is left comes with the command that stops it")
                            self.assertEqual(r.pid() is not None, due, "a restarted run is left, never stopped by name")
                            self.assertFalse(STOPS & after_start(fake))
                            signalled.assert_not_called()
                        finally:
                            self._done(fake)

    def test_a_launchd_registration_whose_plist_is_not_ours_is_left(self):
        for how in ("connect", "install"):
            with self.subTest(how):
                fake = self._real(True, f"foreign-{how}")

                def replaced_then_refused(*a, **kw):
                    cm.supervisor_path(self.local).write_bytes(FOREIGN_PLIST)   # another writer's plist now
                    if how == "install":
                        raise OSError("bridge write failed")
                    return "the bridge was refused"
                with mock.patch.object(cm, "is_macos", return_value=True), \
                        mock.patch.object(cb, "wire_bridge", side_effect=replaced_then_refused):
                    try:
                        rc, out, err = self.connect(self.backend(), fake) if how == "connect" else self.install_seat(fake)
                    except OSError:
                        rc, out, err = 1, "", ""
                try:
                    self.assertNotEqual(rc, 0, out + err)
                    if how == "connect":
                        self.assertIn("is no longer the job this connect started", err)
                    self.assertIsNotNone(fake.runner.pid(), "the job is left, as reported")
                    self.assertTrue(fake.runner.loaded)
                    self.assertFalse(STOPS & after_start(fake), "nothing was booted out")
                finally:
                    self._done(fake)
                    cm.plist_path(self.local).unlink(missing_ok=True)

    def test_a_failed_install_reports_the_bridge_it_wired_and_never_stops_it_by_name(self):
        bridge = cb.bridge_shape(self.local)
        for changed in (False, True):
            for name, mac in MANAGERS:
                with self.subTest(manager=name, bridge_changed=changed):
                    fake = self._real(mac, f"bridge-{int(changed)}")
                    real_problem = cm._holder_problem

                    def refused_after_the_wiring(shape, root, rec, p):
                        if changed:
                            cm.supervisor_path(bridge).write_text(cm.supervisor_path(bridge).read_text() + "\n")
                        return real_problem(shape, root, rec, p) or "the launcher was refused (test)"
                    with mock.patch.object(cm, "is_macos", return_value=mac), \
                            mock.patch.object(cm, "_holder_problem", side_effect=refused_after_the_wiring):
                        rc, out, err = self.install_seat(fake)
                        wired, bridge_job, cm_stop = (cm.supervisor_path(bridge).exists(), cm.job_name(bridge),
                                                      cm.stop_command(bridge))
                    try:
                        self.assertNotEqual(rc, 0, out + err)
                        self.assertTrue(all(p.poll() is not None for p in fake.runner.spawned), "the session job stopped")
                        self.assertTrue(wired, "the manager stops a bridge job only by name, so it is left")
                        self.assertTrue(fake.active, "and still running")
                        self.assertFalse({"stop", "bootout"} & bridge_verbs(fake), "nothing named the bridge to its manager")
                        if changed:
                            self.assertIn(f"{bridge_job} is no longer the unit this attempt wrote, so it was left", err)
                        else:
                            self.assertIn(f"the bridge this attempt wired ({bridge_job}) is active and was left", err)
                            self.assertIn(cm_stop, err, "with the command that stops it")
                    finally:
                        self._done(fake)
                        with mock.patch.object(cm, "is_macos", return_value=mac), mock.patch.object(sm, "_run", side_effect=fake), \
                                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                            cb.unwire_bridge(cb_cfg(self.local), self.local.command, bridge)
                            cm.uninstall(self.local)

    def test_verify_fails_when_the_session_job_is_gone_but_a_bridge_is_configured(self):
        for name, mac in MANAGERS:
            with self.subTest(name):
                fake = self.seam_fake()
                try:
                    with mock.patch.object(cm, "is_macos", return_value=mac):
                        rc, out, err = self.connect(self.backend(), fake)
                        self.assertEqual(rc, 0, out + err)
                        with mock.patch.object(sm, "_run", side_effect=fake):
                            served = self.main("verify")[1]
                            fake.runner.stop_session()
                            rc, gone, _ = self.main("verify")
                            job = cm.job_name(self.local)
                    self.assertIn(f"PASS job: {job} active", served)
                    self.assertNotEqual(rc, 0, gone)
                    self.assertRegex(gone, rf"FAIL job: {re.escape(job)} is (absent|registered), so no session answers the "
                                           "tasks its configured bridge passes on")
                finally:
                    self._done(fake)
                    with mock.patch.object(cm, "is_macos", return_value=mac), mock.patch.object(sm, "_run", side_effect=fake), \
                            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        cm.uninstall(self.local)

    def test_a_launchd_job_that_shows_no_pid_is_left_with_the_command(self):
        fake = self._real(True, "late")
        r, blank, real = fake.runner, [10 ** 6], fake.runner.answer     # launchd never shows its pid

        def late_pid(verb, cmd):
            if verb == "print" and r.alive() and blank[0] > 0:
                blank[0] -= 1
                return (0, "\tstate = spawn scheduled\n", "")
            return real(verb, cmd)
        r.answer = late_pid
        with mock.patch.object(cm, "is_macos", return_value=True):
            rc, out, err = self._fail("connect", fake, [])
        try:
            self.assertNotEqual(rc, 0, out + err)
            self.assertLess(blank[0], 10 ** 6, "the start read the job while it showed no pid")
            self.assertIn("showed no process within 5 s of this operation's start, so it is not this operation's", err)
            self.assertFalse(STOPS & after_start(fake), "nothing named the job to its manager")
        finally:
            self._done(fake)
