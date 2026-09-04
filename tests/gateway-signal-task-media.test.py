#!/usr/bin/env python3
"""Signal-task attachments upload against the task's own lease, not the room.

A task the relay stamped with `signal` carries its media through
`POST /v1/tasks/<wire id>/media`. The route is chosen from a sidecar committed
before the task is queued, so it survives a restart between the ack and the
upload — the served task itself never carries the flag.

Covers:
  1. wire-id resolution — the sidecar and the upload use the DELIVERY id
     (dedup alias applied, instance prefix stripped), never the local id;
  2. deferral — an unreadable alias ledger or media sidecar defers the whole
     result instead of guessing a route;
  3. route selection from the sidecar, including a restart between the ack and
     the upload (a fresh module reads the mode off disk);
  4. the sidecar is durable before the task file is renamed into tasks/;
  5. a 503 before or after the upload defers the result — no POST, no archive —
     and the retry re-offers the same wire id, ordinal and bytes;
  6. 409 and 423 report in-band and are never retried;
  7. two GATEWAY_INSTANCEs sharing a broker id never collide.

Run: python3 tests/gateway-signal-task-media.test.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "remote-gateway-bridge.py"

SIGNAL = {"v": 1, "cid": "cid-1", "route_attempt": 1,
          "source_root": "$root:server", "source_room": "!room:server"}


def _load(ws: Path, instance: str = ""):
    """Load the hyphenated bridge module against a scratch workspace."""
    os.environ["SUTANDO_TEST_WORKSPACE"] = str(ws)
    os.environ.setdefault("REMOTE_TASK_TOKEN", "test-token-0123456789abcdef")
    os.environ.setdefault("REMOTE_TASK_URL", "https://gw.invalid/relay")
    os.environ["GATEWAY_INSTANCE"] = instance
    sys.path.insert(0, str(REPO / "src"))
    spec = importlib.util.spec_from_file_location("rgb_task_media", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("workspace_default.resolve_workspace", return_value=ws):
        spec.loader.exec_module(mod)
    suffix = f".{instance}" if instance else ""
    mod.WS = ws
    mod.TASKS_DIR = ws / "tasks"
    mod.RESULTS_DIR = ws / "results"
    mod.ARCHIVE_RESULTS_DIR = ws / "results" / "archive"
    mod.INFLIGHT_FILE = ws / "state" / f"remote-task-inflight{suffix}.json"
    mod.TASK_ROOMS_FILE = ws / "state" / f"remote-task-rooms{suffix}.json"
    mod.TASK_MEDIA_FILE = ws / "state" / f"remote-task-media{suffix}.json"
    mod.PENDING_ACK_FILE = ws / "state" / f"remote-task-acks{suffix}.json"
    mod.DEDUP_ALIAS_FILE = ws / "state" / f"remote-dedup-alias{suffix}.json"
    for d in (mod.TASKS_DIR, mod.RESULTS_DIR, ws / "state"):
        d.mkdir(parents=True, exist_ok=True)
    return mod


def _http(code: int, body: bytes = b""):
    return urllib.error.HTTPError("https://gw.invalid/relay", code, "err", {},
                                  io.BytesIO(body))


class TaskMediaRoute(unittest.TestCase):
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

    # -- fixtures ---------------------------------------------------------- #

    def _attachment(self) -> str:
        """An allowlisted file: /tmp/sutando-* is a send_allowlist prefix."""
        fd, fpath = tempfile.mkstemp(prefix="sutando-signal-media-", suffix=".txt",
                                     dir="/tmp")
        os.write(fd, b"payload")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(fpath) and os.unlink(fpath))
        return fpath

    def _signal_task(self, tid: str) -> dict:
        return {"id": tid, "task": "dig into this", "source": "ag2space",
                "channel_id": "!room:server", "access_tier": "owner",
                "user_id": "@owner:server", "signal": dict(SIGNAL),
                "thread_root": "$root:server", "source_room_id": "!room:server"}

    def _result(self, mod, tid: str, body: str) -> None:
        (mod.RESULTS_DIR / f"{tid}.txt").write_text(body)

    def _media_posts(self):
        return [c for c in self.calls if "/media" in c[1]]

    def _result_posts(self):
        return [c for c in self.calls if c[1] == "/v1/results"]

    # -- 0. the team-tier allowance (5G ⑤a-cap): the guard runs for real ------ #

    def _team_signal_task(self, tid: str) -> dict:
        task = self._signal_task(tid)
        task["access_tier"] = "team"
        task["user_id"] = "@collab:server"
        return task

    def _allow_this_workspace_results(self, mod) -> None:
        # `send_allowlist` fixes its results root at FIRST import (sutando's shim
        # calls set_dirs() before it, so in production RESULTS_DIR IS that root).
        # This harness reloads the bridge per test but the allowlist module stays
        # cached with the first test's workspace, so register this one's — the
        # same wiring the shim does, and not the thing these tests assert.
        import ag2_sparrow.send_allowlist as send_allowlist
        send_allowlist.register_extra_roots(str(mod.RESULTS_DIR))

    def test_team_result_attaches_a_file_from_its_own_output_dir(self):
        mod = self.mod
        mod.LOCAL_TIER = "team"
        mod._load_tier_map = lambda: {}
        self._allow_this_workspace_results(mod)
        mod._write_task(self._team_signal_task("task-team-in"))
        own = mod.RESULTS_DIR / "task-team-in"
        own.mkdir(parents=True, exist_ok=True)
        (own / "chart.png").write_bytes(b"payload")
        self._result(mod, "task-team-in", f"the chart [file: {own / 'chart.png'}]")
        mod._post_ready_results({"task-team-in"})
        media = self._media_posts()
        self.assertEqual(len(media), 1, "an in-root attachment is uploaded on the task's lease")
        self.assertTrue(media[0][1].startswith("/v1/tasks/"), media[0][1])
        self.assertEqual(media[0][2]["filename"], "chart.png")
        self.assertTrue(self._result_posts(), "and the result itself is delivered")

    def test_team_result_pointing_outside_its_dir_uploads_nothing(self):
        mod = self.mod
        mod.LOCAL_TIER = "team"
        mod._load_tier_map = lambda: {}
        self._allow_this_workspace_results(mod)
        mod._write_task(self._team_signal_task("task-team-out"))
        stray = self._attachment()  # a /tmp file — allowlisted for OWNER sends, not this task's root
        self._result(mod, "task-team-out", f"the chart [file: {stray}]")
        try:
            mod._post_ready_results({"task-team-out"})
        except Exception:
            pass  # a review-routing failure leaves the result for retry; either way no upload
        self.assertEqual(self._media_posts(), [], "an out-of-root marker never reaches the media route")
        delivered = [c for c in self._result_posts() if c[2].get("result", "").startswith("the chart")]
        self.assertEqual(delivered, [], "and the raw body is never delivered to the room")

    # -- 1. wire-id resolution --------------------------------------------- #

    def test_upload_uses_the_delivery_id_not_the_local_id(self):
        mod = self.mod
        mod._write_task(self._signal_task("task-orig"))
        # A dedup re-ask answers the ORIGINAL delivery: same wire id, new local id.
        mod._save_dedup_aliases({"task-reask": "task-orig"})
        (mod.TASKS_DIR / "task-reask.txt").write_text("id: task-reask\naccess_tier: owner\n")
        self._result(mod, "task-reask", f"here [file: {self._attachment()}]")
        mod._post_ready_results({"task-reask"})
        self.assertEqual([c[1] for c in self._media_posts()],
                         ["/v1/tasks/task-orig/media"])
        self.assertEqual(self._result_posts()[0][2]["id"], "task-orig")

    def test_named_instance_posts_the_wire_id(self):
        with tempfile.TemporaryDirectory() as d:
            mod = _load(Path(d), instance="alpha")
            calls: list = []
            with patch.object(mod, "_req", side_effect=lambda *a, **k: calls.append(a) or {}):
                written = mod._write_task(self._signal_task("task-dup"))
                self.assertEqual(written, ("task-alpha~task-dup", True))
                self.assertEqual(mod._load_task_media()["task-dup"],
                                 {"mode": "task-media", "thread_root": "$root:server"})
                self._result(mod, "task-alpha~task-dup",
                             f"x [file: {self._attachment()}]")
                mod._post_ready_results({"task-alpha~task-dup"})
            self.assertIn(("POST", "/v1/tasks/task-dup/media"),
                          [(c[0], c[1]) for c in calls])

    # -- 2. deferral -------------------------------------------------------- #

    def test_unreadable_alias_ledger_defers_the_whole_result(self):
        mod = self.mod
        mod._write_task(self._signal_task("task-defer1"))
        self._result(mod, "task-defer1", f"x [file: {self._attachment()}]")
        inflight = {"task-defer1"}
        with patch.object(mod, "_delivery_tid", return_value=None):
            mod._post_ready_results(inflight)
        self.assertEqual(self._media_posts() + self._result_posts(), [])
        self.assertTrue((mod.RESULTS_DIR / "task-defer1.txt").exists())
        self.assertIn("task-defer1", inflight)

    def test_unreadable_media_sidecar_defers_rather_than_guessing_the_route(self):
        mod = self.mod
        mod._write_task(self._signal_task("task-defer2"))
        self._result(mod, "task-defer2", f"x [file: {self._attachment()}]")
        mod.TASK_MEDIA_FILE.write_text("{not json")
        inflight = {"task-defer2"}
        mod._post_ready_results(inflight)
        self.assertEqual(self._media_posts() + self._result_posts(), [],
                         "no route may be guessed from an unreadable map")
        self.assertIn("task-defer2", inflight)

    # -- 3. route selection ------------------------------------------------- #

    def test_ordinary_task_still_uses_the_room_route(self):
        mod = self.mod
        task = self._signal_task("task-plain")
        task.pop("signal")
        mod._write_task(task)
        self._result(mod, "task-plain", f"x [file: {self._attachment()}]")
        mod._post_ready_results({"task-plain"})
        self.assertEqual([c[1] for c in self._media_posts()],
                         ["/v1/rooms/%21room%3Aserver/media"])
        self.assertFalse(mod.TASK_MEDIA_FILE.exists(),
                         "an ordinary task must not write a media sidecar")

    def test_restart_between_ack_and_upload_still_routes_by_task(self):
        self.mod._write_task(self._signal_task("task-restart"))
        fresh = _load(self.ws)          # a new process: no in-memory state at all
        self._result(fresh, "task-restart", f"x [file: {self._attachment()}]")
        with patch.object(fresh, "_req", side_effect=self._fake_req):
            fresh._post_ready_results({"task-restart"})
        self.assertEqual([c[1] for c in self._media_posts()],
                         ["/v1/tasks/task-restart/media"])

    def test_a_dedup_requeue_keeps_the_task_media_route(self):
        """A requeue carries the SAME delivery forward, so its route must survive.

        The sidecar is keyed by the delivery wire id; retiring it here would
        silently downgrade the re-ask's attachment to the room-scoped route,
        outside the task's own lease.
        """
        mod = self.mod
        holder = "task-22d83e59601f3a1fef"
        mod._write_task(self._signal_task("task-requeue"))
        # The holder delivered nothing, so the plan re-asks rather than honouring.
        mod.ARCHIVE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        (mod.ARCHIVE_RESULTS_DIR / f"{holder}-1785976425.txt").write_text("")
        self._result(mod, "task-requeue", f"[deduped: {holder}]")
        inflight = {"task-requeue"}
        mod._post_ready_results(inflight)
        reask = [p.stem for p in mod.TASKS_DIR.glob("task-*.txt")
                 if p.stem != "task-requeue"]
        self.assertEqual(len(reask), 1, "the dedup was not re-asked")
        self.assertEqual(mod._load_task_media().get("task-requeue"),
                         {"mode": "task-media", "thread_root": "$root:server"},
                         "the re-ask lost the delivery's media route")
        # The re-ask answers the ORIGINAL delivery, so its attachment still
        # uploads against that task's lease.
        self._result(mod, reask[0], f"here [file: {self._attachment()}]")
        mod._post_ready_results(inflight)
        self.assertEqual([c[1] for c in self._media_posts()],
                         ["/v1/tasks/task-requeue/media"])

    def test_delivered_result_retires_the_media_mode(self):
        mod = self.mod
        mod._write_task(self._signal_task("task-retire"))
        self._result(mod, "task-retire", "plain answer")
        mod._post_ready_results({"task-retire"})
        self.assertEqual(json.loads(mod.TASK_MEDIA_FILE.read_text()), {})

    # -- 4. the sidecar commits before the task is published ----------------- #

    def test_sidecar_is_visible_before_the_task_rename(self):
        mod = self.mod
        published: list[str] = []
        real = mod._publish_staged

        def spy(tmp, path):
            if path.name.endswith(".txt"):
                on_disk = json.loads(mod.TASK_MEDIA_FILE.read_text())
                self.assertIn("task-order", on_disk,
                              "the watcher could see the task before its route")
            published.append(path.name)
            return real(tmp, path)

        with patch.object(mod, "_publish_staged", side_effect=spy):
            mod._write_task(self._signal_task("task-order"))
        self.assertEqual(published, ["remote-task-media.json", "task-order.txt"])

    def test_sidecar_failure_publishes_neither_task_nor_ack(self):
        mod = self.mod
        with patch.object(mod, "_record_task_media", return_value=False):
            self.assertIsNone(mod._write_task(self._signal_task("task-nosidecar")))
        self.assertFalse((mod.TASKS_DIR / "task-nosidecar.txt").exists())
        self.assertEqual(list(mod.TASKS_DIR.glob("*.tmp")), [],
                         "the staged file must not be left behind")

    # -- 5. retryable upload failures defer the result ----------------------- #

    def test_503_defers_the_result_and_the_retry_reoffers_the_same_bytes(self):
        mod = self.mod
        mod._write_task(self._signal_task("task-503"))
        fpath = self._attachment()
        self._result(mod, "task-503", f"x [file: {fpath}]")
        inflight = {"task-503"}
        with patch.object(mod, "_req", side_effect=_http(503, b"retry later")):
            mod._post_ready_results(inflight)
        self.assertEqual(self._result_posts(), [], "a deferred upload must not POST")
        self.assertTrue((mod.RESULTS_DIR / "task-503.txt").exists())
        self.assertFalse(list(mod.ARCHIVE_RESULTS_DIR.glob("task-503-*.txt")))
        self.assertIn("task-503", inflight)
        first: list = []

        def refuse(method, path, payload=None, timeout=35):
            first.append((path, payload))
            raise _http(503)

        with patch.object(mod, "_req", side_effect=refuse):
            mod._post_ready_results(inflight)
        mod._post_ready_results(inflight)
        media = self._media_posts()
        self.assertEqual(len(media), 1, "the successful pass uploads exactly once")
        self.assertEqual(media[0][2], {"ordinal": 0,
                                       "filename": os.path.basename(fpath),
                                       "content_b64": "cGF5bG9hZA=="})
        self.assertEqual(first[0], (media[0][1], media[0][2]),
                         "the retry re-offers the same wire id, ordinal and bytes")
        self.assertEqual(len(self._result_posts()), 1)

    def test_503_after_a_restart_uploads_once_with_identical_content(self):
        mod = self.mod
        mod._write_task(self._signal_task("task-503r"))
        fpath = self._attachment()
        self._result(mod, "task-503r", f"x [file: {fpath}]")
        sent: list = []

        def record(method, path, payload=None, timeout=35):
            sent.append((path, payload))
            raise _http(503)

        with patch.object(mod, "_req", side_effect=record):
            mod._post_ready_results({"task-503r"})
        fresh = _load(self.ws)          # restart: no _uploaded_attachments memory
        with patch.object(fresh, "_req", side_effect=self._fake_req):
            fresh._post_ready_results({"task-503r"})
        media = self._media_posts()
        self.assertEqual(len(media), 1)
        self.assertEqual((media[0][1], media[0][2]), sent[0],
                         "the resumed upload is byte-identical to the lost one")

    def test_network_failure_defers_too(self):
        mod = self.mod
        mod._write_task(self._signal_task("task-net"))
        self._result(mod, "task-net", f"x [file: {self._attachment()}]")
        inflight = {"task-net"}
        with patch.object(mod, "_req", side_effect=urllib.error.URLError("down")):
            mod._post_ready_results(inflight)
        self.assertTrue((mod.RESULTS_DIR / "task-net.txt").exists())
        self.assertIn("task-net", inflight)

    # -- 6. terminal upload refusals report in-band -------------------------- #

    def test_conflict_and_encrypted_report_in_band_and_still_deliver(self):
        for code, needle in ((409, "content conflicts with the recorded upload"),
                             (423, "room is encrypted"),
                             (429, "HTTP 429")):
            with self.subTest(code=code):
                mod = _load(self.ws)
                tid = f"task-{code}"
                mod._write_task(self._signal_task(tid))
                fpath = self._attachment()
                self._result(mod, tid, f"x [file: {fpath}]")
                posts: list = []

                def one(method, path, payload=None, timeout=35, _c=code):
                    posts.append((path, payload))
                    if "/media" in path:
                        raise _http(_c)
                    return {}

                inflight = {tid}
                with patch.object(mod, "_req", side_effect=one):
                    mod._post_ready_results(inflight)
                body = [p for p in posts if p[0] == "/v1/results"][0][1]["body"]
                self.assertIn(f"[attachment not sent: {fpath} ({needle}", body)
                self.assertNotIn(tid, inflight, "a reported refusal still delivers")

    # -- 7. instance isolation ---------------------------------------------- #

    def test_identical_wire_ids_in_two_instances_never_collide(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            alpha = _load(ws, instance="alpha")
            beta = _load(ws, instance="beta")
            self.assertNotEqual(alpha.TASK_MEDIA_FILE, beta.TASK_MEDIA_FILE)
            a_task = self._signal_task("task-dup")
            b_task = dict(a_task, thread_root="$beta:server")
            with patch.object(alpha, "_req", return_value={}):
                alpha._write_task(a_task)
            with patch.object(beta, "_req", return_value={}):
                beta._write_task(b_task)
            self.assertEqual(alpha._load_task_media()["task-dup"]["thread_root"],
                             "$root:server")
            self.assertEqual(beta._load_task_media()["task-dup"]["thread_root"],
                             "$beta:server")
            self.assertTrue((ws / "tasks" / "task-alpha~task-dup.txt").exists())
            self.assertTrue((ws / "tasks" / "task-beta~task-dup.txt").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
