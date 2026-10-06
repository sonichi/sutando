#!/usr/bin/env python3
"""health_snapshot: per-agent verdicts from state files, and the agent-api /health route."""
from __future__ import annotations

import http.server
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import health_snapshot as hs  # noqa: E402
import runtime_observation as ro  # noqa: E402

NOW = 1_790_000_000.0
HOST = "test-host"
WID = "40659240fd884f63bcd19fa684b451f1"


class Workspace:
    def __init__(self, root: Path):
        self.root = root
        (root / "state").mkdir(parents=True)

    def json(self, rel: str, value, age: float = 0.0):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        os.utime(path, (NOW - age, NOW - age))
        return path

    def touch(self, rel: str, age: float = 0.0):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        os.utime(path, (NOW - age, NOW - age))

    def lines(self, rel: str, rows):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def supervisor(self, state, kind=None, session="sutando-core", name="core-supervisor.json", age=0.0):
        self.json(f"state/{name}", {"state": state, "detail": "", "prompt": None, "kind": kind,
                                    "session": session}, age)

    def worker(self, state="live", label=None):
        self.json("state/roster.json", {"version": 1, "workers": {WID: {"state": state, "label": label or WID}},
                                        "bindings": {}})


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ws = Workspace(Path(self._tmp.name) / "ws")
        self._host = mock.patch.object(hs, "_host_label", return_value=HOST)
        self._host.start()

    def tearDown(self):
        self._host.stop()
        self._tmp.cleanup()

    def snap(self, **kw):
        return hs.snapshot(self.ws.root, now=NOW, **kw)

    def core(self, **kw):
        return next(a for a in self.snap(**kw)["agents"] if a["id"] == "core")


class CoreVerdicts(Base):
    def test_empty_workspace_is_unknown_not_healthy(self):
        out = self.snap()
        self.assertEqual(out["overall"], "unknown")
        self.assertEqual((out["agents"][0]["motion"], out["agents"][0]["condition"]), ("unknown", "unknown"))

    def test_idle_ready_with_fresh_heartbeat_is_idle_healthy(self):
        self.ws.supervisor("idle-ready")
        self.ws.touch(f"state/cores/{HOST}.alive", age=10)
        c = self.core()
        self.assertEqual((c["motion"], c["condition"], c["reason"]), ("idle", "healthy", None))
        self.assertEqual(self.snap()["overall"], "ok")

    def test_logged_out_is_abnormal_needs_login_since_the_state_change(self):
        self.ws.supervisor("logged-out", age=40)
        c = self.core()
        self.assertEqual((c["motion"], c["condition"], c["reason"]), ("idle", "abnormal", "needs-login"))
        self.assertEqual(c["since"], NOW - 40)
        self.assertEqual(self.snap()["overall"], "attention")

    def test_blocked_human_reason_is_the_gate_kind(self):
        self.ws.supervisor("blocked-human", kind="permission")
        self.assertEqual(self.core()["reason"], "permission")
        self.ws.supervisor("blocked-human", kind="unknown")
        self.assertEqual(self.core()["reason"], "awaiting-input")

    def test_stale_heartbeat_is_offline_but_a_missing_one_is_no_opinion(self):
        self.ws.supervisor("idle-ready")
        self.assertEqual(self.core()["condition"], "healthy")
        self.ws.touch(f"state/cores/{HOST}.alive", age=hs.HEARTBEAT_STALE_S + 5)
        c = self.core()
        self.assertEqual((c["motion"], c["condition"], c["reason"]), ("unknown", "abnormal", "offline"))

    def test_fresh_running_self_report_is_moving_and_a_stale_one_is_not(self):
        self.ws.supervisor("idle-ready")
        self.ws.json("state/core-status.json", {"status": "running", "ts": NOW - 20})
        self.assertEqual(self.core()["motion"], "moving")
        self.ws.json("state/core-status.json", {"status": "running", "ts": NOW - 600})
        self.assertEqual(self.core()["motion"], "idle")

    def test_live_activity_is_moving_while_everything_else_says_idle(self):
        self.ws.supervisor("idle-ready")
        self.ws.json("state/core-status.json", {"status": "idle", "ts": NOW - 5})
        self.ws.lines("state/agent-activity.jsonl", [
            {"ts": NOW - 30, "kind": "processing", "line": "picked up", "task": {"id": "task-a"}},
        ])
        self.assertEqual(self.core()["motion"], "moving")

    def test_a_written_or_archived_result_ends_the_task_without_a_done_row(self):
        self.ws.supervisor("idle-ready")
        self.ws.lines("state/agent-activity.jsonl", [
            {"ts": NOW - 10, "kind": "processing", "task": {"id": "task-a"}}])
        self.assertEqual(self.core()["motion"], "moving")
        self.ws.touch("results/task-a.txt")
        self.assertEqual(self.core()["motion"], "idle")
        (self.ws.root / "results" / "task-a.txt").unlink()
        self.ws.touch("results/archive/task-a-1790000000.txt")
        self.assertEqual(self.core()["motion"], "idle")

    def test_every_archive_layout_ends_the_task_and_a_longer_id_does_not(self):
        layouts = ["results/task-a.txt", "results/archive/task-a-1790000000.txt",
                   "results/archive/task-a.txt", "results/archive/2026-09/task-a.txt",
                   "results/archive/2026-09/task-a-1790000000.txt"]
        self.ws.supervisor("idle-ready")
        self.ws.lines("state/agent-activity.jsonl", [
            {"ts": NOW - 10, "kind": "processing", "task": {"id": "task-a"}}])
        for rel in layouts:
            with self.subTest(layout=rel):
                self.ws.touch(rel)
                self.assertEqual(self.core()["motion"], "idle")
                (self.ws.root / rel).unlink()
        self.ws.touch("results/archive/2026-09/task-ab-1790000000.txt")
        self.assertEqual(self.core()["motion"], "moving")

    def test_finished_or_old_activity_is_not_moving(self):
        self.ws.supervisor("idle-ready")
        self.ws.lines("state/agent-activity.jsonl", [
            {"ts": NOW - 30, "kind": "processing", "task": {"id": "task-a"}},
            {"ts": NOW - 20, "kind": "done", "done": True, "task": {"id": "task-a"}},
            {"ts": NOW - hs.ACTIVITY_LIVE_S - 60, "kind": "processing", "task": {"id": "task-b"}},
        ])
        self.assertEqual(self.core()["motion"], "idle")


