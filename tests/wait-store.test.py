#!/usr/bin/env python3
"""The shared wait store (src/wait_store.py) and its two users.

- The contract: a claim is one rename with exactly one winner; a release puts the wait back; a held
  lock means a live waiter; the resume task is owner-tier, task-last and addressed to the wait's room;
  only a live owner-tier, non-collaborator task of the named source may arm a wait.
- Publication: a claim that owes a resume task is stamped once the task is out; a claim left
  without its stamp (a waiter stopped between the claim and the task) is published exactly once
  by `recover`, and never when a task of that id is already pending or archived.
- The claim: the claimed record names its outcome from the instant it exists; a claimer stopped
  between taking the wait and placing that record is finished by `recover` with the outcome it
  staged; a claim that names no outcome is closed as the non-success outcome its skill names,
  never dropped or turned into a success.
- No replacing: every install, stamp, un-claim and adoption of a claimed record happens under
  the store's mutex and only where no claimed record exists, so a record a claimer installed
  survives a recovery of the same wait, and a taken file left beside it is removed.
- Delegation: connect-apps and connect-mcp both reach the store's claim, release, lock, resume-task
  writer, owner check and detached spawn, so neither carries a copy of them.

Nothing here starts a process or writes outside a temporary workspace.

Run: python3 tests/wait-store.test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ(SUTANDO_SUPPRESS_CCD_FALLBACK_BANNER="1")

import ast
import contextlib
import fcntl
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import local_task_protocol as ltp  # noqa: E402
import wait_store  # noqa: E402


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


CONNECTORS_PATH = ROOT / "skills" / "connect-apps" / "scripts" / "connectors.py"
CONNECT_MCP_PATH = ROOT / "skills" / "connect-mcp" / "scripts" / "connect_mcp.py"
connectors = load("connectors_store", CONNECTORS_PATH)
connect_mcp = load("connect_mcp_store", CONNECT_MCP_PATH)

ROOM = "!dm:example.org"
OWNER = "@owner:example.org"
WAIT_ID = "1789000000000-0000abcd"
NOW = 1_789_000_000.0


class Stopped(BaseException):
    """The process ending mid-claim; not an Exception, so no handler in the store swallows it."""


@contextlib.contextmanager
def stopping_at_rename(n):
    """Let n-1 renames through, then stop the process at the nth, before it happens."""
    real, seen = os.rename, []

    def rename(src, dst):
        seen.append(Path(dst).name)
        if len(seen) == n:
            raise Stopped()
        real(src, dst)

    with mock.patch.object(os, "rename", rename):
        yield seen


def valid(data, wait_id):
    return isinstance(data, dict) and data.get("wait_id") == wait_id and data.get("kind") == "test"


def task_text(task="task-abc", **fields):
    head = {"id": task, "source": "ag2space", "channel_id": ROOM}
    tail = {"user_id": OWNER, "access_tier": "owner"}
    for key, value in fields.items():
        (head if key in head or key == "collaborator" else tail)[key] = value
    lines = [f"{k}: {v}" for k, v in head.items() if v is not None] + ["task: hello"]
    return "\n".join(lines + [f"{k}: {v}" for k, v in tail.items() if v is not None]) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.store = wait_store.WaitStore(self.ws, "test-waits", valid)

    def tearDown(self):
        self.tmp.cleanup()

    def marker(self, wait_id=WAIT_ID, **extra):
        m = {"wait_id": wait_id, "kind": "test", "room": ROOM, "owner": OWNER, **extra}
        self.store.write_marker(m)
        return m


class StoreContract(Base):
    def test_waits_live_under_the_workspace_state_dir(self):
        self.marker()
        self.assertEqual(self.store.marker_path(WAIT_ID), self.ws / "state" / "test-waits" / f"{WAIT_ID}.json")
        self.assertEqual([m["wait_id"] for m in self.store.list_markers()], [WAIT_ID])

    def test_a_claim_has_exactly_one_winner(self):
        self.marker()
        won = []
        threads = [threading.Thread(target=lambda i=i: won.append(self.store.claim(WAIT_ID, f"t{i}", NOW)))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [w for w in won if w is not None]
        self.assertEqual(len(winners), 1)
        self.assertIsNone(self.store.read_marker(WAIT_ID))
        record = json.loads(self.store.claimed_path(WAIT_ID).read_text())
        self.assertEqual((record["claimed_by"], record["claimed_at"]), (winners[0]["claimed_by"], NOW))

    def test_a_marker_that_is_not_this_stores_is_claimed_as_invalid(self):
        self.store.dir().mkdir(parents=True)
        self.store.marker_path(WAIT_ID).write_text(json.dumps({"wait_id": WAIT_ID, "kind": "other"}))
        with self.assertRaises(ValueError):
            self.store.read_marker(WAIT_ID)
        self.assertEqual(self.store.list_markers(), [])
        self.assertEqual(self.store.claim(WAIT_ID, "x", NOW), {"wait_id": WAIT_ID, "invalid": True})

    def test_a_release_puts_the_wait_back_without_its_claim(self):
        self.marker()
        record = self.store.claim(WAIT_ID, "connected", NOW)
        self.assertTrue(self.store.release(record))
        back = self.store.read_marker(WAIT_ID)
        self.assertNotIn("claimed_by", back)
        self.assertNotIn("claimed_at", back)
        self.assertFalse(self.store.claimed_path(WAIT_ID).exists())

    def test_a_held_lock_is_a_live_waiter(self):
        self.assertFalse(self.store.waiter_alive(WAIT_ID))
        held = wait_store.hold_lock(self.store, WAIT_ID)
        try:
            self.assertTrue(self.store.waiter_alive(WAIT_ID))
            self.assertIsNone(wait_store.hold_lock(self.store, WAIT_ID))
            self.assertEqual(self.store.lock_path(WAIT_ID).read_text().strip(), str(os.getpid()))
        finally:
            held.close()
        self.assertFalse(self.store.waiter_alive(WAIT_ID))

    def test_prune_drops_old_claims_and_orphan_locks_only(self):
        self.marker()
        self.marker("1789000000001-0000abcd")
        self.store.claim("1789000000001-0000abcd", "x", NOW)
        for wait_id in (WAIT_ID, "1789000000002-0000abcd"):
            self.store.lock_path(wait_id).write_text("")
        old = NOW - 10
        for p in self.store.dir().iterdir():
            os.utime(p, (old, old))
        self.assertEqual(self.store.prune(NOW, 100), 0)
        self.assertEqual(self.store.prune(NOW, 5), 2)
        self.assertEqual(sorted(p.name for p in self.store.dir().iterdir()),
                         [f"{WAIT_ID}.json", f"{WAIT_ID}.lock"])

    def test_wait_ids_match_the_one_pattern(self):
        self.assertRegex(wait_store.new_wait_id(NOW), wait_store.WAIT_ID_RE)

    def test_the_resume_task_is_owner_tier_task_last_and_addressed_to_the_room(self):
        path = wait_store.write_resume_task(self.ws, "task-connect-x", source="connector-resume", room=ROOM,
                                            owner=OWNER, body="carry on", now=NOW)
        self.assertEqual(path, self.ws / "tasks" / "task-connect-x.txt")
        parsed = ltp.parse_task_headers(path.read_text())
        self.assertEqual(parsed.body.strip(), "carry on")
        self.assertEqual({k: parsed.headers[k] for k in ("id", "source", "channel_id", "user_id", "access_tier")},
                         {"id": "task-connect-x", "source": "connector-resume", "channel_id": ROOM,
                          "user_id": OWNER, "access_tier": "owner"})

    def test_the_detached_waiter_gets_its_own_session(self):
        script = self.ws / "skill.py"
        with mock.patch.object(wait_store.subprocess, "Popen") as popen:
            popen.return_value.pid = 31
            self.assertEqual(wait_store.spawn_detached(script, self.ws, WAIT_ID, "x.log"), 31)
        argv, kwargs = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(argv[1:], [str(script), "--workspace", str(self.ws), "waiter", WAIT_ID])
        self.assertTrue(kwargs["start_new_session"])
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue((self.ws / "logs" / "x.log").exists())


class PublicationCase(Base):
    """A store whose resume tasks are written by a recording writer."""

    OUTCOMES = ("connected", "timeout")

    def setUp(self):
        super().setUp()
        self.written = []

    def write(self, record):
        self.written.append(record["wait_id"])
        return wait_store.write_resume_task(self.ws, self.task_id(record), source="connector-resume", room=ROOM,
                                            owner=OWNER, body=f"resume {record['claimed_by']}", now=NOW)

    @staticmethod
    def task_id(record):
        return f"task-connect-x-{record['wait_id']}"

    def recover(self, now=NOW, stale_after_s=3600):
        return wait_store.recover(self.store, self.ws, outcomes=self.OUTCOMES, task_id=self.task_id,
                                  write=self.write, now=now, stale_after_s=stale_after_s)

    def claimed(self, by="connected", wait_id=WAIT_ID):
        self.marker(wait_id)
        return self.store.claim(wait_id, by, NOW)

    def record(self, wait_id=WAIT_ID):
        return json.loads(self.store.claimed_path(wait_id).read_text())


class Publication(PublicationCase):
    """A claimed wait's resume task is written once, whatever stopped between the claim and the task."""

    def test_a_workspace_with_no_waits_has_nothing_to_recover(self):
        self.assertEqual(self.store.unpublished(self.OUTCOMES), [])
        self.assertEqual(self.recover(), [])
        self.assertFalse((self.ws / "tasks").exists())

    def test_publish_writes_the_task_then_stamps_the_claim(self):
        wait_store.publish(self.store, self.claimed(), self.write, NOW + 1)
        self.assertEqual(self.written, [WAIT_ID])
        self.assertEqual((self.record()["published_at"], self.record()["published"]), (NOW + 1, "task"))
        self.assertEqual(self.store.unpublished(self.OUTCOMES), [])
        self.assertEqual(self.recover(), [])

    def test_a_write_that_fails_leaves_no_stamp(self):
        def broken(record):
            raise OSError("disk full")

        with self.assertRaises(OSError):
            wait_store.publish(self.store, self.claimed(), broken, NOW)
        self.assertNotIn("published_at", self.record())

    def test_a_claim_stopped_before_its_task_is_published_once(self):
        self.claimed()
        self.assertEqual([r["wait_id"] for r in self.store.unpublished(self.OUTCOMES)], [WAIT_ID])
        self.assertEqual(self.recover(), [{"wait_id": WAIT_ID, "published": "task"}])
        self.assertEqual(self.recover(), [])
        self.assertEqual(self.written, [WAIT_ID])
        self.assertEqual(sorted(p.name for p in (self.ws / "tasks").iterdir()), [f"task-connect-x-{WAIT_ID}.txt"])

    def test_a_claim_stopped_after_its_task_and_before_the_stamp_writes_no_second_task(self):
        record = self.claimed()
        self.write(record)
        self.written.clear()
        self.assertEqual(self.recover(), [{"wait_id": WAIT_ID, "published": "found"}])
        self.assertEqual(self.written, [])
        self.assertEqual(self.record()["published"], "found")

    def test_a_stamp_that_could_not_be_written_is_made_good_without_a_second_task(self):
        record = self.claimed()
        with mock.patch.object(wait_store.WaitStore, "mark_published", side_effect=OSError("read-only")):
            wait_store.publish(self.store, record, self.write, NOW)
        self.assertNotIn("published_at", self.record())
        self.assertEqual(self.recover(), [{"wait_id": WAIT_ID, "published": "found"}])
        self.assertEqual(self.written, [WAIT_ID])

    def test_a_task_already_archived_counts_as_published(self):
        path = self.write(self.claimed())
        (self.ws / "tasks" / "archive").mkdir()
        path.rename(self.ws / "tasks" / "archive" / path.name)
        self.written.clear()
        self.assertEqual(self.recover(), [{"wait_id": WAIT_ID, "published": "found"}])
        self.assertEqual(self.written, [])

    def test_a_claim_that_owes_no_task_is_never_published(self):
        for by in ("superseded", "invalid", "expired", "claim"):
            with self.subTest(by):
                self.claimed(by)
                self.assertEqual(self.recover(), [])
                self.assertNotIn("published_at", self.record())
        self.assertEqual(self.written, [])

    def test_an_old_unstamped_claim_is_closed_without_a_task(self):
        self.claimed()
        self.assertEqual(self.recover(now=NOW + 3600), [{"wait_id": WAIT_ID, "published": "stale"}])
        self.assertEqual(self.written, [])
        self.assertEqual(self.recover(now=NOW + 3601), [])

    def test_a_claim_whose_lock_is_held_is_left_to_the_holder(self):
        self.claimed()
        held = self.store.acquire_lock(WAIT_ID)
        try:
            self.assertEqual(self.recover(), [])
        finally:
            held.close()
        self.assertEqual(self.written, [])
        self.assertEqual(self.recover(), [{"wait_id": WAIT_ID, "published": "task"}])

    def test_a_recovery_that_cannot_write_or_stamp_is_retried(self):
        self.claimed()

        def broken(record):
            raise OSError("disk full")

        self.assertEqual(wait_store.recover(self.store, self.ws, outcomes=self.OUTCOMES, task_id=self.task_id,
                                            write=broken, now=NOW, stale_after_s=3600), [])
        self.assertNotIn("published_at", self.record())
        with mock.patch.object(wait_store.WaitStore, "mark_published", side_effect=OSError("read-only")):
            self.assertEqual(self.recover(), [{"wait_id": WAIT_ID, "published": "task"}])
        self.assertEqual(self.recover(), [{"wait_id": WAIT_ID, "published": "found"}])
        self.assertEqual(self.written, [WAIT_ID])

    def test_two_recoveries_at_once_write_one_task(self):
        self.claimed()
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.recover())) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(self.written, [WAIT_ID])
        self.assertEqual(sum(len(r) for r in results), 1)


