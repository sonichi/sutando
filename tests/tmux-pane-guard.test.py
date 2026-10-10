#!/usr/bin/env python3
"""Contracts for the production send-guard writer, recovery and error handling."""
import io
import json
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import tmux_pane_keys as keys  # noqa: E402


class GuardContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sock = str(self.root / "s")
        self.script = self.root / "send.sh"
        self.script.write_text('''#!/bin/bash
case "$3" in
  claimed) mv "$2/ticket" "$2/ticket.claimed"; exit 124;;
  revoked) exit 124;;
  unclaimed) exit 0;;
  missing) rm "$2/ticket"; exit 0;;
esac
mv "$2/ticket" "$2/ticket.claimed"
echo SENT
echo DIAGNOSTIC >&2
exit 0
''')

    def tearDown(self):
        pending = keys.guard_path(self.sock) / "pending.json"
        try:
            ticket = Path(json.loads(pending.read_text())["ticket"])
            if ticket.parent.name.startswith("tmux-pane-keys."):
                shutil.rmtree(ticket.parent, ignore_errors=True)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        self.temp.cleanup()

    def cli(self, action, *args):
        out, err = io.BytesIO(), io.BytesIO()
        stdout = io.TextIOWrapper(out, write_through=True)
        stderr = io.TextIOWrapper(err, write_through=True)
        with mock.patch.object(sys, "argv", ["guard", action, self.sock, *args]), \
             mock.patch.object(sys, "stdout", stdout), mock.patch.object(sys, "stderr", stderr), \
             mock.patch.object(keys, "SCRIPT", self.script):
            rc = keys._main()
        return rc, out.getvalue(), err.getvalue()

    def test_success_relays_output_clears_pending_and_restores_signal_handlers(self):
        before = signal.getsignal(signal.SIGTERM)
        rc, out, err = self.cli("run", "success")
        self.assertEqual((rc, out, err), (0, b"SENT\n", b"DIAGNOSTIC\n"))
        self.assertEqual(keys.fence_status(self.sock), "clear")
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)
        self.assertFalse((keys.guard_path(self.sock) / "pending.json").exists())

    def test_revoked_timeout_can_be_retried(self):
        self.assertEqual(self.cli("run", "revoked")[0], 124)
        self.assertEqual(self.cli("run", "success")[0], 0)

    def test_missing_claim_is_failure_and_never_mistaken_for_delivery(self):
        rc, _, err = self.cli("run", "unclaimed")
        self.assertEqual(rc, 1)
        self.assertIn(b"delivery not confirmed", err)
        self.assertEqual(self.cli("run", "success")[0], 0)
        self.assertEqual(self.cli("run", "missing")[0], 125)
        self.assertEqual(self.cli("run", "success")[0], 125)

    def test_claimed_timeout_stays_fenced_until_explicit_recovery(self):
        self.assertEqual(self.cli("run", "claimed")[0], 125)
        self.assertEqual(self.cli("run", "success")[0], 125)
        self.assertEqual(keys.fence_status(self.sock), "uncertain")
        (keys.guard_path(self.sock) / "pending.json").unlink()
        self.assertEqual(keys.finish_guard(self.sock), 125)
        self.assertEqual(self.cli("recover")[0], 0)
        self.assertEqual(self.cli("run", "success")[0], 0)

    def test_active_sender_is_busy_and_cannot_be_recovered(self):
        with keys.prepare_guard(self.sock).open("ab") as mutex:
            self.assertEqual(keys.acquire_guard(self.sock, mutex.fileno()), 0)
            rc, _, err = self.cli("run", "success")
            self.assertEqual(rc, 75)
            self.assertIn(b"retry later", err)
            self.assertEqual(self.cli("recover")[0], 75)
            self.assertEqual(keys.fence_status(self.sock), "busy")

    def test_interruption_revokes_the_ticket_and_releases_the_active_lock(self):
        before = signal.getsignal(signal.SIGTERM)
        def interrupt(*_args, **_kwargs):
            signal.raise_signal(signal.SIGTERM)
        with mock.patch.object(keys.subprocess, "run", side_effect=interrupt):
            with self.assertRaises(SystemExit) as raised:
                self.cli("run", "success")
        self.assertEqual(raised.exception.code, 143)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)
        self.assertEqual(self.cli("run", "success")[0], 0)

    def test_dead_holder_ticket_is_revoked_before_new_work(self):
        keys.prepare_guard(self.sock)
        with tempfile.TemporaryDirectory(prefix="tmux-pane-keys.") as work:
            ticket = Path(work) / "ticket"
            keys.begin_guard(self.sock, ticket)
            self.assertEqual(self.cli("run", "success")[0], 0)
            self.assertFalse(ticket.exists())

    def test_lost_ticket_is_uncertain_and_not_automatically_reclaimed(self):
        keys.prepare_guard(self.sock)
        with tempfile.TemporaryDirectory(prefix="tmux-pane-keys.") as work:
            ticket = Path(work) / "ticket"
            keys.begin_guard(self.sock, ticket)
            ticket.unlink()
            self.assertEqual(self.cli("run", "success")[0], 125)
            self.assertEqual(keys.fence_status(self.sock), "uncertain")

    def test_legacy_fence_requires_explicit_recovery(self):
        keys.guard_path(self.sock).mkdir()
        self.assertEqual(self.cli("run", "success")[0], 125)
        self.assertEqual(self.cli("recover")[0], 0)
        self.assertEqual(self.cli("run", "success")[0], 0)

    def test_corrupt_or_foreign_record_fails_closed_and_recovery_does_not_follow_it(self):
        keys.prepare_guard(self.sock)
        pending = keys.guard_path(self.sock) / "pending.json"
        foreign = self.root / "user-data"
        foreign.write_text("KEEP")
        for data in ("{", "x" * 4097, "[]", json.dumps({"ticket": str(foreign)})):
            with self.subTest(data=data[:30]):
                pending.write_text(data)
                self.assertEqual(keys.fence_status(self.sock), "uncertain")
                self.assertEqual(self.cli("run", "success")[0], 125)
                self.assertEqual(self.cli("recover")[0], 0)
                self.assertEqual(foreign.read_text(), "KEEP")

    def test_publish_race_keeps_the_winners_mutex_and_removes_only_the_loser(self):
        winner = self.root / "winner"
        winner.mkdir()
        (winner / "mutex").touch()
        real_symlink = Path.symlink_to
        def publish_winner(path, _prepared):
            real_symlink(path, winner)
            raise FileExistsError
        with mock.patch.object(Path, "symlink_to", publish_winner):
            self.assertEqual(keys.prepare_guard(self.sock), keys.guard_path(self.sock) / "mutex")
        self.assertEqual(keys.guard_path(self.sock).resolve(), winner.resolve())
        self.assertEqual(list(self.root.glob("s.pane-keys-lock.*")), [])

    def test_broken_guard_and_invalid_command_fail_without_sending(self):
        keys.guard_path(self.sock).symlink_to(self.root / "missing")
        self.assertEqual(keys.fence_status(self.sock), "uncertain")
        self.assertEqual(self.cli("run", "success")[0], 125)
        self.assertEqual(self.cli("bad-command")[0], 2)


if __name__ == "__main__":
    unittest.main()