class CliWedge(Base):
    def _window(self, patterns, abnormal, last_age=0.0, static=False):
        rows = []
        for i, age in enumerate((last_age + 120, last_age + 60, last_age)):
            rows.append({"ts": NOW - age, "state": "s" if static else f"s{i}",
                         "raw_state": "r" if static else f"r{i}", "patterns": patterns, "abnormal": abnormal})
        self.ws.lines("state/cli-wedge/window.jsonl", rows)

    def test_retry_text_on_a_moving_pane_is_moving_abnormal(self):
        self.ws.supervisor("running")
        self._window(["retrying"], [])
        c = self.core()
        self.assertEqual((c["motion"], c["condition"], c["reason"]), ("moving", "abnormal", "retry-loop"))

    def test_a_limit_on_a_still_pane_names_the_limit(self):
        self.ws.supervisor("idle-ready")
        self._window([], ["quota-limit"], static=True)
        c = self.core()
        self.assertEqual((c["motion"], c["condition"], c["reason"]), ("idle", "abnormal", "quota-limit"))

    def test_a_working_reading_older_than_the_motion_window_is_not_moving(self):
        self.ws.supervisor("idle-ready")
        self._window([], [], last_age=10)
        self.assertEqual(self.core()["motion"], "moving")
        self._window([], [], last_age=hs.WEDGE_MOTION_FRESH_S + 40)
        c = self.core()
        self.assertEqual((c["motion"], c["condition"]), ("idle", "healthy"))

    def test_a_still_clean_pane_is_idle_healthy(self):
        self._window([], [], static=True)
        c = self.core()
        self.assertEqual((c["motion"], c["condition"]), ("idle", "healthy"))

    def test_other_abnormal_text_names_the_first_pattern(self):
        self._window([], ["needs-login"], static=True)
        c = self.core()
        self.assertEqual((c["motion"], c["condition"], c["reason"]), ("idle", "abnormal", "needs-login"))

    def test_a_single_sample_is_no_opinion(self):
        self.ws.lines("state/cli-wedge/window.jsonl", [
            {"ts": NOW, "state": "s", "raw_state": "r", "patterns": [], "abnormal": []}])
        self.assertIsNone(self.core(view="full")["sources"]["cli_wedge"]["opinion"])

    def test_an_old_window_gives_no_opinion(self):
        self.ws.supervisor("idle-ready")
        self._window(["retrying"], [], last_age=hs.WEDGE_STALE_S + 60)
        self.assertEqual(self.core()["condition"], "healthy")

    def test_supervisor_reason_wins_over_the_wedge_reason(self):
        self.ws.supervisor("logged-out")
        self._window(["retrying"], [])
        self.assertEqual(self.core()["reason"], "needs-login")


class EdgeInputs(Base):
    def test_an_unknown_supervisor_state_gives_no_opinion(self):
        self.ws.supervisor("some-future-state")
        self.assertEqual(self.core()["condition"], "unknown")

    def test_a_path_outside_the_workspace_is_reported_by_name(self):
        self.assertEqual(hs._rel(Path("/elsewhere/core-status.json"), self.ws.root), "core-status.json")

    def test_an_empty_or_unreadable_window_gives_no_opinion(self):
        self.ws.supervisor("idle-ready")
        self.ws.lines("state/cli-wedge/window.jsonl", [])
        self.assertIsNone(self.core(view="full")["sources"]["cli_wedge"]["opinion"])
        self.ws.lines("state/cli-wedge/window.jsonl", [{"ts": NOW, "state": "s", "patterns": []}])
        with mock.patch.object(hs.cli_wedge, "classify_window", side_effect=RuntimeError("boom")):
            self.assertIsNone(self.core(view="full")["sources"]["cli_wedge"]["opinion"])

    def test_malformed_activity_rows_are_skipped(self):
        self.ws.supervisor("idle-ready")
        path = self.ws.root / "state" / "agent-activity.jsonl"
        path.write_text("not json\n" + json.dumps({"ts": NOW - 5, "kind": "processing"}) + "\n"
                        + json.dumps({"ts": "late", "task": {"id": "task-x"}}) + "\n")
        self.assertEqual(self.core()["motion"], "idle")

    def test_cli_main_prints_the_snapshot(self):
        self.ws.supervisor("idle-ready")
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(hs.main(["--workspace", str(self.ws.root), "--agent", "core"]), 0)
        self.assertEqual(json.loads(out.getvalue())["agents"][0]["id"], "core")