class InterruptedCase(PublicationCase):
    """Waits whose claimer was stopped part-way through a claim."""

    def names(self):
        return sorted(p.name.split(".")[-1] for p in self.store.dir().iterdir() if p.suffix != ".lock")

    def stop_after_taking(self, by="connected", staged=True):
        self.marker()
        fail_staging = mock.patch.object(wait_store.WaitStore, "write_json", side_effect=OSError("disk full"))
        with contextlib.ExitStack() as stack:
            if not staged:
                stack.enter_context(fail_staging)
            seen = stack.enter_context(stopping_at_rename(2))
            with self.assertRaises(Stopped):
                self.store.claim(WAIT_ID, by, NOW)
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.store.list_markers(), [], "the wait is no longer pending")
        self.assertFalse(self.store.claimed_path(WAIT_ID).exists())

    def recover_closing(self, **kw):
        return wait_store.recover(self.store, self.ws, outcomes=self.OUTCOMES, task_id=self.task_id,
                                  write=self.write, now=NOW, stale_after_s=3600, unstamped_as="timeout", **kw)

    def body(self):
        return ltp.parse_task_headers((self.ws / "tasks" / f"task-connect-x-{WAIT_ID}.txt").read_text()).body.strip()


class InterruptedClaim(InterruptedCase):
    """Every stop between taking a wait and stamping its outcome ends in one task of the right kind."""

    def test_a_claimed_record_names_its_outcome_as_soon_as_it_exists(self):
        self.marker()
        real, at_placing = os.rename, []

        def rename(src, dst):
            real(src, dst)
            if Path(dst) == self.store.claimed_path(WAIT_ID):
                at_placing.append(json.loads(Path(dst).read_text()).get("claimed_by"))

        with mock.patch.object(os, "rename", rename):
            self.store.claim(WAIT_ID, "connected", NOW)
        self.assertEqual(at_placing, ["connected"])
        self.assertEqual(self.names(), ["claimed"])

    def test_a_claimer_stopped_after_taking_the_wait_is_finished_with_its_own_outcome(self):
        self.stop_after_taking("connected")
        self.assertEqual(self.names(), ["claiming", "taken"])
        self.assertEqual(self.recover(), [{"wait_id": WAIT_ID, "published": "task"}])
        self.assertEqual((self.record()["claimed_by"], self.record()["claimed_at"]), ("connected", NOW))
        self.assertEqual(self.body(), "resume connected")
        self.assertEqual(self.recover(), [])
        self.assertEqual(self.written, [WAIT_ID])
        self.assertEqual(self.names(), ["claimed"])

    def test_a_stopped_claim_that_says_nothing_is_finished_silently(self):
        self.stop_after_taking("superseded")
        self.assertEqual(self.recover_closing(), [])
        self.assertEqual(self.record()["claimed_by"], "superseded")
        self.assertEqual(self.written, [])

    def test_a_claimer_stopped_before_taking_the_wait_leaves_it_pending(self):
        self.marker()
        with stopping_at_rename(1), self.assertRaises(Stopped):
            self.store.claim(WAIT_ID, "connected", NOW)
        self.assertEqual([m["wait_id"] for m in self.store.list_markers()], [WAIT_ID])
        self.assertEqual(self.recover_closing(), [])
        self.assertEqual(self.store.claim(WAIT_ID, "timeout", NOW)["claimed_by"], "timeout")
        self.assertEqual(self.store.prune(NOW + 10 ** 9, 5), 2, "the stale staged record goes with the old claim")

    def test_a_stopped_claim_with_no_staged_outcome_is_closed_as_the_non_success_one(self):
        self.stop_after_taking("connected", staged=False)
        self.assertEqual(self.names(), ["taken"])
        self.assertEqual(self.recover_closing(), [{"wait_id": WAIT_ID, "published": "task"}])
        self.assertEqual(self.recover_closing(), [])
        self.assertEqual(self.body(), "resume timeout")
        self.assertEqual((self.record()["claimed_by"], self.record()["unstamped"]), ("timeout", True))
        self.assertEqual((self.written, self.names()), ([WAIT_ID], ["claimed"]))

    def test_an_old_claim_with_no_outcome_gets_no_late_task(self):
        self.stop_after_taking("connected", staged=False)
        [taken] = self.store.dir().glob("*.taken")
        os.utime(taken, (NOW - 7200, NOW - 7200))
        self.assertEqual(self.recover_closing(), [{"wait_id": WAIT_ID, "published": "stale"}])
        self.assertEqual(self.written, [])
        self.assertEqual((self.record()["claimed_by"], self.record()["claimed_at"], self.record()["published"]),
                         ("timeout", NOW - 7200, "stale"))
        self.assertEqual(self.recover_closing(), [])

    def test_a_claim_with_no_outcome_inside_the_window_is_dated_by_its_file_not_by_recovery(self):
        self.stop_after_taking("connected", staged=False)
        [taken] = self.store.dir().glob("*.taken")
        os.utime(taken, (NOW - 1800, NOW - 1800))
        self.assertEqual(self.recover_closing(), [{"wait_id": WAIT_ID, "published": "task"}])
        self.assertEqual((self.record()["claimed_at"], self.body()), (NOW - 1800, "resume timeout"))

    def test_the_waits_recorded_deadline_dates_the_claim_whatever_the_files_time(self):
        cases = {"old deadline, fresh file": (NOW - 7200, NOW, "stale", NOW - 7200),
                 "deadline still ahead, old file": (NOW + 600, NOW - 7200, "task", NOW),
                 "deadline inside the window": (NOW - 600, NOW, "task", NOW - 600)}
        for n, (name, (deadline, file_time, how, claimed_at)) in enumerate(cases.items()):
            with self.subTest(name):
                wait_id = f"178900000000{n}-0000abcd"
                self.marker(wait_id, deadline=deadline)
                with mock.patch.object(wait_store.WaitStore, "write_json", side_effect=OSError("disk full")), \
                        stopping_at_rename(2), self.assertRaises(Stopped):
                    self.store.claim(wait_id, "connected", NOW)
                [taken] = self.store.dir().glob(f"{wait_id}.*.taken")
                os.utime(taken, (file_time, file_time))
                self.assertEqual(self.recover_closing(), [{"wait_id": wait_id, "published": how}])
                self.assertEqual(self.record(wait_id)["claimed_at"], claimed_at)
        self.assertEqual(len(self.written), 2)

    def test_a_deadline_that_is_not_a_time_falls_back_to_the_file(self):
        for n, deadline in enumerate((True, "soon", float("inf"), None, 10 ** 400)):
            with self.subTest(repr(deadline)):
                wait_id = f"178900000001{n}-0000abcd"
                self.marker(wait_id, deadline=deadline)
                with mock.patch.object(wait_store.WaitStore, "write_json", side_effect=OSError("disk full")), \
                        stopping_at_rename(2), self.assertRaises(Stopped):
                    self.store.claim(wait_id, "connected", NOW)
                [taken] = self.store.dir().glob(f"{wait_id}.*.taken")
                os.utime(taken, (NOW - 7200, NOW - 7200))
                self.assertEqual(self.recover_closing(), [{"wait_id": wait_id, "published": "stale"}])
        self.assertEqual(self.written, [])

    def test_a_claim_that_cannot_be_dated_is_closed_as_stale_and_said_so(self):
        self.stop_after_taking("connected", staged=False)
        err = io.StringIO()
        with mock.patch.object(wait_store.WaitStore, "unstamped_since", return_value=None), \
                contextlib.redirect_stderr(err):
            self.assertEqual(self.recover_closing(), [{"wait_id": WAIT_ID, "published": "stale"}])
        self.assertIn("cannot be dated", err.getvalue())
        self.assertEqual((self.written, self.record()["claimed_at"]), ([], 0.0))

    def test_a_claimed_file_that_cannot_be_read_for_its_time_has_no_date(self):
        self.assertIsNone(self.store.unstamped_since(WAIT_ID, {"wait_id": WAIT_ID}))

    def test_recovery_does_not_refresh_a_stamped_claims_time(self):
        self.stop_after_taking("connected")
        self.assertEqual(self.recover(now=NOW + 3599), [{"wait_id": WAIT_ID, "published": "task"}])
        self.assertEqual(self.record()["claimed_at"], NOW)
        self.marker("1789000000009-0000abcd")
        with stopping_at_rename(2), self.assertRaises(Stopped):
            self.store.claim("1789000000009-0000abcd", "connected", NOW)
        self.assertEqual(self.recover(now=NOW + 3600), [{"wait_id": "1789000000009-0000abcd", "published": "stale"}])

    def test_a_claim_whose_stamp_could_not_be_written_is_closed_the_same_way(self):
        self.marker()
        with mock.patch.object(wait_store.WaitStore, "write_json", side_effect=OSError("disk full")):
            won = self.store.claim(WAIT_ID, "connected", NOW)
        self.assertEqual(won["claimed_by"], "connected")
        self.assertNotIn("claimed_by", self.record())
        self.assertEqual(self.recover_closing(), [{"wait_id": WAIT_ID, "published": "task"}])
        self.assertEqual(self.body(), "resume timeout")

    def test_a_claim_with_no_outcome_is_kept_when_no_closing_outcome_is_named(self):
        self.stop_after_taking("connected", staged=False)
        self.assertEqual(self.recover(), [])
        self.assertEqual((self.names(), self.written), (["claimed"], []))
        self.assertNotIn("claimed_by", self.record())

    def test_an_adoption_that_cannot_be_written_leaves_the_claim_for_next_time(self):
        self.stop_after_taking("connected", staged=False)
        with mock.patch.object(wait_store.WaitStore, "write_json", side_effect=OSError("read-only")):
            self.assertEqual(self.recover_closing(), [])
        self.assertEqual((self.names(), self.written), (["claimed"], []))
        self.assertNotIn("claimed_by", self.record())
        self.assertEqual(self.recover_closing(), [{"wait_id": WAIT_ID, "published": "task"}])

    def test_a_losers_staged_record_never_decides_the_outcome(self):
        self.marker()
        (self.store.dir() / f"{WAIT_ID}.0000aaaa.claiming").write_text(
            json.dumps({"wait_id": WAIT_ID, "kind": "test", "claimed_by": "connected", "claimed_at": NOW}))
        with mock.patch.object(wait_store.WaitStore, "write_json", side_effect=OSError("disk full")), \
                stopping_at_rename(2), self.assertRaises(Stopped):
            self.store.claim(WAIT_ID, "timeout", NOW)
        self.assertEqual(self.recover_closing(), [{"wait_id": WAIT_ID, "published": "task"}])
        self.assertEqual(self.body(), "resume timeout")

    def test_a_taken_file_that_cannot_be_placed_is_kept_for_the_next_recovery(self):
        self.stop_after_taking("connected")
        with mock.patch.object(os, "rename", side_effect=OSError("read-only")):
            self.assertFalse(self.store.finish_taken(WAIT_ID))
            self.assertEqual(self.recover(), [])
        self.assertEqual(self.names(), ["claiming", "taken"])
        self.assertEqual(self.recover(), [{"wait_id": WAIT_ID, "published": "task"}])

    def test_two_recoveries_of_a_taken_wait_write_one_task_with_its_outcome(self):
        self.stop_after_taking("connected")
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.recover_closing())) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((self.written, sum(len(r) for r in results)), ([WAIT_ID], 1))
        self.assertEqual((self.body(), self.names()), ("resume connected", ["claimed"]))


