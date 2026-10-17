#!/usr/bin/env python3
"""`durable: true` is a promise the client has to be able to keep.

The relay only calls a task accepted when the ack carries `durable: true`, so
the client may send it only once the task file, its media sidecar and the
in-flight set have reached the disk — and must withhold the ack entirely
otherwise. Acks that never confirm are persisted and retried.

Covers:
  1. the four state writes go temp → fsync file → rename → fsync directory
     where the platform supports directory descriptors;
  2. a task queued by a pre-durability client and redelivered after the upgrade
     is verified, fsync'd and given its sidecar before a durable ack is allowed,
     as is the pending reply of a redelivery this node already handled;
  3. every failure boundary (task write, sidecar, in-flight set, ack ledger)
     withholds the ack rather than claiming a task that could be lost;
  4. pending acks are retried by the outbound worker, retired on a per-task
     `not leased` 404, and kept behind the cooldown on a bare no-route 404;
  5. the production ack round — `main()` itself, not a copy of its ordering —
     puts `durable` on the wire and withholds every ack of a failed round.

Run: python3 tests/gateway-durable-ack.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "remote-gateway-bridge.py"

SIGNAL = {"v": 1, "cid": "cid-9", "route_attempt": 1,
          "source_root": "$root:server", "source_room": "!room:server"}


def _load(ws: Path):
    """Load the hyphenated bridge module against a scratch workspace."""
    os.environ["SUTANDO_TEST_WORKSPACE"] = str(ws)
    os.environ.setdefault("REMOTE_TASK_TOKEN", "test-token-0123456789abcdef")
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.invalid/relay")
    os.environ["GATEWAY_INSTANCE"] = ""
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location("rgb_durable_ack", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("workspace_default.resolve_workspace", return_value=ws):
        spec.loader.exec_module(mod)
    mod.WS = ws
    mod.TASKS_DIR = ws / "tasks"
    mod.RESULTS_DIR = ws / "results"
    mod.ARCHIVE_RESULTS_DIR = ws / "results" / "archive"
    mod.INFLIGHT_FILE = ws / "state" / "remote-task-inflight.json"
    mod.TASK_ROOMS_FILE = ws / "state" / "remote-task-rooms.json"
    mod.TASK_MEDIA_FILE = ws / "state" / "remote-task-media.json"
    mod.PENDING_ACK_FILE = ws / "state" / "remote-task-acks.json"
    mod.DEDUP_ALIAS_FILE = ws / "state" / "remote-dedup-alias.json"
    mod._ack_disabled_until = 0.0
    for d in (mod.TASKS_DIR, mod.RESULTS_DIR, ws / "state"):
        d.mkdir(parents=True, exist_ok=True)
    return mod


def _http(code: int, body: bytes = b""):
    return urllib.error.HTTPError("https://gw.invalid/relay", code, "err", {},
                                  io.BytesIO(body))


class DurableAck(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self._tmp.name)
        self.mod = _load(self.ws)
        self.calls: list[tuple[str, str, dict | None]] = []
        self._req_patch = patch.object(self.mod, "_req", side_effect=self._fake_req)
        self._req_patch.start()
        self.addCleanup(self._req_patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def _fake_req(self, method, path, payload=None, timeout=35):
        self.calls.append((method, path, payload))
        return {}

    def _acks(self):
        return [c for c in self.calls if c[1].endswith("/ack")]

    def _task(self, tid: str, signal: bool = True) -> dict:
        task = {"id": tid, "task": "dig into this", "source": "ag2space",
                "channel_id": "!room:server", "access_tier": "owner",
                "user_id": "@owner:server", "thread_root": "$root:server"}
        if signal:
            task["signal"] = dict(SIGNAL)
        return task

    def _ack_round(self, tasks: list) -> list:
        """main()'s ordering: nothing is acked until every durable write lands."""
        mod = self.mod
        inflight, pending = mod._load_inflight(), []
        for task in tasks:
            written = mod._write_task(task)
            if written:
                tid, durable = written
                inflight.add(tid)
                pending.append((tid, durable, mod._lease_id_of(task)))
        committed = mod._save_inflight(inflight) if pending else True
        if pending and committed:
            committed = mod._record_pending_acks(pending)
        for tid, durable, lease in pending if committed else ():
            mod._post_task_ack(tid, durable, lease)
        return pending

    # -- 1. the durable-write sequence -------------------------------------- #

    def test_durable_write_fsyncs_the_file_then_the_directory_when_supported(self):
        mod = self.mod
        trace: list[str] = []
        real_fsync, real_replace = mod.os.fsync, mod.os.replace

        def spy_fsync(fd):
            kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            trace.append(f"fsync-{kind}")
            return real_fsync(fd)

        def spy_replace(src, dst):
            trace.append("rename")
            return real_replace(src, dst)

        with patch.object(mod.os, "fsync", spy_fsync), \
             patch.object(mod.os, "replace", spy_replace):
            self.assertTrue(mod._durable_write(self.ws / "state" / "x.json", "{}"))
        expected = ["fsync-file", "rename"]
        if os.name != "nt":
            expected.append("fsync-dir")
        self.assertEqual(trace, expected)

    def test_the_queue_write_publishes_through_the_durable_sequence(self):
        mod = self.mod
        trace: list[str] = []
        real_fsync, real_replace = mod.os.fsync, mod.os.replace

        def spy_fsync(fd):
            trace.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
            return real_fsync(fd)

        def spy_replace(src, dst):
            trace.append(f"rename:{Path(dst).name}")
            return real_replace(src, dst)

        with patch.object(mod.os, "fsync", spy_fsync), \
             patch.object(mod.os, "replace", spy_replace):
            self.assertEqual(mod._write_task(self._task("task-seq")), ("task-seq", True))
        at = trace.index("rename:remote-task-media.json")
        # Both files staged+fsync'd first, then the media + room-map sidecars
        # commit, and only then does the task become visible to the watcher.
        self.assertEqual(trace[:at].count("file"), 2)
        # Room sidecar commits BEFORE publish, so its rename sits between the
        # media sidecar's dir-fsync and the task file's own rename.
        expected = ["rename:remote-task-media.json", "rename:remote-task-rooms.json",
                    "rename:task-seq.txt"]
        if os.name != "nt":
            expected[1:1] = ["dir"]
            expected.append("dir")
        self.assertEqual(trace[at:at + len(expected)], expected)

    # -- 2. a pre-durability task redelivered after the upgrade -------------- #

    def test_pre_s1_pending_task_is_repaired_before_a_durable_ack(self):
        mod = self.mod
        # What a pre-S1 client left behind: a queued task file and nothing else.
        (mod.TASKS_DIR / "task-old.txt").write_text(
            "id: task-old\naccess_tier: owner\ntask: earlier\n")
        self.assertFalse(mod.TASK_MEDIA_FILE.exists())
        seen: list[bool] = []
        real = mod._post_task_ack

        def ack(tid, durable=False, lease_id=None):
            seen.append(mod.TASK_MEDIA_FILE.exists())
            return real(tid, durable, lease_id)

        with patch.object(mod, "_post_task_ack", side_effect=ack):
            self._ack_round([self._task("task-old")])
        self.assertEqual(seen, [True], "the sidecar must exist before the ack")
        self.assertEqual(self._acks()[0][2], {"id": "task-old", "durable": True})
        self.assertEqual(mod._load_task_media()["task-old"],
                         {"mode": "task-media", "thread_root": "$root:server"})
        self.assertEqual((mod.TASKS_DIR / "task-old.txt").read_text(),
                         "id: task-old\naccess_tier: owner\ntask: earlier\n",
                         "repair must not rewrite the queued file")

    def _fsync_watch(self, want: "set[int]") -> "tuple[list, object]":
        """(what the ack saw, a context manager) — records whether every inode in
        `want` had been fsync'd by the time the ack was posted."""
        mod = self.mod
        synced: "set[int]" = set()
        at_ack: list = []
        real_fsync, real_ack = mod.os.fsync, mod._post_task_ack

        def spy(fd):
            synced.add(os.fstat(fd).st_ino)
            return real_fsync(fd)

        def ack(tid, durable=False, lease_id=None):
            at_ack.append(want <= synced)
            return real_ack(tid, durable, lease_id)

        class _Watch:
            def __enter__(_self):
                _self._p = [patch.object(mod.os, "fsync", spy),
                            patch.object(mod, "_post_task_ack", side_effect=ack)]
                for one in _self._p:
                    one.start()
                return _self

            def __exit__(_self, *exc):
                for one in reversed(_self._p):
                    one.stop()
                return False

        return at_ack, _Watch()

    def _refuse_fsync_of(self, *paths):
        """os.open refuses exactly these paths, so the fsync loop over them raises."""
        real_open = self.mod.os.open
        blocked = {str(p) for p in paths}

        def refuse(path, *a, **k):
            if str(path) in blocked:
                raise OSError("cannot open for fsync")
            return real_open(path, *a, **k)

        return patch.object(self.mod.os, "open", refuse)

    def test_the_repair_fsyncs_the_queued_task_and_its_directory_when_supported(self):
        mod = self.mod
        tfile = mod.TASKS_DIR / "task-old4.txt"
        tfile.write_text("id: task-old4\n")
        want = {tfile.stat().st_ino}
        if os.name != "nt":
            want.add(mod.TASKS_DIR.stat().st_ino)
        at_ack, watch = self._fsync_watch(want)
        with watch:
            self._ack_round([self._task("task-old4")])
        self.assertEqual(at_ack, [True],
                         "the queued file and tasks/ must be fsync'd before the ack")
        self.assertEqual(self._acks()[0][2], {"id": "task-old4", "durable": True})

    def test_a_repair_whose_fsync_fails_is_acked_without_durability(self):
        mod = self.mod
        tfile = mod.TASKS_DIR / "task-old5.txt"
        tfile.write_text("id: task-old5\n")
        with self._refuse_fsync_of(tfile, mod.TASKS_DIR):
            self._ack_round([self._task("task-old5")])
        self.assertEqual(self._acks()[0][2], {"id": "task-old5"},
                         "an uncommitted queue file must not be claimed as durable")

    def test_a_redelivery_commits_the_pending_reply_before_a_durable_ack(self):
        mod = self.mod
        (mod.TASKS_DIR / "archive").mkdir(parents=True, exist_ok=True)
        (mod.TASKS_DIR / "archive" / "task-red.txt").write_text("id: task-red\n")
        # What the local core left behind: a reply written with a plain
        # write_text, so this process has committed nothing of its own.
        rfile = mod.RESULTS_DIR / "task-red.txt"
        rfile.write_text("the answer the core already produced")
        want = {rfile.stat().st_ino}
        if os.name != "nt":
            want.add(mod.RESULTS_DIR.stat().st_ino)
        at_ack, watch = self._fsync_watch(want)
        with watch:
            self._ack_round([self._task("task-red")])
        self.assertEqual(at_ack, [True],
                         "the pending reply and results/ must commit before the ack")
        self.assertEqual(self._acks()[0][2], {"id": "task-red", "durable": True})
        self.assertEqual(rfile.read_text(), "the answer the core already produced",
                         "the repair must not rewrite the pending reply")

    def test_a_redelivery_whose_reply_will_not_commit_is_acked_plain(self):
        mod = self.mod
        (mod.TASKS_DIR / "archive").mkdir(parents=True, exist_ok=True)
        (mod.TASKS_DIR / "archive" / "task-red2.txt").write_text("id: task-red2\n")
        rfile = mod.RESULTS_DIR / "task-red2.txt"
        rfile.write_text("an earlier answer")
        with self._refuse_fsync_of(rfile, mod.RESULTS_DIR):
            self._ack_round([self._task("task-red2")])
        self.assertEqual(self._acks()[0][2], {"id": "task-red2"},
                         "an uncommitted reply must not be claimed as durable")

    def test_a_redelivery_recommits_the_in_flight_set(self):
        mod = self.mod
        # A pre-durability client's ledger: readable, but never fsync'd.
        mod.INFLIGHT_FILE.write_text(json.dumps(["task-old3"]))
        (mod.TASKS_DIR / "task-old3.txt").write_text("id: task-old3\n")
        saved: list = []
        real = mod._save_inflight
        with patch.object(mod, "_save_inflight",
                          side_effect=lambda s: saved.append(sorted(s)) or real(s)):
            self._ack_round([self._task("task-old3")])
        self.assertEqual(saved, [["task-old3"]],
                         "a redelivery of an already-inflight id still re-commits")
        self.assertEqual(self._acks()[0][2], {"id": "task-old3", "durable": True})

    def test_unrepairable_pending_task_is_acked_without_durability(self):
        mod = self.mod
        (mod.TASKS_DIR / "task-old2.txt").write_text("id: task-old2\n")
        with patch.object(mod, "_record_task_media", return_value=False):
            self._ack_round([self._task("task-old2")])
        self.assertEqual(self._acks()[0][2], {"id": "task-old2"},
                         "an unrepaired task must not be claimed as durable")

    def test_an_ordinary_task_is_acked_durable_with_no_sidecar(self):
        self._ack_round([self._task("task-plain", signal=False)])
        self.assertEqual(self._acks()[0][2], {"id": "task-plain", "durable": True})
        self.assertFalse(self.mod.TASK_MEDIA_FILE.exists())

    # -- 3. every failure boundary withholds the ack ------------------------- #

    def test_task_write_failure_queues_nothing_and_acks_nothing(self):
        mod = self.mod
        with patch.object(mod, "_stage_durable", return_value=None):
            self.assertEqual(self._ack_round([self._task("task-w")]), [])
        self.assertEqual(self._acks(), [])
        self.assertFalse((mod.TASKS_DIR / "task-w.txt").exists())

    def test_sidecar_failure_queues_nothing_and_acks_nothing(self):
        mod = self.mod
        with patch.object(mod, "_save_inflight", return_value=True), \
             patch.object(mod, "_load_task_media", return_value=None):
            self.assertEqual(self._ack_round([self._task("task-s")]), [])
        self.assertEqual(self._acks(), [])
        self.assertFalse((mod.TASKS_DIR / "task-s.txt").exists())

    def test_inflight_persist_failure_withholds_the_ack(self):
        mod = self.mod
        with patch.object(mod, "_save_inflight", return_value=False):
            self._ack_round([self._task("task-i")])
        self.assertEqual(self._acks(), [])
        self.assertTrue((mod.TASKS_DIR / "task-i.txt").exists(),
                        "the task is queued; only the claim is withheld")

    def test_ack_ledger_persist_failure_withholds_the_ack(self):
        mod = self.mod
        with patch.object(mod, "_record_pending_acks", return_value=False):
            self._ack_round([self._task("task-a")])
        self.assertEqual(self._acks(), [])

    def test_one_failed_write_withholds_the_whole_round(self):
        mod = self.mod
        with patch.object(mod, "_save_inflight", return_value=False):
            self._ack_round([self._task("task-b1"), self._task("task-b2")])
        self.assertEqual(self._acks(), [])

    # -- 4. the pending-ack ledger ------------------------------------------ #

    def test_a_confirmed_ack_retires_its_record(self):
        mod = self.mod
        self._ack_round([self._task("task-ok")])
        self.assertEqual(mod._load_pending_acks(), {})

    def test_lost_response_keeps_the_record_and_the_worker_retries_it(self):
        mod = self.mod
        with patch.object(mod, "_req", side_effect=urllib.error.URLError("down")):
            self._ack_round([self._task("task-lost")])
        self.assertEqual(mod._load_pending_acks(), {"task-lost": True})
        mod._retry_pending_acks({"task-lost"})
        self.assertEqual(self._acks()[-1][2], {"id": "task-lost", "durable": True})
        self.assertEqual(mod._load_pending_acks(), {})

    def test_not_leased_404_retires_the_record_without_a_cooldown(self):
        mod = self.mod
        with patch.object(mod, "_req", side_effect=urllib.error.URLError("down")):
            self._ack_round([self._task("task-gone")])
        # The result completed before the retry, so the lease is closed for good.
        with patch.object(mod, "_req",
                          side_effect=_http(404, b'{"error":"not leased to you"}')):
            mod._retry_pending_acks({"task-gone"})
        self.assertEqual(mod._load_pending_acks(), {})
        self.assertEqual(mod._ack_disabled_until, 0.0)

    def test_bare_404_keeps_the_record_behind_the_cooldown(self):
        mod = self.mod
        with patch.object(mod, "_req", side_effect=_http(404, b"no route here")):
            self._ack_round([self._task("task-nortr")])
        self.assertEqual(mod._load_pending_acks(), {"task-nortr": True})
        self.assertGreater(mod._ack_disabled_until, 0.0)

    def test_a_retired_task_drops_its_pending_ack_unposted(self):
        mod = self.mod
        with patch.object(mod, "_req", side_effect=urllib.error.URLError("down")):
            self._ack_round([self._task("task-done")])
        before = len(self._acks())
        mod._retry_pending_acks(set())     # the result POST already closed it
        self.assertEqual(len(self._acks()), before)
        self.assertEqual(mod._load_pending_acks(), {})

    def test_the_outbound_worker_is_what_retries_a_pending_ack(self):
        """The retry reaches production only through `_outbound_worker`, so the
        seam — not the inner function — is what has to be driven."""
        mod = self.mod
        with patch.object(mod, "_req", side_effect=urllib.error.URLError("down")):
            self._ack_round([self._task("task-worker")])
        self.assertEqual(mod._load_pending_acks(), {"task-worker": True})
        mod.OUTBOUND_SCAN_S = 0.2
        worker = mod._start_outbound_worker({"task-worker"})
        try:
            mod.wake_outbound()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not self._acks():
                time.sleep(0.02)
        finally:
            mod._OUTBOUND_STOP.set()
            mod.wake_outbound()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual([c[2] for c in self._acks()],
                         [{"id": "task-worker", "durable": True}],
                         "the worker never re-posted the persisted ack")
        self.assertEqual(mod._load_pending_acks(), {})

    def test_the_ledger_survives_a_restart(self):
        mod = self.mod
        with patch.object(mod, "_req", side_effect=urllib.error.URLError("down")):
            self._ack_round([self._task("task-restart")])
        fresh = _load(self.ws)
        self.assertEqual(fresh._load_pending_acks(), {"task-restart": True})
        calls: list = []
        with patch.object(fresh, "_req", side_effect=lambda *a, **k: calls.append(a) or {}):
            fresh._retry_pending_acks({"task-restart"})
        self.assertEqual(calls[0][2], {"id": "task-restart", "durable": True})

    def test_a_served_lease_generation_rides_the_ack_and_its_retry_after_a_restart(self):
        """A task served with `lease_id` acks under that generation; a lease-less
        task acks without the key. The ledger keeps the generation, so the retry
        a fresh process posts after a restart names the serve it answers."""
        mod = self.mod
        leased = {**self._task("task-leased"), "lease_id": "gen-2"}
        self._ack_round([leased, self._task("task-plain")])
        self.assertEqual([c[2] for c in self._acks()],
                         [{"id": "task-leased", "lease_id": "gen-2", "durable": True},
                          {"id": "task-plain", "durable": True}])
        with patch.object(mod, "_req", side_effect=urllib.error.URLError("down")):
            self._ack_round([{**self._task("task-lost"), "lease_id": "gen-5"}])
        fresh = _load(self.ws)
        self.assertEqual(fresh._load_pending_acks(), {"task-lost": True})
        self.assertEqual(fresh._load_pending_leases(), {"task-lost": "gen-5"})
        calls: list = []
        with patch.object(fresh, "_req", side_effect=lambda *a, **k: calls.append(a) or {}):
            fresh._retry_pending_acks({"task-lost"})
        self.assertEqual(calls[0][2], {"id": "task-lost", "lease_id": "gen-5", "durable": True})
        self.assertEqual(fresh._load_pending_acks(), {})

    def test_a_stale_generation_404_retires_the_record_and_logs_the_task(self):
        """`404 {"reason": "stale lease generation"}`: the broker re-served the
        task under a newer generation, so this ack is retired like any other
        not-leased 404 — one log line names the task, no retry, no cooldown."""
        mod = self.mod
        with patch.object(mod, "_req", side_effect=urllib.error.URLError("down")):
            self._ack_round([{**self._task("task-stale"), "lease_id": "gen-1"}])
        self.assertEqual(mod._load_pending_leases(), {"task-stale": "gen-1"})
        stale = _http(404, b'{"error":"not leased to you","reason":"stale lease generation"}')
        logs: list[str] = []
        with patch.object(mod, "_req", side_effect=stale), \
             patch.object(mod, "_log", side_effect=lambda m, *a, **k: logs.append(str(m))):
            mod._retry_pending_acks({"task-stale"})
        self.assertEqual(mod._load_pending_acks(), {})
        self.assertEqual(mod._ack_disabled_until, 0.0)
        named = [l for l in logs if "task-stale" in l and "lease generation" in l]
        self.assertEqual(len(named), 1, logs)
        before = len(self.calls)
        mod._retry_pending_acks({"task-stale"})
        self.assertEqual(len(self.calls), before, "a retired ack is never re-posted")


    def test_an_older_reader_takes_a_leased_ledger_entry_at_its_durability(self):
        """A downgraded Sparrow reads every ledger value as `bool(v)`; a leased,
        non-durable entry must still read False there, never a durable claim."""
        mod = self.mod
        self.assertTrue(mod._record_pending_acks([("task-nd", False, "gen-1"),
                                                  ("task-d", True, "gen-2")]))
        raw = json.loads(mod.PENDING_ACK_FILE.read_text())
        older = {str(k): bool(v) for k, v in raw.items()}
        self.assertEqual(older, {"task-nd": False, "task-d": True})
        self.assertEqual(mod._load_pending_leases(), {"task-nd": "gen-1", "task-d": "gen-2"})

    def test_a_late_stale_404_does_not_drop_the_newer_generations_record(self):
        """A gen-1 retry answered `stale lease generation` after the task was
        re-served and recorded under gen-2 keeps the gen-2 record for its ack."""
        mod = self.mod
        self.assertTrue(mod._record_pending_acks([("task-regen", True, "gen-1")]))
        stale = _http(404, b'{"error":"not leased to you","reason":"stale lease generation"}')

        def req(method, path, payload=None, timeout=35):
            self.calls.append((method, path, payload))
            mod._record_pending_acks([("task-regen", True, "gen-2")])
            raise stale

        with patch.object(mod, "_req", side_effect=req):
            self.assertFalse(mod._post_task_ack("task-regen", True, "gen-1"))
        self.assertEqual(mod._load_pending_acks(), {"task-regen": True})
        self.assertEqual(mod._load_pending_leases(), {"task-regen": "gen-2"})
        mod._retry_pending_acks({"task-regen"})
        self.assertEqual(self._acks()[-1][2],
                         {"id": "task-regen", "lease_id": "gen-2", "durable": True})
        self.assertEqual(mod._load_pending_acks(), {})


    def test_a_lease_file_that_did_not_commit_withholds_the_round(self):
        """The ledger must never name a leased task whose generation is not on
        disk; an unreadable lease file fails open to no generations."""
        mod = self.mod
        real = mod._durable_write
        with patch.object(mod, "_durable_write",
                          side_effect=lambda p, t: False if p == mod._ack_lease_file() else real(p, t)):
            self.assertFalse(mod._record_pending_acks([("task-wl", True, "gen-1")]))
        self.assertEqual(mod._load_pending_acks(), {})
        self.assertTrue(mod._record_pending_acks([("task-wl", True, "gen-1")]))
        mod._ack_lease_file().write_text("{not json")
        self.assertEqual(mod._load_pending_leases(), {})
        self.assertEqual(mod._load_pending_acks(), {"task-wl": True})

    # -- 5. the production ack round, driven through main() ------------------ #

    def _drive_main(self, tasks: list, ack_raises: BaseException | None = None) -> None:
        """One iteration of the real `main()` — the second singleton check ends
        it. `_write_task`, `_save_inflight`, `_record_pending_acks` and
        `_post_task_ack` all stay live, so this drives the production ack round
        rather than a copy of it. Mirrors tests/gateway-longpoll-timeout.test.py.
        """
        mod = self.mod
        alive = [True, False]

        def req(method, path, payload=None, timeout=35):
            self.calls.append((method, path, payload))
            if path.startswith("/v1/tasks?wait="):
                return {"tasks": list(tasks)}
            if ack_raises is not None and path.endswith("/ack"):
                raise ack_raises
            return {}

        noops = ("_recover_orphan_proactive", "_maybe_start_event_channel",
                 "_post_heartbeat", "_post_ready_results", "_post_proactive",
                 "_retry_pending_acks", "_reconcile_orphan_results",
                 "_retry_pending_publications", "_retry_review_card_resolutions",
                 "_retry_review_control_results", "_emit_gateway_status",
                 "_start_results_watcher", "_log")
        with patch.multiple(mod, **{n: lambda *a, **k: None for n in noops}), \
                patch.object(mod, "_acquire_singleton", return_value=True), \
                patch.object(mod, "_heartbeat_singleton",
                             side_effect=lambda *a, **k: alive.pop(0) if alive else False), \
                patch.object(mod, "_reconcile_abandoned",
                             side_effect=lambda inflight, s, *a, **k: s), \
                patch.object(mod, "_req", side_effect=req):
            mod.main()

    def test_main_acks_the_task_it_durably_queued(self):
        mod = self.mod
        self._drive_main([self._task("task-main")])
        self.assertEqual(self._acks()[0][2], {"id": "task-main", "durable": True},
                         "the production call site must put `durable` on the wire")
        self.assertTrue((mod.TASKS_DIR / "task-main.txt").exists())
        self.assertEqual(json.loads(mod.INFLIGHT_FILE.read_text()), ["task-main"])
        self.assertEqual(mod._load_pending_acks(), {})

    def test_main_withholds_the_ack_when_the_in_flight_set_did_not_commit(self):
        with patch.object(self.mod, "_save_inflight", return_value=False):
            self._drive_main([self._task("task-main-i")])
        self.assertEqual(self._acks(), [])

    def test_main_withholds_the_ack_when_the_ack_ledger_did_not_commit(self):
        with patch.object(self.mod, "_record_pending_acks", return_value=False):
            self._drive_main([self._task("task-main-a")])
        self.assertEqual(self._acks(), [])

    def test_main_records_the_pending_ack_before_posting_it(self):
        mod = self.mod
        self._drive_main([self._task("task-main-lost")],
                         ack_raises=urllib.error.URLError("down"))
        self.assertEqual(mod._load_pending_acks(), {"task-main-lost": True},
                         "the ledger must be committed before the ack is posted")

    def test_main_carries_the_served_lease_generation_to_the_ack_and_the_ledger(self):
        """The production round echoes `lease_id` on the wire and records it for
        the retry, so a restart still acks under the generation it was served."""
        mod = self.mod
        self._drive_main([{**self._task("task-main-gen"), "lease_id": "gen-3"}])
        self.assertEqual(self._acks()[0][2],
                         {"id": "task-main-gen", "lease_id": "gen-3", "durable": True})
        self.calls.clear()
        self._drive_main([{**self._task("task-main-gen-lost"), "lease_id": "gen-4"}],
                         ack_raises=urllib.error.URLError("down"))
        self.assertEqual(_load(self.ws)._load_pending_leases(),
                         {"task-main-gen-lost": "gen-4"})

    def test_main_acks_an_unrepaired_redelivery_without_the_flag(self):
        mod = self.mod
        (mod.TASKS_DIR / "task-main-old.txt").write_text("id: task-main-old\n")
        with patch.object(mod, "_record_task_media", return_value=False):
            self._drive_main([self._task("task-main-old")])
        self.assertEqual(self._acks()[0][2], {"id": "task-main-old"})

    # Two fail-opens compose: `_load_inflight` returns empty on a corrupt file, and
    # `_retry_pending_acks` retires every id absent from it — unposted.

    def _seed_acks(self, ids):
        self.mod._durable_write(self.mod.PENDING_ACK_FILE,
                                json.dumps({t: True for t in ids}, sort_keys=True))

    def test_intact_inflight_posts_and_keeps_the_ledger(self):
        """CONTROL: a readable in-flight set holding the same ids posts all three."""
        mod = self.mod
        ids = ["task-A", "task-B", "task-C"]
        self._seed_acks(ids)
        mod.INFLIGHT_FILE.write_text(json.dumps(ids))
        mod._INFLIGHT_DEGRADED = False
        mod._retry_pending_acks(mod._load_inflight())
        self.assertEqual(sorted(c[1].split("/")[-2] for c in self._acks()), ids,
                         "an intact in-flight set must post every pending ack")

    def test_corrupt_inflight_does_not_retire_acks_unposted(self):
        """TREATMENT: identical, except the in-flight file is truncated mid-write."""
        mod = self.mod
        ids = ["task-A", "task-B", "task-C"]
        self._seed_acks(ids)
        mod.INFLIGHT_FILE.write_text('["task-A", "task-B", "tas')   # truncated
        mod._INFLIGHT_DEGRADED = False
        inflight = mod._load_inflight()
        self.assertEqual(inflight, set(), "precondition: the restore fails open to empty")
        self.assertTrue(mod._INFLIGHT_DEGRADED, "a failed restore must mark itself degraded")
        mod._retry_pending_acks(inflight)
        self.assertEqual(sorted(c[1].split("/")[-2] for c in self._acks()), ids,
                         "acks must be POSTED, not retired, when in-flight is unknown")
        self.assertEqual(sorted(mod._load_pending_acks()), [],
                         "a posted ack is retired normally by _post_task_ack")

    def test_non_list_inflight_is_also_unknown(self):
        """Valid JSON of the wrong type is corruption too, not an empty set."""
        mod = self.mod
        self._seed_acks(["task-A"])
        mod.INFLIGHT_FILE.write_text('{"task-A": true}')            # object, not list
        mod._INFLIGHT_DEGRADED = False
        inflight = mod._load_inflight()
        self.assertEqual(inflight, set())
        self.assertTrue(mod._INFLIGHT_DEGRADED, "a non-list payload must mark degraded")
        mod._retry_pending_acks(inflight)
        self.assertEqual([c[1].split("/")[-2] for c in self._acks()], ["task-A"])

    def test_missing_inflight_file_still_retires(self):
        """The fix must NOT disable retirement generally: a genuinely absent file
        means nothing is in flight, and that inference is still sound."""
        mod = self.mod
        self._seed_acks(["task-gone"])
        if mod.INFLIGHT_FILE.exists():
            mod.INFLIGHT_FILE.unlink()
        mod._INFLIGHT_DEGRADED = False
        inflight = mod._load_inflight()
        self.assertFalse(mod._INFLIGHT_DEGRADED, "FileNotFoundError is not degradation")
        mod._retry_pending_acks(inflight)
        self.assertEqual(self._acks(), [], "a closed lease is retired without posting")
        self.assertEqual(sorted(mod._load_pending_acks()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