class Workers(Base):
    def test_live_worker_with_its_seat_supervisor(self):
        self.ws.worker(label="Browser Debug")
        self.ws.supervisor("idle-ready", session=f"sutando-worker-{WID}",
                           name=f"core-supervisor.sutando-worker-{WID}.json")
        w = self.snap(agent=WID)["agents"]
        self.assertEqual(len(w), 1)
        self.assertEqual((w[0]["label"], w[0]["motion"], w[0]["condition"]), ("Browser Debug", "idle", "healthy"))

    def test_a_stale_watcher_beat_is_offline_whatever_the_supervisor_file_last_said(self):
        self.ws.worker()
        self.ws.supervisor("idle-ready", session=f"sutando-worker-{WID}",
                           name=f"core-supervisor.sutando-worker-{WID}.json", age=60)
        self.ws.touch(f"state/watchers/{WID}.alive", age=10)
        self.assertEqual(self.snap(agent="workers")["agents"][0]["condition"], "healthy")
        self.ws.touch(f"state/watchers/{WID}.alive", age=hs.HEARTBEAT_STALE_S + 30)
        w = self.snap(agent="workers")["agents"][0]
        self.assertEqual((w["motion"], w["condition"], w["reason"]), ("unknown", "abnormal", "offline"))
        self.assertEqual(w["since"], NOW - hs.HEARTBEAT_STALE_S - 30)

    def _incarnation(self, started_ago):
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - started_ago))
        self.ws.json(f"state/workers/{WID}/current.json", {"incarnation_id": "b"})
        self.ws.json(f"state/workers/{WID}/incarnations.json",
                     {"incarnations": [{"incarnation_id": "b", "started_at": started}]})

    def test_a_seat_the_watcher_saw_end_is_crashed_while_the_inbox_beat_is_fresh(self):
        self.ws.worker()
        self._incarnation(60)
        self.ws.touch(f"state/watchers/{WID}.alive", age=5)
        self.ws.supervisor("crashed", session=f"sutando-worker-{WID}",
                           name=f"core-supervisor.sutando-worker-{WID}.json", age=3)
        w = self.snap(agent="workers")["agents"][0]
        self.assertEqual((w["alive"], w["condition"], w["reason"]), (False, "abnormal", "crashed"))

    def test_a_crashed_verdict_of_unknown_incarnation_cannot_override_a_fresh_beat(self):
        self.ws.worker()
        self.ws.touch(f"state/watchers/{WID}.alive", age=5)
        self.ws.supervisor("crashed", session=f"sutando-worker-{WID}",
                           name=f"core-supervisor.sutando-worker-{WID}.json", age=3)
        w = self.snap(agent="workers")["agents"][0]
        self.assertEqual((w["alive"], w["condition"], w["reason"]), (True, "abnormal", "crashed"))

    def test_a_core_the_supervisor_saw_crash_is_not_alive_while_its_beat_is_fresh(self):
        self.ws.touch(f"state/cores/{HOST}.alive", age=5)
        self.ws.supervisor("crashed")
        c = self.core()
        self.assertEqual((c["alive"], c["condition"], c["reason"]), (False, "abnormal", "crashed"))

    def test_a_crashed_core_verdict_a_later_beat_contradicts_is_dropped(self):
        self.ws.supervisor("crashed", age=600)
        self.ws.json(f"state/cores/{HOST}.alive", {"pid": 4242, "heartbeat_pid": 99}, age=5)
        c = self.core(view="full")
        self.assertEqual((c["alive"], c["reason"]), (True, None))
        self.assertTrue(c["sources"]["supervisor"]["value"]["superseded"])

    def test_a_crashed_core_verdict_stands_when_the_beat_saw_no_core_pane(self):
        self.ws.supervisor("crashed", age=600)
        self.ws.json(f"state/cores/{HOST}.alive", {"pid": 99, "heartbeat_pid": 99}, age=5)
        c = self.core()
        self.assertEqual((c["alive"], c["reason"]), (False, "crashed"))

    def test_a_crashed_core_verdict_newer_than_the_beat_stands(self):
        self.ws.json(f"state/cores/{HOST}.alive", {"pid": 4242, "heartbeat_pid": 99}, age=20)
        self.ws.supervisor("crashed", age=3)
        self.assertEqual(self.core()["reason"], "crashed")

    def test_alive_follows_the_beat_before_any_screen_verdict_exists(self):
        self.ws.worker()
        self.assertIsNone(self.snap(agent="workers")["agents"][0]["alive"])
        self.ws.touch(f"state/watchers/{WID}.alive", age=5)
        w = self.snap(agent="workers")["agents"][0]
        self.assertEqual((w["alive"], w["motion"], w["condition"]), (True, "unknown", "unknown"))
        self.ws.touch(f"state/watchers/{WID}.alive", age=hs.HEARTBEAT_STALE_S + 1)
        self.assertIs(self.snap(agent="workers")["agents"][0]["alive"], False)

    def test_core_alive_follows_its_heartbeat(self):
        self.assertIsNone(self.core()["alive"])
        self.ws.touch(f"state/cores/{HOST}.alive", age=5)
        self.assertIs(self.core()["alive"], True)

    def test_a_future_dated_beat_is_offline_too(self):
        self.ws.worker()
        self.ws.touch(f"state/watchers/{WID}.alive", age=-60)
        self.assertEqual(self.snap(agent="workers")["agents"][0]["reason"], "offline")

    def test_a_supervisor_verdict_from_a_previous_incarnation_is_ignored(self):
        self.ws.worker()
        self.ws.touch(f"state/watchers/{WID}.alive", age=5)
        self.ws.supervisor("idle-ready", session=f"sutando-worker-{WID}",
                           name=f"core-supervisor.sutando-worker-{WID}.json", age=450)
        self.ws.json(f"state/workers/{WID}/current.json", {"incarnation_id": "b"})
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 60))
        self.ws.json(f"state/workers/{WID}/incarnations.json", {"incarnations": [
            {"incarnation_id": "a", "started_at": "2026-09-01T00:00:00Z"},
            {"incarnation_id": "b", "started_at": started}]})
        w = self.snap(agent="workers", view="full")["agents"][0]
        self.assertEqual((w["alive"], w["condition"]), (True, "unknown"))
        self.assertTrue(w["sources"]["supervisor"]["value"]["previous_run"])
        self.ws.supervisor("idle-ready", session=f"sutando-worker-{WID}",
                           name=f"core-supervisor.sutando-worker-{WID}.json", age=10)
        self.assertEqual(self.snap(agent="workers")["agents"][0]["condition"], "healthy")

    def test_the_seat_file_matches_the_worker_id_exactly(self):
        self.ws.worker()
        self.ws.supervisor("logged-out", session=f"sutando-worker-x{WID}",
                           name=f"core-supervisor.sutando-worker-x{WID}.json")
        self.assertEqual(self.snap(agent="workers")["agents"][0]["condition"], "unknown")

    def test_raw_id_label_reads_as_no_label(self):
        self.ws.worker()
        self.assertIsNone(self.snap(agent="workers")["agents"][0]["label"])

    def test_escalated_wedge_in_a_fresh_pool_sample_is_abnormal(self):
        self.ws.worker()
        self.ws.json("state/pool-supervision.json", {"last_sample_at": NOW - 60, "workers": {
            WID: {"wedge_escalated": True, "wedge_first_detected_at": NOW - 400}}})
        w = self.snap(agent="workers")["agents"][0]
        self.assertEqual((w["condition"], w["reason"], w["since"]), ("abnormal", "wedged", NOW - 400))

    def test_a_dead_worker_the_pool_gave_up_on_reads_not_answering_not_offline(self):
        self.ws.worker()
        self.ws.touch(f"state/watchers/{WID}.alive", age=hs.HEARTBEAT_STALE_S + 30)
        self.ws.json("state/pool-supervision.json", {"last_sample_at": NOW - 60, "workers": {
            WID: {"consecutive": 351, "escalated": True, "first_detected_at": NOW - 600}}})
        w = self.snap(agent="workers")["agents"][0]
        self.assertEqual((w["alive"], w["motion"], w["condition"], w["reason"], w["since"]),
                         (False, "unknown", "abnormal", "not-answering", NOW - 600))

    def test_a_given_up_worker_without_a_first_detection_takes_the_beats_since(self):
        self.ws.worker()
        self.ws.touch(f"state/watchers/{WID}.alive", age=hs.HEARTBEAT_STALE_S + 30)
        self.ws.json("state/pool-supervision.json", {"last_sample_at": NOW - 60, "workers": {
            WID: {"consecutive": 9, "escalated": True}}})
        w = self.snap(agent="workers")["agents"][0]
        self.assertEqual((w["reason"], w["since"]), ("not-answering", NOW - hs.HEARTBEAT_STALE_S - 30))

    def test_a_fresh_pool_sample_without_escalation_gives_no_opinion(self):
        self.ws.worker()
        self.ws.json("state/pool-supervision.json", {"last_sample_at": NOW - 60, "workers": {
            WID: {"consecutive": 1}}})
        self.assertIsNone(self.snap(agent="workers", view="full")["agents"][0]["sources"]["pool"]["opinion"])

    def test_a_stale_pool_sample_gives_no_opinion(self):
        self.ws.worker()
        self.ws.json("state/pool-supervision.json", {"last_sample_at": NOW - hs.POOL_STALE_S - 1, "workers": {
            WID: {"escalated": True}}})
        self.assertEqual(self.snap(agent="workers")["agents"][0]["condition"], "unknown")

    def test_roster_state_other_than_live_is_abnormal_and_retired_is_hidden(self):
        self.ws.worker(state="recovering")
        self.assertEqual(self.snap(agent="workers")["agents"][0]["reason"], "recovering")
        self.ws.worker(state="retired")
        self.assertEqual(self.snap(agent="workers")["agents"], [])

    def test_activity_for_a_delivered_task_belongs_to_the_worker(self):
        self.ws.worker()
        self.ws.supervisor("idle-ready")
        self.ws.touch(f"deliveries/{WID}/task-w.txt")
        self.ws.lines("state/agent-activity.jsonl", [
            {"ts": NOW - 10, "kind": "processing", "task": {"id": "task-w"}}])
        out = {a["id"]: a for a in self.snap()["agents"]}
        self.assertEqual(out[WID]["motion"], "moving")
        self.assertEqual(out["core"]["motion"], "idle")

    def test_agent_filters(self):
        self.ws.worker()
        self.assertEqual([a["id"] for a in self.snap(agent="core")["agents"]], ["core"])
        self.assertEqual([a["id"] for a in self.snap(agent="workers")["agents"]], [WID])
        self.assertEqual([a["id"] for a in self.snap(agent="all")["agents"]], ["core", WID])
        self.assertEqual(self.snap(agent="nope")["agents"], [])