class NoReplacing(InterruptedCase):
    """A claimed record, once installed, is never replaced; whoever arrives second tidies up."""

    def taken_and_staged(self):
        [taken] = self.store.dir().glob(f"{WAIT_ID}.*.taken")
        return taken, taken.with_suffix(".claiming")

    def claimer_lands_before_recovery_installs(self, finish):
        """Run `finish` (the live claimer's remaining steps) the first time recovery reaches for the mutex."""
        real, fired = wait_store.WaitStore.installing, []

        @contextlib.contextmanager
        def installing(store):
            if not fired:
                fired.append(1)
                finish()
            with real(store):
                yield

        return mock.patch.object(wait_store.WaitStore, "installing", installing)

    def test_a_foreground_claim_that_lands_first_keeps_its_named_outcome(self):
        self.stop_after_taking("claim")
        taken, staged = self.taken_and_staged()

        def finish():
            os.rename(staged, self.store.claimed_path(WAIT_ID))

        with self.claimer_lands_before_recovery_installs(finish):
            self.assertEqual(self.recover_closing(), [])
        self.assertEqual((self.record()["claimed_by"], self.record()["claimed_at"]), ("claim", NOW))
        self.assertNotIn("unstamped", self.record())
        self.assertEqual((self.written, self.names()), ([], ["claimed"]), "no timeout task; the taken file is gone")

    def test_a_waiters_outcome_that_lands_first_gets_its_one_task_not_a_timeout(self):
        self.stop_after_taking("connected")
        taken, staged = self.taken_and_staged()

        def finish():
            os.rename(staged, self.store.claimed_path(WAIT_ID))
            taken.unlink()

        with self.claimer_lands_before_recovery_installs(finish):
            self.assertEqual(self.recover_closing(), [{"wait_id": WAIT_ID, "published": "task"}])
        self.assertEqual((self.body(), self.written), ("resume connected", [WAIT_ID]))
        self.assertEqual(self.recover_closing(), [])

    def test_a_leftover_taken_file_beside_an_installed_claim_is_removed_by_recovery(self):
        self.stop_after_taking("superseded")
        taken, staged = self.taken_and_staged()
        os.rename(staged, self.store.claimed_path(WAIT_ID))
        before = self.store.claimed_path(WAIT_ID).read_bytes()
        self.assertEqual(self.store.taken_ids(), [WAIT_ID])
        self.assertFalse(self.store.finish_taken(WAIT_ID))
        self.assertEqual(self.store.claimed_path(WAIT_ID).read_bytes(), before)
        self.assertEqual((self.names(), self.store.taken_ids()), (["claimed"], []))

    def test_prune_drops_a_taken_file_only_once_its_claim_is_installed(self):
        self.stop_after_taking("connected")
        taken, staged = self.taken_and_staged()
        old = NOW - 10 ** 6
        os.utime(taken, (old, old))
        self.assertEqual(self.store.prune(NOW, 10 ** 9), 0)
        self.assertTrue(taken.exists(), "a taken file with no claim installed is the wait itself")
        os.rename(staged, self.store.claimed_path(WAIT_ID))
        self.assertEqual(self.store.prune(time.time(), 10 ** 9), 1)
        self.assertEqual(self.names(), ["claimed"])

    def test_a_claim_never_replaces_an_installed_record(self):
        self.marker()
        self.store.claim(WAIT_ID, "connected", NOW)
        before = self.store.claimed_path(WAIT_ID).read_bytes()
        self.marker()
        self.assertIsNone(self.store.claim(WAIT_ID, "timeout", NOW + 1))
        self.assertEqual(self.store.claimed_path(WAIT_ID).read_bytes(), before)
        self.assertEqual(self.names(), ["claimed"])

    def mutex_is_held(self):
        with open(self.store.dir().with_name(self.store.dir().name + ".mutex"), "a+") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            return False

    def test_the_mutex_is_held_for_every_change_to_a_claimed_record(self):
        held = {}
        real_rename, real_write = os.rename, wait_store.write_json

        def rename(src, dst):
            held.setdefault("rename " + Path(dst).suffix, self.mutex_is_held())
            real_rename(src, dst)

        def write(path, data):
            held.setdefault("write " + Path(path).suffix, self.mutex_is_held())
            real_write(path, data)

        with mock.patch.object(os, "rename", rename), mock.patch.object(wait_store, "write_json", write):
            self.marker()
            held.clear()
            won = self.store.claim(WAIT_ID, "connected", NOW)
            self.assertEqual(held, {"write .claiming": True, "rename .taken": True, "rename .claimed": True})
            held.clear()
            self.assertTrue(self.store.release(won))
            self.assertEqual(held, {"write .claimed": True, "rename .json": True})
        self.stop_after_taking("connected", staged=False)
        with mock.patch.object(os, "rename", rename), mock.patch.object(wait_store, "write_json", write):
            held.clear()
            self.recover_closing()
        self.assertEqual((held["rename .claimed"], held["write .claimed"]), (True, True))
        self.assertFalse(self.mutex_is_held())

    def test_a_claimer_cannot_put_back_a_claim_that_recovery_closed(self):
        self.marker()
        with mock.patch.object(wait_store.WaitStore, "write_json", side_effect=OSError("disk full")):
            won = self.store.claim(WAIT_ID, "superseded", NOW)
        self.assertEqual(self.recover_closing(), [{"wait_id": WAIT_ID, "published": "task"}])
        self.assertFalse(self.store.release(won))
        self.assertEqual(self.record()["published"], "task")
        self.assertEqual(self.store.list_markers(), [])

    def test_a_claimer_puts_back_its_own_claim_stamped_or_not(self):
        self.marker()
        won = self.store.claim(WAIT_ID, "connected", NOW)
        self.assertTrue(self.store.release(won))
        with mock.patch.object(wait_store.WaitStore, "write_json", side_effect=OSError("disk full")):
            unstamped = self.store.claim(WAIT_ID, "connected", NOW)
        self.assertTrue(self.store.release(unstamped))
        self.assertEqual([m["wait_id"] for m in self.store.list_markers()], [WAIT_ID])

    def test_a_workspace_with_no_waits_has_nothing_to_finish(self):
        self.assertFalse(self.store.finish_taken(WAIT_ID))
        self.assertEqual(self.store.taken_ids(), [])


