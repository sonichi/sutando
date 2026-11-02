"""The undo of a start at its exact seams, for both managers (systemd, and launchd's real branch with
its shim), mixed into each adapter's ConnectCode suite. A suite provides `local` (its shape), `seam_fake()`
(its bridge-routing manager fake), `connect(backend, fake)`, `backend()`, `install_seat(fake)` (install
--token-env-file over a seeded seat), `agent_lock_holder()` (another operation's agent lock), `root`, `home`.
"""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from unittest import mock

from ag2_sparrow import connect_bridge as cb, connect_common as cm, task_io

MANAGERS = (("systemd", False), ("launchd", True))
STOPS = {"stop", "bootout", "disable"}
STARTS = {"restart", "start", "bootstrap"}


def after_start(fake) -> set:
    """The manager verbs from this attempt's start on (install stops a job of its own before it starts)."""
    verbs = [fake.runner.verb(c) for c in fake.runner.calls]
    first = next((i for i, v in enumerate(verbs) if v in STARTS), len(verbs))
    return set(verbs[first:])


def _until(predicate, seconds=15.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.05)
    return predicate()


class UndoSeams:

    def _real(self, mac, tag=""):
        fake = self.seam_fake()
        fake.runner.real, fake.runner.shim = True, mac
        fake.runner.pause, fake.runner.acted = "", str(self.home / f"acted-{tag}-{int(mac)}")
        return fake

    def _done(self, fake):
        """The test's own cleanup: every run it spawned gone, so one manager cannot leak into the next."""
        r = fake.runner
        for p in r.spawned:
            try:
                os.killpg(p.pid, signal.SIGKILL)    # the group too: a shim's child can outlive the shim
            except (ProcessLookupError, PermissionError):
                pass
            if p.poll() is None:
                p.kill()
            p.wait(timeout=30)
        r.session = None

    def _reaped(self, fake):
        """The old run, exited after a signal, reaped as its manager would, before the manager starts another."""
        old, fake.runner.session = fake.runner.session, None
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
                            self.assertIn("which is not provably this operation's, so that run was left", err)
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
                            self._reaped(fake)
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
                            self.assertIn("runs again as pid", err)
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
                    verbs = [fake.runner.verb(c) for c in fake.runner.calls]
                    try:
                        self.assertTrue(rc or refused, out + err)
                        self.assertEqual(signalled.call_args_list[0].args[2], signal.SIGTERM)
                        self.assertTrue(all(p.poll() is not None for p in fake.runner.spawned), "its run is gone")
                        self.assertIn("bootout" if mac else "stop", verbs, "and the manager will not respawn it")
                        self.assertFalse(Path(fake.runner.acted).exists(), "it never acted on the root")
                    finally:
                        self._done(fake)

    def test_a_launchd_pid_shown_late_after_our_bootstrap_is_still_ours(self):
        fake = self._real(True, "late")
        r, blank, real = fake.runner, [3], fake.runner.answer

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
            self.assertEqual(blank[0], 0, "the first reads showed no pid")
            self.assertIn("the job its install started, was stopped", err)
            self.assertTrue(all(p.poll() is not None for p in r.spawned), "its run is gone")
        finally:
            self._done(fake)