class Views(Base):
    def test_summary_carries_no_paths_or_sources(self):
        self.ws.supervisor("blocked-human", kind="permission")
        self.ws.worker()
        text = json.dumps(self.snap(view="summary"))
        self.assertNotIn("sources", text)
        self.assertNotIn(str(self.ws.root), text)
        self.assertNotIn("state/", text)

    def test_full_lists_every_source_with_workspace_relative_paths(self):
        self.ws.supervisor("idle-ready", age=12)
        core = self.core(view="full")
        self.assertEqual(set(core["sources"]), {"supervisor", "observation", "cli_wedge", "heartbeat",
                                           "activity", "self_report"})
        self.assertEqual(core["sources"]["supervisor"]["path"], "state/core-supervisor.json")
        self.assertEqual(core["sources"]["supervisor"]["age_s"], 12.0)
        self.assertNotIn(str(self.ws.root), json.dumps(core))

    def test_unknown_view_is_refused(self):
        with self.assertRaises(ValueError):
            self.snap(view="everything")


class InstanceSessionSuspended(Base):
    ME = "@mark-desktop.agent:ag2.space"

    def lane(self, name="gateway-status.json", agent_id=ME, connected=True, age=5.0, last_ok=True):
        self.ws.json(f"state/{name}", {"connected": connected, "ts": NOW - age,
                                       "last_ok_ts": NOW - age if last_ok else None, "agent_id": agent_id})

    def test_instance_is_the_identity_the_serving_lane_signed_in_as(self):
        self.lane()
        self.assertEqual(self.snap()["instance"], self.ME)

    def test_a_parked_or_stale_lane_does_not_name_the_instance(self):
        self.lane()
        self.lane("gateway-status.dev.json", agent_id="@old:dev.ag2.space", age=600)
        self.lane("gateway-status.local.json", agent_id="@down:ag2.space", connected=False)
        self.assertEqual(self.snap()["instance"], self.ME)

    def test_no_serving_lane_or_no_identity_is_null(self):
        self.assertIsNone(self.snap()["instance"])
        self.lane(last_ok=False)
        self.assertIsNone(self.snap()["instance"])
        self.lane(agent_id=None)
        self.assertIsNone(self.snap()["instance"])

    def test_two_serving_lanes_that_disagree_name_no_instance(self):
        self.lane()
        self.lane("gateway-status.dev.json", agent_id="@mark-dev:dev.ag2.space")
        self.assertIsNone(self.snap()["instance"])

    def test_core_session_comes_from_its_beat_then_its_supervisor(self):
        self.assertIsNone(self.core()["session"])
        self.ws.supervisor("idle-ready", session="sutando-core")
        self.assertEqual(self.core()["session"], "sutando-core")
        self.ws.json(f"state/cores/{HOST}.alive", {"session": "sutando-core-2"})
        self.assertEqual(self.core()["session"], "sutando-core-2")

    def test_worker_session_comes_from_its_seat_file_or_is_null(self):
        self.ws.worker()
        worker = lambda: next(a for a in self.snap()["agents"] if a["id"] == WID)  # noqa: E731
        self.assertIsNone(worker()["session"])
        self.ws.supervisor("idle-ready", session=f"sutando-worker-{WID}",
                           name=f"core-supervisor.sutando-worker-{WID}.json")
        self.assertEqual(worker()["session"], f"sutando-worker-{WID}")

    def test_a_worker_the_suspension_took_down_is_not_alive_whatever_its_files_say(self):
        self.ws.worker()
        self.ws.touch(f"state/watchers/{WID}.alive", age=5)
        self.ws.supervisor("idle-ready", session=f"sutando-worker-{WID}",
                           name=f"core-supervisor.sutando-worker-{WID}.json", age=10)
        self.assertEqual(self.snap(agent="workers")["agents"][0]["condition"], "healthy")
        self.ws.json("state/pool-suspended", {"reason": "app-quit", "at": NOW - 3, "stopped": [WID]})
        w = self.snap(agent="workers")["agents"][0]
        self.assertEqual((w["alive"], w["motion"], w["condition"], w["reason"], w["since"]),
                         (False, "unknown", "unknown", "suspended", NOW - 3))
        self.ws.json("state/pool-suspended", {"reason": "app-quit", "at": NOW - 3, "stopped": []})
        self.assertEqual(self.snap(agent="workers")["agents"][0]["condition"], "healthy")

    def test_suspended_reads_the_pool_marker(self):
        self.assertIsNone(self.snap()["suspended"])
        self.ws.json("state/pool-suspended", {"reason": "app-quit", "at": 5, "stopped": [WID]})
        self.assertEqual(self.snap()["suspended"], {"reason": "app-quit", "at": 5})
        (self.ws.root / "state" / "pool-suspended").write_text("app-quit 5\n")
        self.assertEqual(self.snap()["suspended"], {"reason": "app-quit 5", "at": None})
        (self.ws.root / "state" / "pool-suspended").write_text("")
        self.assertEqual(self.snap()["suspended"], {"reason": "suspended", "at": None})