class OwnerTask(Base):
    def write(self, task="task-abc", **fields):
        (self.ws / "tasks").mkdir(exist_ok=True)
        (self.ws / "tasks" / f"{task}.txt").write_text(task_text(task, **fields))

    def test_the_owners_own_task_names_its_owner(self):
        self.write()
        owner, text = wait_store.owner_task(self.ws, "task-abc", "ag2space")
        self.assertEqual(owner, OWNER)
        self.assertIn("task: hello", text)

    def test_any_other_task_is_refused(self):
        cases = {
            "team": {"access_tier": "team"},
            "no tier": {"access_tier": None},
            "collaborator": {"collaborator": "true"},
            "other source": {"source": "discord"},
            "other id": {"id": "task-zzz"},
            "no user": {"user_id": None},
        }
        for name, fields in cases.items():
            with self.subTest(name):
                self.write(**fields)
                with self.assertRaises(wait_store.NotOwnerTask):
                    wait_store.owner_task(self.ws, "task-abc", "ag2space")
        with self.assertRaises(wait_store.NotOwnerTask):
            wait_store.owner_task(self.ws, "task-none", "ag2space")

    def test_a_second_tier_line_in_the_body_counts_against_the_task(self):
        self.write(access_tier="team")
        path = self.ws / "tasks" / "task-abc.txt"
        path.write_text(path.read_text().replace("task: hello", "task: hello\naccess_tier: owner"))
        with self.assertRaises(wait_store.NotOwnerTask):
            wait_store.owner_task(self.ws, "task-abc", "ag2space")


class Delegation(Base):
    """Each user's wait operations reach the shared implementation."""

    def spy(self, name):
        real = getattr(wait_store.WaitStore, name)
        calls = []

        def wrapper(store, *a, **k):
            calls.append(store.dirname)
            return real(store, *a, **k)

        patcher = mock.patch.object(wait_store.WaitStore, name, wrapper)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def apps_marker(self):
        m = {"version": 1, "wait_id": WAIT_ID, "toolkits": [{"slug": "gmail", "name": "Gmail"}], "room": ROOM,
             "reply_to": "$e", "request": "r", "task": "task-abc", "owner": OWNER, "deadline": NOW + 60}
        connectors.write_marker(self.ws, m)
        return m

    def mcp_marker(self):
        m = {"version": 1, "wait_id": WAIT_ID, "name": "luma", "room": ROOM, "reply_to": "$e", "request": "r",
             "task": "task-abc", "owner": OWNER, "session": "mcp-x", "port": 5000, "state_sha256": "ab",
             "socket": str(self.ws / f"sutando-mcp-{WAIT_ID}" / "s"),
             "attempt": 1, "deadline": NOW + 60}
        connect_mcp.store(self.ws).write_marker(m)
        return m

    def test_connect_apps_claims_releases_and_locks_through_the_store(self):
        claims, releases, locks = self.spy("claim"), self.spy("release"), self.spy("acquire_lock")
        self.apps_marker()
        record = connectors.claim(self.ws, WAIT_ID, "connected", NOW)
        self.assertEqual(record["claimed_by"], "connected")
        self.assertTrue(connectors.release(self.ws, record))
        self.assertFalse(connectors.waiter_alive(self.ws, WAIT_ID))
        self.assertEqual((claims, releases, locks), (["connect-waits"], ["connect-waits"], ["connect-waits"]))
        self.assertEqual(connectors.marker_path(self.ws, WAIT_ID),
                         wait_store.WaitStore(self.ws, "connect-waits", valid).marker_path(WAIT_ID))

    def test_connect_mcp_claims_and_locks_through_the_store(self):
        claims, locks = self.spy("claim"), self.spy("acquire_lock")
        self.mcp_marker()

        def gone(argv, timeout, stdin_text=None):
            return 1, ""

        self.assertEqual(connect_mcp.run_waiter(self.ws, WAIT_ID, gone, lambda: NOW, lambda s: None), "timeout")
        self.assertEqual((claims, locks), (["connect-mcp-waits"], ["connect-mcp-waits"]))

    def test_connect_apps_republishes_a_claim_its_waiter_left_without_a_task(self):
        self.apps_marker()
        self.assertIsNotNone(connectors.claim(self.ws, WAIT_ID, "connected", NOW))
        task = self.ws / "tasks" / f"task-connect-{WAIT_ID}.txt"
        self.assertFalse(task.exists())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(connectors.cmd_rearm(self.ws, spawn=lambda ws, w: 1, now=lambda: NOW), 0)
        self.assertEqual(json.loads(out.getvalue())["recovered"], [{"wait_id": WAIT_ID, "published": "task"}])
        self.assertIn("Gmail is now connected", task.read_text())
        written = task.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as again:
            connectors.cmd_rearm(self.ws, spawn=lambda ws, w: 1, now=lambda: NOW)
        self.assertEqual(json.loads(again.getvalue())["recovered"], [])
        self.assertEqual(task.read_bytes(), written)

    def test_a_connect_apps_waiter_restarted_on_a_claimed_wait_publishes_it_once(self):
        self.apps_marker()
        connectors.claim(self.ws, WAIT_ID, "timeout", NOW)
        cloud = mock.Mock()
        self.assertEqual(connectors.run_waiter(self.ws, WAIT_ID, cloud, lambda: NOW, lambda s: None), "recovered")
        self.assertEqual(connectors.run_waiter(self.ws, WAIT_ID, cloud, lambda: NOW, lambda s: None),
                         "claimed_elsewhere")
        self.assertEqual([p.name for p in (self.ws / "tasks").iterdir()], [f"task-connect-{WAIT_ID}.txt"])

    def test_connect_apps_finishes_a_claim_stopped_before_its_outcome_was_placed(self):
        self.apps_marker()
        with stopping_at_rename(2), self.assertRaises(Stopped):
            connectors.claim(self.ws, WAIT_ID, "connected", NOW)
        self.assertFalse(connectors.claimed_path(self.ws, WAIT_ID).exists())
        self.assertEqual(connectors.list_markers(self.ws), [])
        task = self.ws / "tasks" / f"task-connect-{WAIT_ID}.txt"
        for expected in ([{"wait_id": WAIT_ID, "published": "task"}], []):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                connectors.cmd_rearm(self.ws, spawn=lambda ws, w: 1, now=lambda: NOW)
            self.assertEqual(json.loads(out.getvalue())["recovered"], expected)
        self.assertIn("Gmail is now connected", task.read_text())
        self.assertEqual([p.name for p in (self.ws / "tasks").iterdir()], [task.name])

    def test_connect_apps_closes_a_claim_with_no_outcome_as_a_timeout_never_as_connected(self):
        self.apps_marker()
        with mock.patch.object(connectors, "_write_json", side_effect=OSError("disk full")), \
                stopping_at_rename(2), self.assertRaises(Stopped):
            connectors.claim(self.ws, WAIT_ID, "connected", NOW)
        self.assertEqual(connectors.recover(self.ws, NOW), [{"wait_id": WAIT_ID, "published": "task"}])
        self.assertEqual(connectors.recover(self.ws, NOW), [])
        text = (self.ws / "tasks" / f"task-connect-{WAIT_ID}.txt").read_text()
        self.assertIn("did not finish within", text)
        self.assertNotIn("now connected", text)

    def test_connect_apps_sends_no_late_note_for_an_old_claim_with_no_outcome(self):
        self.apps_marker()
        with mock.patch.object(connectors, "_write_json", side_effect=OSError("disk full")), \
                stopping_at_rename(2), self.assertRaises(Stopped):
            connectors.claim(self.ws, WAIT_ID, "connected", NOW)
        late = NOW + 60 + connectors.STALE_GRACE_S
        self.assertEqual(connectors.recover(self.ws, late), [{"wait_id": WAIT_ID, "published": "stale"}])
        self.assertFalse((self.ws / "tasks").exists())
        record = json.loads(connectors.claimed_path(self.ws, WAIT_ID).read_text())
        self.assertEqual((record["claimed_at"], record["published"]), (NOW + 60, "stale"))

    def test_connect_apps_still_closes_one_inside_its_window_a_second_before_it_ends(self):
        self.apps_marker()
        with mock.patch.object(connectors, "_write_json", side_effect=OSError("disk full")), \
                stopping_at_rename(2), self.assertRaises(Stopped):
            connectors.claim(self.ws, WAIT_ID, "connected", NOW)
        almost = NOW + 60 + connectors.STALE_GRACE_S - 1
        self.assertEqual(connectors.recover(self.ws, almost), [{"wait_id": WAIT_ID, "published": "task"}])

    def test_a_connect_apps_foreground_claim_is_not_replaced_by_a_recovery_of_the_same_wait(self):
        self.apps_marker()
        with stopping_at_rename(2), self.assertRaises(Stopped):
            connectors.claim(self.ws, WAIT_ID, "claim", NOW)
        [taken] = connectors.waits_dir(self.ws).glob("*.taken")
        os.rename(taken.with_suffix(".claiming"), connectors.claimed_path(self.ws, WAIT_ID))
        self.assertEqual(connectors.recover(self.ws, NOW), [])
        record = json.loads(connectors.claimed_path(self.ws, WAIT_ID).read_text())
        self.assertEqual(record["claimed_by"], "claim")
        self.assertFalse(taken.exists())
        self.assertFalse((self.ws / "tasks").exists(), "no timeout task for a wait the agent answered itself")

    def test_a_connect_apps_claim_by_the_agent_itself_owes_no_task(self):
        self.apps_marker()
        connectors.claim(self.ws, WAIT_ID, "claim", NOW)
        self.assertEqual(connectors.recover(self.ws, NOW), [])
        self.assertFalse((self.ws / "tasks").exists())

    def test_both_write_their_resume_task_with_the_one_writer(self):
        seen = []
        real = wait_store.write_resume_task

        def spy(ws, task_id, **k):
            seen.append((task_id, k["source"], k["room"], k["owner"]))
            return real(ws, task_id, **k)

        with mock.patch.object(wait_store, "write_resume_task", spy):
            connectors.write_resume_task(self.ws, self.apps_marker(), "connected", NOW)
            connect_mcp.write_resume_task(self.ws, {**self.mcp_marker(), "claimed_by": "connected"}, "connected", NOW)
        self.assertEqual(seen, [(f"task-connect-{WAIT_ID}", "connector-resume", ROOM, OWNER),
                                (f"task-connect-mcp-{WAIT_ID}", "connector-resume", ROOM, OWNER)])

    def test_both_check_the_owner_with_the_one_test(self):
        seen = []

        def refuse(ws, task, source, source_name=""):
            seen.append((task, source))
            raise wait_store.NotOwnerTask("is not owner tier")

        with mock.patch.object(wait_store, "owner_task", refuse):
            with self.assertRaises(connectors.Setup) as apps:
                connectors.origin_owner(self.ws, "task-abc")
            with self.assertRaises(connect_mcp.Setup) as mcp:
                connect_mcp.dm_task(self.ws, "task-abc")
        self.assertEqual(seen, [("task-abc", "ag2space"), ("task-abc", "ag2space")])
        self.assertEqual((apps.exception.code, mcp.exception.code), ("not_owner_task", "not_owner_dm"))

    def test_both_spawn_their_waiter_with_the_one_spawner(self):
        with mock.patch.object(wait_store.subprocess, "Popen") as popen:
            popen.return_value.pid = 9
            connectors.spawn_waiter(self.ws, WAIT_ID)
            connect_mcp.spawn_waiter(self.ws, WAIT_ID)
        scripts = [c.args[0][1] for c in popen.call_args_list]
        self.assertEqual(scripts, [str(CONNECTORS_PATH), str(CONNECT_MCP_PATH)])

    def test_neither_user_keeps_its_own_rename_or_lock(self):
        for path in (CONNECTORS_PATH, CONNECT_MCP_PATH):
            with self.subTest(path.name):
                calls = {f"{n.func.value.id}.{n.func.attr}" for n in ast.walk(ast.parse(path.read_text()))
                         if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                         and isinstance(n.func.value, ast.Name)}
                self.assertFalse(calls & {"os.rename", "subprocess.Popen", "ltp.write_task_file"},
                                 "claim, spawn and the resume task belong to src/wait_store.py")


if __name__ == "__main__":
    unittest.main()