def _load_agent_api():
    spec = importlib.util.spec_from_file_location("agent_api_health", REPO / "src" / "agent-api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SESSION = f"sutando-worker-{WID}"


class Observation(Base):
    def obs(self, seat="core", session="sutando-core", **over):
        rec = {"schema": 1, "observer": "obs", "observer_version": "0.3.0", "observer_id": "a" * 16,
               "observer_started_at": NOW - 600, "seat": seat, "session": session, "claude_session_id": None,
               "seq": 4, "changed_at": NOW - 10, "condition_since": None, "last_success_at": None,
               "heartbeat_at": NOW - 3, "phase": "idle", "motion": "idle", "condition": "healthy", "reason": None}
        self.assertTrue(ro.write({**rec, **over}, self.ws.root))

    def worker_seat(self, state="idle-ready", age=0.0):
        self.ws.worker()
        self.ws.supervisor(state, session=SESSION, name=f"core-supervisor.{SESSION}.json", age=age)

    def worker(self, **kw):
        return self.snap(agent=WID, **kw)["agents"][0]

    def wedge(self, abnormal, patterns=(), static=True, last_age=0.0):
        rows = [{"ts": NOW - a, "state": "s" if static else f"s{i}", "raw_state": "r" if static else f"r{i}",
                 "patterns": list(patterns), "abnormal": list(abnormal)}
                for i, a in enumerate((last_age + 120, last_age + 60, last_age))]
        self.ws.lines("state/cli-wedge/window.jsonl", rows)

    def test_no_file_changes_no_verdict(self):
        self.ws.supervisor("logged-out", age=40)
        self.wedge(["quota-limit"])
        self.worker_seat("hung", age=30)
        out = self.snap(view="full")
        for agent in out["agents"]:
            self.assertEqual(agent["sources"]["observation"],
                             {"path": f"state/runtime-observations/{agent['id'] if agent['id'] == 'core' else WID}.json",
                              "age_s": None, "value": None, "opinion": None})
            bare = {k: v for k, v in agent["sources"].items() if k != "observation"}
            base = {k: v for k, v in agent.items() if k not in ("sources", "motion", "condition", "reason", "since")}
            summary = {k: v for k, v in agent.items() if k != "sources"}
            self.assertEqual(hs._verdict(base, bare), summary)
        core = out["agents"][0]
        self.assertEqual((core["condition"], core["reason"], core["since"]), ("abnormal", "needs-login", NOW - 40))
        self.assertEqual(self.worker()["reason"], "hung")

    def test_valid_healthy_idle(self):
        self.obs(last_success_at=NOW - 20)
        c = self.core(view="full")
        self.assertEqual((c["motion"], c["condition"], c["reason"]), ("idle", "healthy", None))
        self.assertEqual(c["sources"]["observation"]["value"], {
            "phase": "idle", "observer": "obs", "observer_version": "0.3.0", "seq": 4,
            "heartbeat_age_s": 3.0, "last_success_age_s": 20.0})
        self.assertEqual(c["sources"]["observation"]["age_s"], 3.0)

    def test_valid_abnormal_needs_login_names_reason_since_condition_since(self):
        self.obs(condition="abnormal", reason="needs-login", phase="failed", condition_since=NOW - 50)
        c = self.core()
        self.assertEqual((c["motion"], c["condition"], c["reason"], c["since"]),
                         ("idle", "abnormal", "needs-login", NOW - 50))

    def test_unknown_motion_and_condition_give_no_opinion_fields(self):
        self.obs(motion="unknown", condition="unknown", phase="unknown")
        self.assertEqual(self.core()["condition"], "unknown")

    def test_expired_lease_is_ignored(self):
        self.obs(condition="abnormal", reason="needs-login", condition_since=NOW - 50,
                 heartbeat_at=NOW - ro.LEASE_S - 1)
        c = self.core(view="full")
        self.assertEqual(c["condition"], "unknown")
        self.assertIsNone(c["sources"]["observation"]["opinion"])

    def test_future_dated_record_is_ignored(self):
        self.obs(condition="abnormal", reason="needs-login", heartbeat_at=NOW + 30)
        self.assertEqual(self.core()["condition"], "unknown")

    def test_a_record_from_another_session_is_ignored_when_the_session_is_known(self):
        self.ws.supervisor("idle-ready", session="sutando-core")
        self.obs(session="other-core", condition="abnormal", reason="needs-login")
        self.assertEqual(self.core()["condition"], "healthy")
        self.obs(session="sutando-core", condition="abnormal", reason="needs-login", seq=5,
                 heartbeat_at=NOW - 2)
        self.assertEqual(self.core()["reason"], "needs-login")

    def test_unknown_session_does_not_reject_a_record(self):
        self.obs(session="anything", condition="abnormal", reason="quota-limit")
        self.assertEqual(self.core()["reason"], "quota-limit")

    def test_worker_record_from_a_previous_incarnation_is_ignored(self):
        self.worker_seat()
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 60))
        self.ws.json(f"state/workers/{WID}/current.json", {"incarnation_id": "b"})
        self.ws.json(f"state/workers/{WID}/incarnations.json",
                     {"incarnations": [{"incarnation_id": "b", "started_at": started}]})
        self.obs(WID, SESSION, observer_started_at=NOW - 300, condition="abnormal", reason="needs-login",
                 condition_since=NOW - 100)
        w = self.worker(view="full")
        self.assertEqual(w["condition"], "healthy")
        self.assertEqual(w["sources"]["observation"]["value"], {"previous_run": True})
        self.obs(WID, SESSION, observer_started_at=NOW - 30, observer_id="b" * 16, seq=1,
                 condition="abnormal", reason="needs-login", condition_since=NOW - 20)
        self.assertEqual(self.worker()["reason"], "needs-login")

    def test_worker_observation_without_known_incarnation_still_counts(self):
        self.worker_seat()
        self.obs(WID, SESSION, condition="abnormal", reason="permission", condition_since=NOW - 9)
        w = self.worker()
        self.assertEqual((w["reason"], w["since"]), ("permission", NOW - 9))

    def test_supervisor_logged_out_older_than_last_success_reads_healthy(self):
        self.ws.supervisor("logged-out", age=40)
        self.obs(last_success_at=NOW - 10)
        c = self.core(view="full")
        self.assertEqual((c["condition"], c["reason"]), ("healthy", None))
        self.assertEqual(c["sources"]["supervisor"]["value"]["superseded_by"], "observation")
        self.assertIsNone(c["sources"]["supervisor"]["opinion"])

    def test_supervisor_logged_out_newer_than_last_success_stays_abnormal(self):
        self.ws.supervisor("logged-out", age=5)
        self.obs(last_success_at=NOW - 10)
        c = self.core(view="full")
        self.assertEqual((c["condition"], c["reason"]), ("abnormal", "needs-login"))
        self.assertNotIn("superseded_by", c["sources"]["supervisor"]["value"])

    def test_no_last_success_supersedes_nothing(self):
        self.ws.supervisor("logged-out", age=40)
        self.obs()
        self.assertEqual(self.core()["reason"], "needs-login")

    def test_supervisor_crashed_with_a_healthy_observation_is_still_crashed(self):
        self.ws.supervisor("crashed", age=40)
        self.obs(last_success_at=NOW - 10)
        c = self.core()
        self.assertEqual((c["alive"], c["condition"], c["reason"]), (False, "abnormal", "crashed"))

    def test_hung_gateway_down_and_blocked_human_are_not_superseded(self):
        self.obs(last_success_at=NOW - 10)
        for state in ("hung", "gateway-down"):
            self.ws.supervisor(state, age=40)
            self.assertEqual(self.core()["reason"], state)
        self.ws.supervisor("blocked-human", kind="permission", age=40)
        self.assertEqual(self.core()["reason"], "permission")

    def test_cli_wedge_quota_limit_is_superseded(self):
        self.wedge(["quota-limit"])
        self.obs(last_success_at=NOW - 1)
        c = self.core(view="full")
        self.assertEqual((c["condition"], c["reason"]), ("healthy", None))
        self.assertEqual(c["sources"]["cli_wedge"]["value"]["superseded_by"], "observation")

    def test_cli_wedge_quota_limit_newer_than_success_stands(self):
        self.wedge(["quota-limit"])
        self.obs(last_success_at=NOW - 3600)
        self.assertEqual(self.core()["reason"], "quota-limit")

    def test_retry_loop_is_not_superseded(self):
        self.wedge([], patterns=["retrying"], static=False)
        self.obs(last_success_at=NOW - 1)
        self.assertEqual(self.core()["reason"], "retry-loop")

    def test_a_claim_with_no_time_is_never_superseded(self):
        claim = {"path": "x", "age_s": None, "value": {"kind": "provider-limit"},
                 "opinion": hs._opinion("idle", hs.ABNORMAL, "quota-limit", None)}
        out = hs._supersede({"cli_wedge": claim}, {"last_success_at": NOW - 1}, NOW)
        self.assertEqual(out["cli_wedge"], claim)

    def test_worker_pool_and_roster_states_are_not_superseded(self):
        self.worker_seat()
        self.ws.json("state/pool-supervision.json", {"last_sample_at": NOW - 60, "workers": {
            WID: {"wedge_escalated": True, "wedge_first_detected_at": NOW - 400}}})
        self.obs(WID, SESSION, last_success_at=NOW - 1)
        self.assertEqual(self.worker()["reason"], "wedged")

    def test_worker_supervisor_login_claim_is_superseded(self):
        self.worker_seat("logged-out", age=40)
        self.obs(WID, SESSION, last_success_at=NOW - 10)
        self.assertEqual((self.worker()["condition"], self.worker()["reason"]), ("healthy", None))

    def test_suspended_worker_is_unchanged(self):
        self.worker_seat()
        self.obs(WID, SESSION, last_success_at=NOW - 10)
        self.ws.json("state/pool-suspended", {"reason": "app-quit", "at": NOW - 3, "stopped": [WID]})
        w = self.worker()
        self.assertEqual((w["alive"], w["condition"], w["reason"], w["since"]), (False, "unknown", "suspended", NOW - 3))

    def test_full_view_exposes_no_session_ids_or_absolute_paths(self):
        self.obs(claude_session_id="secret-claude-session", session="sutando-core")
        text = json.dumps(self.core(view="full")["sources"]["observation"])
        self.assertNotIn("secret-claude-session", text)
        self.assertNotIn(self.ws.root.as_posix(), text)
        self.assertNotIn("a" * 16, text)
        self.assertNotIn("sutando-core", text)

    def test_summary_view_has_no_observation_detail(self):
        self.obs()
        self.assertNotIn("observation", json.dumps(self.snap(view="summary")))


class Route(Base):
    @classmethod
    def setUpClass(cls):
        cls.api = _load_agent_api()

    def setUp(self):
        super().setUp()
        self.ws.supervisor("logged-out")
        self._patches = [mock.patch.object(self.api, "WORKSPACE_DIR", self.ws.root),
                         mock.patch.object(self.api, "API_TOKEN", "")]
        for p in self._patches:
            p.start()
        self.server = http.server.HTTPServer(("127.0.0.1", 0), self.api.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for p in self._patches:
            p.stop()
        super().tearDown()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read()), r.headers
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read()), e.headers

    def _direct(self, path, headers=None, token=""):
        """Drive do_GET on the calling thread, where the coverage tracer sees it."""
        h = self.api.Handler.__new__(self.api.Handler)
        h.path, h.headers, h.client_address = path, headers or {}, ("127.0.0.1", 0)
        sent = []
        h.send_private_json = lambda status, body: sent.append((status, body))
        h.send_json = lambda status, body: sent.append((status, body))
        with mock.patch.object(self.api, "API_TOKEN", token):
            h.do_GET()
        return sent[-1]

    def test_direct_summary_full_and_refusals(self):
        status, body = self._direct("/health?agent=core")
        self.assertEqual((status, body["agents"][0]["reason"]), (200, "needs-login"))
        self.assertIn("sources", self._direct("/health?view=full")[1]["agents"][0])
        self.assertEqual(self._direct("/health?view=everything")[0], 400)
        self.assertEqual(self._direct("/health?view=full", headers={"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self._direct("/health", token="secret")[0], 401)
        self.assertEqual(self._direct("/health", headers={"Authorization": "Bearer secret"}, token="secret")[0], 200)

    def test_route_delegates_to_the_snapshot_with_its_query(self):
        with mock.patch.object(self.api.health_snapshot, "snapshot", return_value={"overall": "x"}) as snap:
            status, body, _ = self.get("/health?agent=core&view=full")
        self.assertEqual((status, body), (200, {"overall": "x"}))
        snap.assert_called_once_with(self.ws.root, agent="core", view="full")

    def test_defaults_are_all_and_summary_and_the_body_is_private(self):
        status, body, headers = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["overall"], "attention")
        self.assertEqual(body["agents"][0]["reason"], "needs-login")
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_bad_view_is_400(self):
        status, body, _ = self.get("/health?view=everything")
        self.assertEqual(status, 400)
        self.assertIn("summary", body["error"])

    def test_full_view_refuses_a_browser_origin(self):
        status, _, _ = self.get("/health?view=full", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.get("/health?view=summary", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
