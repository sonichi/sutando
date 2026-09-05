#!/usr/bin/env python3
"""cli_wedge: normalization, novelty, the advisory classifier, the persisted
window, the I/O edge with fakes, and the record/replay/probe CLI end to end."""
import contextlib
import io
import json
import argparse
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import cli_wedge as w  # noqa: E402

IDLE = "❯ \n⏵⏵ bypass permissions on · 1 monitor · esc to interrupt\n"


def idle_with_clock(i):
    return f"❯ \n⏵⏵ bypass permissions on · 12:0{i % 10}:0{i % 6} PM · 1 monitor\n"


def retry_frame(i):
    return f"Connection error. Retrying in {3 * (i % 3)}s… (attempt {i}/10) 04:2{i % 10}:11\n"


def working_frame(i):
    return f"● step: wrote {'abcdefghijklmnopqrstuvwxyz'[i]}.ts\n  editing hunk\n"


class Normalization(unittest.TestCase):
    def test_volatile_fields_collapse_to_one_state(self):
        frames = [idle_with_clock(i) for i in range(12)]
        self.assertEqual(len({w.state_id(f) for f in frames}), 1)

    def test_each_volatile_class_is_replaced(self):
        n = w.normalize("2026-09-05T04:20:01Z 12:34 PM took 3.5s 12.3k tokens 3/10 45% ⠋ loading... ───── attempt 4 run 42")
        for token in ("<ts>", "<clock>", "<dur>", "<tokens>", "<count>", "<pct>", "<spin>", "<dots>", "<rule>", "attempt #"):
            self.assertIn(token, n, token)
        # No blanket digit stripping (owner review): a bare number is content until a trace says otherwise.
        self.assertIn("run 42", n)

    def test_semantic_digits_are_progress_not_noise(self):
        self.assertNotEqual(w.state_id("editing migration_41.sql\n"), w.state_id("editing migration_42.sql\n"))
        self.assertNotEqual(w.state_id("processing shard 17\n"), w.state_id("processing shard 18\n"))
        # …while counters that only say "again" still collapse
        self.assertEqual(w.state_id("attempt 3 of the same call\n"), w.state_id("attempt 4 of the same call\n"))
        self.assertEqual(w.state_id("error at line 183\n"), w.state_id("error at line 184\n"))

    def test_real_content_change_is_a_new_state(self):
        self.assertNotEqual(w.state_id(working_frame(0)), w.state_id(working_frame(1)))

    def test_blank_lines_and_trailing_space_do_not_count(self):
        self.assertEqual(w.state_id("a  \n\n\nb\n"), w.state_id("a\nb"))


class NoveltyStats(unittest.TestCase):
    def test_static(self):
        nov = w.novelty([IDLE] * 5)
        self.assertEqual((nov.sample_count, nov.novel_state_count, nov.static), (5, 1, True))
        self.assertAlmostEqual(nov.novelty_rate, 0.2)

    def test_all_novel(self):
        nov = w.novelty([working_frame(i) for i in range(10)])
        self.assertEqual((nov.novel_state_count, nov.novelty_rate, nov.static), (10, 1.0, False))

    def test_cycling_states_are_counted_once(self):
        frames = [f"state {'ABC'[i % 3]}\n" for i in range(12)]
        nov = w.novelty(frames)
        self.assertEqual(nov.novel_state_count, 3)
        self.assertAlmostEqual(nov.novelty_rate, 0.25)

    def test_empty(self):
        self.assertEqual(w.novelty([]).novelty_rate, 0.0)


class Classifier(unittest.TestCase):
    def test_idle_when_raw_static_and_nothing_outstanding(self):
        v = w.classify([IDLE] * 6, False, 30)
        self.assertEqual((v["kind"], v["warn"], v["raw_static"]), ("idle", False, True))

    def test_case1_is_pure_raw_static_with_work(self):
        low = w.classify([IDLE] * 6, True, 60, "core-status running")
        high = w.classify([IDLE] * 6, True, 900, "core-status running")
        self.assertEqual((low["kind"], low["warn"], low["confidence"]), ("static-with-work", True, "low"))
        self.assertEqual(high["confidence"], "high")
        self.assertIn("core-status running", high["reason"])

    def test_clock_only_pane_is_not_case1(self):
        # Spec: case 1 is pure static, no normalization. A ticking clock is motion.
        frames = [idle_with_clock(i) for i in range(6)]
        v = w.classify(frames, True, 900)
        self.assertNotEqual(v["kind"], "static-with-work")
        self.assertFalse(v["raw_static"])
        self.assertTrue(v["clock_only"])
        # A clock-only pane is ALIVE (Chi): never a warning, with or without work, however long
        for work in (False, True):
            q = w.classify([idle_with_clock(i) for i in range(12)], work, 900)
            self.assertEqual((q["kind"], q["warn"]), ("clock-only", False), work)
        # ...unless retry text says otherwise: counters-only motion WITH retry text is still case 2
        r = w.classify([retry_frame(i) for i in range(12)], True, 60)
        self.assertEqual(r["kind"], "retry-loop")

    def test_quota_limited_pane_is_case2_even_though_it_moves(self):
        # Owner screenshot 2026-09-05 (Claude Code): each scheduled turn ends at once
        # with the same provider message; only the clock and the verb change.
        def frame(i):
            verb = ("Brewed", "Worked", "Sautéed")[i % 3]
            return (
                f"* Running scheduled task (Sep 4 5:{17 + i // 3:02d}pm)\n"
                "  L You've hit your session limit · resets 6pm (America/Los_Angeles)\n"
                "    /usage-credits to finish what you're working on.\n"
                f"* {verb} for 1s · done 5:{17 + i // 3:02d} PM · 1 monitor still running\n"
            )
        v = w.classify([frame(i) for i in range(12)], True, 300)
        # A provider-blocked CLI is its own kind (owner review), not forced into "retry loop".
        self.assertEqual((v["kind"], v["warn"], v["confidence"]), ("provider-limit", True, "high"))
        self.assertIn("quota-limit", v["current_patterns"])
        self.assertFalse(v["raw_static"])  # the pane moved: this is case 2, not case 1

    def test_codex_idle_banner_is_not_a_provider_limit(self):
        # Codex CLI at its idle prompt (chi-air-blue, 2026-09-05): the banner mentions "usage limit"
        # while nothing is limited. Only a HIT (hit / reached / exceeded / limit reached) is the pattern.
        def frame(i):
            return (
                "OpenAI Codex (v0.42)\n"
                "You have 3 usage limit resets available\n"
                f"  {'⠋⠙⠹⠸'[i % 4]} model: gpt-5-codex · /status for details\n"
                "› \n"
            )
        v = w.classify([frame(i) for i in range(20)], False, 60)
        self.assertNotIn("quota-limit", v["matched_patterns"], v)
        self.assertNotEqual(v["kind"], "provider-limit", v)
        # Positive controls: the phrasings that DO mean a limit was hit still match.
        for line in ("You've hit your usage limit · resets 6pm",
                     "You have reached your weekly limit",
                     "Session limit reached. Try again at 6pm",
                     "usage limit exceeded for this plan",
                     "/usage-credits to finish what you're working on."):
            self.assertIn("quota-limit", w.matched_patterns([line]), line)

    def test_a_pattern_in_one_old_sample_does_not_colour_the_window(self):
        # Owner review P1: sample 1 says "command timed out", the rest is a finished, idle pane.
        frames = ["$ run tests\ncommand timed out after 30s\n"] + [IDLE] * 11
        v = w.classify(frames, True, 600)
        self.assertNotEqual(v["kind"], "retry-loop")
        self.assertEqual(v["current_patterns"], [])
        self.assertEqual((v["pattern_sample_count"], v["consecutive_pattern_samples"]), (1, 0))
        self.assertIn("timeout", v["matched_patterns"])  # still reported as evidence, just not deciding
        # the same residue through the persisted window: the trailing static run decides
        entries = [{"ts": 0.0, "state": "t", "raw_state": "t", "patterns": ["timeout"]}] + \
                  [{"ts": 60.0 * i, "state": "i", "raw_state": "i", "patterns": []} for i in range(1, 12)]
        self.assertEqual(w.classify_window(entries, (False, ""), 660.0)["kind"], "idle")

    def test_retry_text_must_be_current_and_recurrent(self):
        # retry-loop = low novelty AND retry text that is current and recurrent
        frames = [retry_frame(0), IDLE, IDLE] + [retry_frame(i) for i in range(9)]
        v = w.classify(frames, True, 60)
        self.assertEqual(v["kind"], "retry-loop")
        self.assertEqual(v["consecutive_pattern_samples"], 9)
        self.assertEqual(v["pattern_sample_count"], 10)
        # a single current sample with retry text is not yet recurrent — low novelty alone
        # is the softer, cause-unknown warning, never "retry loop"
        v2 = w.classify([IDLE] * 11 + [retry_frame(0)], True, 60)
        self.assertNotIn(v2["kind"], ("retry-loop", "provider-limit"))
        self.assertEqual(v2["consecutive_pattern_samples"], 1)

    def test_case2_retry_loop_when_only_counters_move(self):
        v = w.classify([retry_frame(i) for i in range(20)], True, 60)
        self.assertEqual((v["kind"], v["warn"], v["confidence"]), ("retry-loop", True, "high"))
        self.assertIn("retrying", v["matched_patterns"])
        self.assertEqual(v["novel_state_count"], 1)

    def test_case2_retry_loop_over_cycling_states(self):
        frames = [f"rate limit hit, backing off ({'ABC'[i % 3]})\n" for i in range(12)]
        v = w.classify(frames, True, 60)
        self.assertEqual(v["kind"], "retry-loop")
        self.assertIn("rate-limit", v["matched_patterns"])

    def test_retry_text_with_few_samples_is_medium_confidence(self):
        v = w.classify([retry_frame(i) for i in range(4)], True, 90)
        self.assertEqual((v["kind"], v["confidence"]), ("retry-loop", "medium"))

    def test_no_verdict_before_the_run_has_lasted(self):
        # Live witness 2026-09-05: two identical frames one second apart with the word
        # "timeout" in the operator's own shell text read as retry-loop. A second is not evidence.
        v = w.classify([retry_frame(0), retry_frame(0)], True, 1)
        self.assertEqual((v["kind"], v["warn"]), ("unknown", False))
        self.assertIn("too short", v["reason"])
        self.assertEqual(w.classify([IDLE] * 3, True, 30)["kind"], "unknown")          # would warn → too short
        self.assertEqual(w.classify([IDLE] * 3, True, 60)["kind"], "static-with-work")
        self.assertEqual(w.classify([IDLE] * 3, False, 1)["kind"], "idle")               # not a warning: stated

    def test_low_novelty_without_retry_text_is_a_soft_warning_only_with_work(self):
        frames = [f"state {'AB'[i % 2]}\n" for i in range(12)]
        v = w.classify(frames, True, 60)
        self.assertEqual((v["kind"], v["warn"], v["confidence"]), ("low-novelty", True, "low"))
        self.assertEqual(w.classify(frames, False, 60)["kind"], "working")

    def test_working_is_not_a_warning(self):
        v = w.classify([working_frame(i) for i in range(20)], True, 60)
        self.assertEqual((v["kind"], v["warn"]), ("working", False))
        v2 = w.classify([working_frame(i // 3) for i in range(12)], True, 60)
        self.assertEqual((v2["kind"], v2["confidence"]), ("working", "medium"))

    def test_too_few_samples_is_unknown_not_a_warning(self):
        v = w.classify([IDLE], True, 5)
        self.assertEqual((v["kind"], v["warn"], v["confidence"]), ("unknown", False, "none"))

    def test_raw_state_id_differs_where_normalized_does_not(self):
        a, b = idle_with_clock(1), idle_with_clock(2)
        self.assertEqual(w.state_id(a), w.state_id(b))
        self.assertNotEqual(w.raw_state_id(a), w.raw_state_id(b))

    def test_thresholds_are_reported_and_overridable(self):
        v = w.classify([f"state {'AB'[i % 2]}\n" for i in range(6)], True, 60,
                       thresholds={"min_samples": 4, "low_novelty_rate": 0.5})
        self.assertEqual(v["kind"], "low-novelty")
        self.assertEqual(v["thresholds"]["min_samples"], 4)
        # the same frames under the provisional thresholds read as working (2/6 = 0.33 > 0.25)
        self.assertEqual(w.classify([f"state {'AB'[i % 2]}\n" for i in range(6)], True, 60)["kind"], "working")
        self.assertTrue(v["advisory"])
        self.assertIn("not a health guarantee", v["note"])


class IoEdge(unittest.TestCase):
    def test_capture_pane_returns_stdout_on_success_none_otherwise(self):
        ok = w.capture_pane("/s", "core", "tmux", runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="frame\n"))
        bad = w.capture_pane("/s", "core", "tmux", runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))

        def boom(*a, **k):
            raise OSError("no tmux")

        self.assertEqual(ok, "frame\n")
        self.assertIsNone(bad)
        self.assertIsNone(w.capture_pane("/s", "core", "tmux", runner=boom))

    def test_work_outstanding_reads_core_status_and_task_queue(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "state").mkdir()
            (ws / "tasks").mkdir()
            self.assertEqual(w.work_outstanding(ws), (False, ""))
            (ws / "state" / "core-status.json").write_text(json.dumps({"status": "running", "ts": 1000.0}))
            self.assertEqual(w.work_outstanding(ws, now=1012.0), (True, "core-status running (12s old)"))
            # graceful-restart's contract: "running" older than the TTL is a crashed core's last word
            self.assertEqual(w.work_outstanding(ws, now=1000.0 + 901), (False, ""))
            (ws / "state" / "core-status.json").write_text(json.dumps({"status": "running"}))  # no ts: not counted
            self.assertEqual(w.work_outstanding(ws, now=1000.0), (False, ""))
            (ws / "state" / "core-status.json").write_text("{not json")
            (ws / "tasks" / "task-abc.txt").write_text("task: x\n")
            self.assertEqual(w.work_outstanding(ws), (True, "1 queued task(s)"))

    def test_window_persists_and_measures_the_static_run(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "tasks").mkdir()
            (ws / "state").mkdir()
            (ws / "state" / "core-status.json").write_text(json.dumps({"status": "running", "ts": 1490.0}))
            entries = w.append_window(ws, working_frame(0), 1000.0)
            entries = w.append_window(ws, IDLE, 1100.0)
            entries = w.append_window(ws, IDLE, 1400.0)
            self.assertEqual(len(entries), 3)
            v = w.classify_window(entries, w.work_outstanding(ws, now=1500.0), 1500.0)
            # the static run started at 1100 (the working frame before it does not count)
            self.assertEqual(v["duration"], 400.0)
            self.assertEqual(v["kind"], "static-with-work")
            self.assertEqual(v["trailing_static_samples"], 2)
            self.assertEqual(v["sample_count"], 3)  # the run is still reported whole
            self.assertEqual(v["confidence"], "low")  # 2 samples: one gap, however long (TustinOC)
            self.assertEqual((v["observation_runs"], v["median_gap_s"]), (1, 300.0))
            # a clock-only trailing run is NOT a static run (raw ids differ)
            for i in range(3):
                entries = w.append_window(ws, idle_with_clock(i), 1500.0 + i)
            self.assertNotEqual(w.classify_window(entries, w.work_outstanding(ws, now=1600.0), 1600.0)["kind"], "static-with-work")
            # a corrupt line is skipped, not fatal; the cap holds
            with w.window_path(ws).open("a") as fh:
                fh.write("{not json\n")
            for i in range(30):
                entries = w.append_window(ws, working_frame(i % 26), 2000.0 + i, keep=20)
            self.assertEqual(len(entries), 20)
            self.assertEqual(w.classify_window([], (False, ""), 0.0)["kind"], "unknown")

    def test_a_gap_starts_a_new_observation_run(self):
        # Owner review P1: 10:00 pane A, laptop asleep, 18:00 pane A — two glimpses, not 8 hours.
        e = lambda ts: {"ts": ts, "state": "a", "raw_state": "a", "patterns": []}
        entries = [e(36000.0), e(36000.0 + 8 * 3600)]
        v = w.classify_window(entries, (True, "1 queued task(s)"), 36000.0 + 8 * 3600 + 5)
        self.assertEqual((v["kind"], v["observation_runs"], v["run_samples"]), ("unknown", 2, 1))
        self.assertNotEqual(v.get("confidence"), "high")
        # three samples within the continuity limit ARE a run, and the duration is the run's
        entries = [e(0.0), e(600.0), e(1200.0)]
        v = w.classify_window(entries, (True, "1 queued task(s)"), 1230.0)
        self.assertEqual((v["kind"], v["confidence"], v["duration"], v["observation_runs"]), ("static-with-work", "high", 1230.0, 1))
        # a window whose newest sample is itself older than the limit has no current observation
        v = w.classify_window(entries, (True, "1 queued task(s)"), 1200.0 + 5000)
        self.assertEqual(v["kind"], "unknown")
        self.assertIn("no current observation", v["reason"])

    def test_sampling_slower_than_the_continuity_limit_is_named_not_silent(self):
        # TustinOC: at hourly sampling every sample is its own run and case 1 is unreachable;
        # the verdict must say "undetectable here", not "nothing to report".
        e = lambda ts: {"ts": ts, "state": "a", "raw_state": "a", "patterns": []}
        entries = [e(3600.0 * i) for i in range(6)]
        v = w.classify_window(entries, (True, "1 queued task(s)"), 3600.0 * 5 + 10)
        self.assertEqual((v["kind"], v["warn"], v["observation_runs"], v["run_samples"]), ("cadence-too-sparse", False, 6, 1))
        self.assertEqual((v["window_median_gap_s"], v["recent_gap_s"]), (3600.0, 3600.0))
        self.assertIn("cannot be observed at this rate", v["reason"])
        # the same pane sampled inside the limit is a plain case-1 warning
        dense = [e(1800.0 * i) for i in range(6)]
        self.assertEqual(w.classify_window(dense, (True, "x"), 1800.0 * 5 + 10)["kind"], "static-with-work")

    def test_a_cadence_change_is_judged_on_the_recent_gaps_not_the_window_median(self):
        # Codex on 8ada45a: 15 half-hourly samples then 5 hourly ones kept the window median
        # at 1800 s, so the verdict stayed "unknown" for half the window. Recent gaps decide.
        e = lambda ts: {"ts": ts, "state": "a", "raw_state": "a", "patterns": []}
        dense = [e(1800.0 * i) for i in range(15)]
        t0 = dense[-1]["ts"]
        sparse = [e(t0 + 3600.0 * i) for i in range(1, 6)]
        v = w.classify_window(dense + sparse, (True, "x"), sparse[-1]["ts"] + 10)
        self.assertEqual(v["kind"], "cadence-too-sparse")
        self.assertEqual(v["window_median_gap_s"], 1800.0)   # the window still remembers the dense era…
        self.assertEqual(v["recent_gap_s"], 3600.0)          # …but the decision reads the recent one
        self.assertEqual(v["observation_runs"], 6)
        # two hourly samples after the dense era are not yet a cadence (one gap is a gap)
        v2 = w.classify_window(dense + sparse[:2], (True, "x"), sparse[1]["ts"] + 10)
        self.assertEqual(v2["kind"], "unknown")

    def test_rapid_pane_replacements_are_not_a_sparse_cadence(self):
        # Codex on 07e56e5: three singleton runs split by pane identity, 10 s apart, must not
        # read as "samples arrive every 10s, past the 2700s limit".
        e = lambda ts, pane: {"ts": ts, "state": "a", "raw_state": "a", "patterns": [], "pane": pane}
        entries = [e(0.0, "1:1"), e(10.0, "2:2"), e(20.0, "3:3"), e(30.0, "4:4")]
        v = w.classify_window(entries, (True, "x"), 35.0)
        self.assertEqual((v["kind"], v["observation_runs"], v["run_samples"]), ("unknown", 4, 1))
        self.assertEqual(v["recent_gap_s"], 10.0)
        self.assertIn("fewer than 2 samples", v["reason"])

    def test_a_new_pane_identity_starts_a_new_run(self):
        e = lambda ts, pane: {"ts": ts, "state": "a", "raw_state": "a", "patterns": [], "pane": pane}
        entries = [e(0.0, "100:1"), e(60.0, "100:1"), e(120.0, "200:2"), e(180.0, "200:2")]
        v = w.classify_window(entries, (True, "x"), 200.0)
        self.assertEqual((v["observation_runs"], v["run_samples"], v["duration"]), (2, 2, 80.0))
        self.assertEqual(w.observation_runs(entries, 2700)[1][0]["pane"], "200:2")
        # an unknown identity never resets
        entries2 = [e(0.0, "100:1"), {"ts": 60.0, "state": "a", "raw_state": "a", "patterns": []}]
        self.assertEqual(len(w.observation_runs(entries2, 2700)), 1)

    def test_core_target_uses_the_lowest_live_window_index(self):
        run = lambda *a, **k: SimpleNamespace(returncode=0, stdout="1\n2\n")   # base-index 1
        self.assertEqual(w.core_target("/s", "sutando-core", "tmux", runner=run), "=sutando-core:1")
        run0 = lambda *a, **k: SimpleNamespace(returncode=0, stdout="0\n1\n")
        self.assertEqual(w.core_target("/s", "custom", "tmux", runner=run0), "=custom:0")
        self.assertIsNone(w.core_target("/s", "gone", "tmux", runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout="")))
        self.assertIsNone(w.core_target("/s", "s", "tmux", runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="")))

    def test_sampled_from_inside_guard(self):
        ok = lambda *a, **k: SimpleNamespace(returncode=0, stdout="%7:4242\n")
        self.assertIn("TMUX_PANE", w.sampled_from_inside("/s", "=c:1", "tmux", runner=ok, tmux_pane="%7", tmux_env="/s,1,0", ancestors=[1]))
        # Pane ids are per tmux server: the same %7 under another socket, or unbound, is not the target.
        self.assertIsNone(w.sampled_from_inside("/s", "=c:1", "tmux", runner=ok, tmux_pane="%7", tmux_env="/other,1,0", ancestors=[1]))
        self.assertIsNone(w.sampled_from_inside("/s", "=c:1", "tmux", runner=ok, tmux_pane="%7", tmux_env="", ancestors=[1]))
        self.assertTrue(w._same_tmux_server("/tmp/s.sock", "/tmp/s.sock,4242,0"))
        self.assertFalse(w._same_tmux_server("/tmp/s.sock", "/tmp/t.sock,4242,0"))
        self.assertFalse(w._same_tmux_server("/tmp/s.sock", ""))
        self.assertIn("pid chain", w.sampled_from_inside("/s", "=c:1", "tmux", runner=ok, tmux_pane="", ancestors=[99, 4242]))
        self.assertIsNone(w.sampled_from_inside("/s", "=c:1", "tmux", runner=ok, tmux_pane="%8", tmux_env="/s,1,0", ancestors=[99]))
        bad = lambda *a, **k: SimpleNamespace(returncode=1, stdout="")
        self.assertIsNone(w.sampled_from_inside("/s", "=c:1", "tmux", runner=bad, tmux_pane="%7", ancestors=[4242]))
        # The error path must stay falsy: a raising tmux, or an unparsable answer, is unknown provenance, never a skip.
        def boom(*a, **k):
            raise OSError("no tmux")
        self.assertIsNone(w.sampled_from_inside("/s", "=c:1", "tmux", runner=boom, tmux_pane="%7", tmux_env="/s,1,0", ancestors=[4242]))
        junk = lambda *a, **k: SimpleNamespace(returncode=0, stdout="garbage\n")
        self.assertIsNone(w.sampled_from_inside("/s", "=c:1", "tmux", runner=junk, tmux_pane="%7", tmux_env="/s,1,0", ancestors=[4242]))
        self.assertEqual(w._pid_ancestors(pid=777, runner=boom), [777])
        self.assertIn(os.getppid(), w._pid_ancestors())

    def test_pane_identity_probe(self):
        ok = w.pane_identity("/s", "core", "tmux", runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="4242:1788000000\n"))
        self.assertEqual(ok, "4242:1788000000")
        junk = w.pane_identity("/s", "core", "tmux", runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="❯ some pane text"))
        self.assertIsNone(junk)
        self.assertIsNone(w.pane_identity("/s", "core", "tmux", runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout="")))


def fake_tmux(dir_: Path, frames_file: Path) -> Path:
    """A stand-in tmux binary: each capture-pane prints the next frame from a file."""
    script = dir_ / "tmux"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        "if 'list-windows' in sys.argv: print('0'); sys.exit(0)\n"
        "if 'display-message' in sys.argv: print('4242:1788000000'); sys.exit(0)\n"
        f"ff = pathlib.Path({str(frames_file)!r}); idx = pathlib.Path({str(frames_file)!r} + '.idx')\n"
        "frames = ff.read_text().split('\\n===\\n')\n"
        "i = int(idx.read_text()) if idx.exists() else 0\n"
        "idx.write_text(str(i + 1))\n"
        "sys.stdout.write(frames[min(i, len(frames) - 1)])\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


class Identity(unittest.TestCase):
    # Outside Cli: that class pins _local_host_label to 'host' for every probe.

    def test_local_host_label_comes_from_util_paths_or_the_short_hostname(self):
        # Coverage of the resolver itself (the class pins it elsewhere): the shared helper when it
        # imports, the short hostname when it cannot.
        import socket
        self.assertIsInstance(w._local_host_label(), str)
        with patch.dict(sys.modules, {"util_paths": None}):
            self.assertEqual(w._local_host_label(), socket.gethostname().split(".")[0])

    def test_an_unreadable_pane_is_an_unknown_verdict_for_that_target(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"; (ws / "state" / "cores").mkdir(parents=True)
            with patch.object(w, "capture_pane", return_value=None):
                v = w.probe_one(argparse.Namespace(socket="/s", tmux="tmux", workspace=str(ws), work_file=None, work_outstanding=None, now=None, warn_after=None, sample=None), ws, "=core-9:0", "worker")
            self.assertEqual((v["kind"], v["reason"], v["role"]), ("unknown", "pane not readable", "worker"))


class Cli(unittest.TestCase):
    """main() is driven IN-PROCESS so coverage sees it; one subprocess test keeps the entry point honest."""

    def run_main(self, *args):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = w.main(list(args))
        return rc, buf.getvalue()

    def test_record_then_replay_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"
            frames = Path(d) / "frames.txt"
            frames.write_text("\n===\n".join(retry_frame(i) for i in range(12)))
            tmux = fake_tmux(Path(d), frames)
            rc, out = self.run_main("record", "--socket", "/x", "--tmux", str(tmux), "--workspace", str(ws),
                                    "--label", "retry", "--seconds", "5", "--interval", "0", "--max-samples", "12", "--keep-raw")
            self.assertEqual(rc, 0)
            trace = Path(out.strip())
            self.assertTrue(trace.exists() and trace.name.startswith("retry-"))
            lines = [json.loads(l) for l in trace.read_text().splitlines()]
            self.assertEqual(len(lines), 13)  # 12 samples + summary
            self.assertTrue(lines[-1]["summary"])
            self.assertIn("raw", lines[0])
            rc, out = self.run_main("replay", str(trace), "--work-outstanding")
            self.assertEqual(rc, 0)
            # 12 samples at interval 0 span under a second: a warning needs the run to have lasted
            v = json.loads(out)
            self.assertEqual((v["kind"], v["warn"]), ("unknown", False))
            self.assertIn("too short", v["reason"])
            self.assertIn("retrying", v["matched_patterns"])  # the evidence is still there

    def test_probe_persists_a_window_and_reports(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"
            (ws / "tasks").mkdir(parents=True)
            frames = Path(d) / "frames.txt"
            frames.write_text("\n===\n".join([IDLE, IDLE, IDLE]))
            tmux = fake_tmux(Path(d), frames)
            out = None
            for _ in range(3):
                rc, out = self.run_main("probe", "--socket", "/x", "--tmux", str(tmux), "--workspace", str(ws))
                self.assertEqual(rc, 0)
            verdict = json.loads(out)
            self.assertEqual((verdict["kind"], verdict["sample_count"]), ("idle", 3))
            self.assertTrue(w.window_path(ws).exists())

    def test_probe_targets_keep_separate_windows(self):
        # Two worker panes probed alternately each keep their own run; the core's window is untouched.
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"
            frames = Path(d) / "frames.txt"
            frames.write_text("\n===\n".join(working_frame(i) for i in range(6)))
            tmux = fake_tmux(Path(d), frames)
            outs = []
            for target in ("=worker-1:1", "=worker-2:1", "=worker-1:1", "=worker-2:1"):
                rc, out = self.run_main("probe", "--socket", "/s", "--target", target, "--workspace", str(ws), "--tmux", str(tmux))
                self.assertEqual(rc, 0)
                outs.append(json.loads(out))
            self.assertEqual([o["sample_count"] for o in outs], [1, 1, 2, 2], outs)
            self.assertIn("no work signal", outs[0]["work_detail"])
            self.assertFalse(w.window_path(ws).exists(), "an explicit target must not write the core's window")
            self.assertTrue(w.window_path(ws, w.window_slot("/s", "=worker-1:1")).exists())
            self.assertNotEqual(w.window_path(ws, w.window_slot("/s", "=worker-1:1")), w.window_path(ws, w.window_slot("/s", "=worker-2:1")))
            self.assertEqual(w.window_path(ws), w.window_path(ws, None))

    def test_probe_takes_the_callers_work_signal_for_a_worker(self):
        # The deliverer knows what a worker owes; the core's queue does not. A work file keyed by
        # target, or an explicit flag, replaces the core-queue read for explicit targets.
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"
            frames = Path(d) / "frames.txt"
            frames.write_text("\n===\n".join([IDLE] * 8))
            tmux = fake_tmux(Path(d), frames)
            wf = Path(d) / "work.json"
            wf.write_text(json.dumps({"=worker-1:1": {"outstanding": True, "detail": "task-abc handed 40s ago, no result"},
                                      "=worker-2:1": {"outstanding": False}}))
            base = ["probe", "--socket", "/s", "--workspace", str(ws), "--tmux", str(tmux)]
            rc, out = self.run_main(*base, "--target", "=worker-1:1", "--work-file", str(wf))
            v = json.loads(out)
            self.assertEqual((rc, v["work_outstanding"], v["work_detail"], v["target"]), (0, True, "task-abc handed 40s ago, no result", "=worker-1:1"))
            rc, out = self.run_main(*base, "--target", "=worker-2:1", "--work-file", str(wf))
            v = json.loads(out)
            self.assertEqual((v["work_outstanding"], v["work_detail"]), (False, "work file: nothing outstanding"))
            rc, out = self.run_main(*base, "--target", "=worker-9:1", "--work-file", str(wf))
            v = json.loads(out)
            self.assertEqual(v["work_outstanding"], False)
            self.assertIn("no work signal for '=worker-9:1'", v["work_detail"])
            rc, out = self.run_main(*base, "--target", "=worker-9:1", "--work-outstanding")
            self.assertEqual(json.loads(out)["work_outstanding"], True)
            rc, out = self.run_main(*base, "--target", "=worker-9:1", "--no-work")
            v = json.loads(out)
            self.assertEqual((v["work_outstanding"], v["work_detail"]), (False, "caller says nothing outstanding"))
            # a missing or broken work file is a missing signal, never a verdict input
            rc, out = self.run_main(*base, "--target", "=worker-1:1", "--work-file", str(Path(d) / "absent.json"))
            self.assertIn("work file unreadable", json.loads(out)["work_detail"])
            # the core's own pane still reads its own queue (no explicit target)
            (ws / "tasks").mkdir(parents=True, exist_ok=True); (ws / "tasks" / "task-1.txt").write_text("x")
            rc, out = self.run_main(*base)
            self.assertIn("queued task", json.loads(out)["work_detail"])

    def test_a_work_file_never_silences_the_cores_own_queue(self):
        # --work-file keyed for workers plus the core in --targets: the core still reads its queue
        # (no key for it), and a key for the core's own target is honoured when present.
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"
            frames = Path(d) / "frames.txt"
            frames.write_text("\n===\n".join([IDLE] * 8))
            tmux = fake_tmux(Path(d), frames)
            self._alive(ws, "sutando-core")
            (ws / "tasks").mkdir(parents=True, exist_ok=True)
            for i in range(3):
                (ws / "tasks" / f"task-{i}.txt").write_text("x")
            wf = Path(d) / "work.json"
            wf.write_text(json.dumps({"=w1:1": {"outstanding": False}}))
            base = ["probe", "--socket", "/s", "--workspace", str(ws), "--tmux", str(tmux), "--targets", "=w1:1,=sutando-core:0"]
            with patch.object(w, "_local_host_label", return_value="host"):
                rc, out = self.run_main(*base, "--work-file", str(wf))
                by = json.loads(out)["targets"]
                self.assertEqual(by["=sutando-core:0"]["role"], "core")
                self.assertTrue(by["=sutando-core:0"]["work_outstanding"], by["=sutando-core:0"])
                self.assertIn("queued task", by["=sutando-core:0"]["work_detail"])
                self.assertNotIn("None", by["=sutando-core:0"]["work_detail"])
                self.assertEqual(by["=w1:1"]["work_outstanding"], False)
                wf.write_text(json.dumps({"=sutando-core:0": {"outstanding": False, "detail": "deliverer says drained"}}))
                rc, out = self.run_main(*base, "--work-file", str(wf))
                by = json.loads(out)["targets"]
                self.assertEqual((by["=sutando-core:0"]["work_outstanding"], by["=sutando-core:0"]["work_detail"]), (False, "deliverer says drained"))

    def test_probe_targets_gives_one_verdict_per_worker_with_its_own_signal(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"
            frames = Path(d) / "frames.txt"
            frames.write_text("\n===\n".join(working_frame(i) for i in range(8)))
            tmux = fake_tmux(Path(d), frames)
            wf = Path(d) / "work.json"
            wf.write_text(json.dumps({"=w1:1": {"outstanding": True, "detail": "owes task-1"}}))
            base = ["probe", "--socket", "/s", "--workspace", str(ws), "--tmux", str(tmux), "--targets", "=w1:1, =w2:1", "--work-file", str(wf)]
            rc, out = self.run_main(*base)
            v = json.loads(out)
            self.assertEqual((rc, v["socket"], sorted(v["targets"])), (0, "/s", ["=w1:1", "=w2:1"]))
            self.assertEqual((v["targets"]["=w1:1"]["work_outstanding"], v["targets"]["=w1:1"]["work_detail"]), (True, "owes task-1"))
            self.assertFalse(v["targets"]["=w2:1"]["work_outstanding"])
            rc, out = self.run_main(*base)
            v2 = json.loads(out)
            self.assertEqual([v2["targets"][k]["sample_count"] for k in ("=w1:1", "=w2:1")], [2, 2])
            self.assertFalse(w.window_path(ws).exists())
            self.assertTrue(w.window_path(ws, w.window_slot("/s", "=w1:1")).exists() and w.window_path(ws, w.window_slot("/s", "=w2:1")).exists())

    def setUp(self):
        super().setUp()
        p = patch.object(w, "_local_host_label", return_value="host"); p.start(); self.addCleanup(p.stop)

    def _alive(self, ws: Path, session: str, socket: str = "/s") -> None:
        cores = ws / "state" / "cores"; cores.mkdir(parents=True, exist_ok=True)
        (cores / "host.alive").write_text(json.dumps({"host": "host", "socket": socket, "session": session, "schema_version": 4}))

    def test_core_identity_reads_this_hosts_heartbeat_only(self):
        # A shared workspace holds other hosts' heartbeats; a fresher peer record must not become the
        # local core, and with no local record the identity is the configured default, never the env.
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"; cores = ws / "state" / "cores"; cores.mkdir(parents=True)
            (cores / "host.alive").write_text(json.dumps({"host": "host", "socket": "/s", "session": "sutando-core"}))
            (cores / "peer.alive").write_text(json.dumps({"host": "peer", "socket": "/p", "session": "core-9"}))
            os.utime(cores / "host.alive", (1, 1))  # the peer's record is the newer one
            with patch.object(w, "_local_host_label", return_value="host"):
                self.assertEqual(w.core_identity(ws), ("/s", "sutando-core"))
                (cores / "host.alive").unlink()
                with patch.dict(os.environ, {"SUTANDO_TMUX_SESSION": "core-2"}):
                    self.assertEqual(w.core_identity(ws), (None, w.DEFAULT_SESSION))
                (cores / "host.alive").write_text("not json")
                self.assertEqual(w.core_identity(ws), (None, w.DEFAULT_SESSION))

    def test_a_malformed_socket_in_the_heartbeat_is_an_unreadable_record(self):
        # One bad socket must not crash the diagnostic at realpath(), nor cost the core its session.
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"; cores = ws / "state" / "cores"; cores.mkdir(parents=True)
            with patch.object(w, "_local_host_label", return_value="host"):
                for bad in (["/s"], {"path": "/s"}, 7, True):
                    (cores / "host.alive").write_text(json.dumps({"host": "host", "socket": bad, "session": "core-2"}))
                    self.assertEqual(w.core_identity(ws), (None, "core-2"), bad)
                for absent in ({"host": "host", "session": "core-2"}, {"host": "host", "socket": None, "session": "core-2"}, {"host": "host", "socket": "", "session": "core-2"}):
                    (cores / "host.alive").write_text(json.dumps(absent))
                    self.assertEqual(w.core_identity(ws), (None, "core-2"), absent)

    def test_role_comes_from_identity_not_from_how_the_session_was_spelled(self):
        # The heartbeat record names the core's session. Naming that session explicitly is still the
        # core; a worker reached through the environment with no flag is still a worker.
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"; frames = Path(d) / "frames.txt"
            frames.write_text("\n===\n".join(working_frame(i) for i in range(12)))
            tmux = fake_tmux(Path(d), frames)
            self._alive(ws, "sutando-core")
            (ws / "tasks").mkdir(parents=True, exist_ok=True); (ws / "tasks" / "task-1.txt").write_text("x")
            base = ["probe", "--socket", "/s", "--workspace", str(ws), "--tmux", str(tmux)]
            rc, out = self.run_main(*base, "--session", "sutando-core")        # explicit, but it IS the core
            v = json.loads(out)
            self.assertEqual((v["role"], v["work_outstanding"]), ("core", True), v)
            self.assertIn("queued task", v["work_detail"])
            self.assertTrue(w.window_path(ws).exists())
            with patch.dict(os.environ, {"SUTANDO_TMUX_SESSION": "core-2"}):     # env names a WORKER, no flag
                rc, out = self.run_main(*base)
            v = json.loads(out)
            self.assertEqual((v["role"], v["work_outstanding"]), ("worker", False), v)
            self.assertIn("no work signal", v["work_detail"])
            self.assertEqual(v["target"], "=core-2:0")
            rc, out = self.run_main(*base, "--target", "=sutando-core:0")       # explicit target that IS the core
            self.assertEqual(json.loads(out)["role"], "core")
            rc, out = self.run_main(*base, "--session", "sutando-core", "--role", "worker")   # the override wins
            v = json.loads(out); self.assertEqual((v["role"], v["work_outstanding"]), ("worker", False))
            rc, out = self.run_main(*base, "--target", "=core-9:0", "--role", "core")
            self.assertEqual(json.loads(out)["role"], "core")
            # no heartbeat record: the configured default is the core, a named other session a worker,
            # and a session named only by the environment is a worker too (never promoted to core)
            (ws / "state" / "cores" / "host.alive").unlink()
            rc, out = self.run_main(*base); self.assertEqual(json.loads(out)["role"], "core")
            rc, out = self.run_main(*base, "--session", "core-3"); self.assertEqual(json.loads(out)["role"], "worker")
            with patch.dict(os.environ, {"SUTANDO_TMUX_SESSION": "core-2"}):
                rc, out = self.run_main(*base)
            v = json.loads(out); self.assertEqual((v["role"], v["target"]), ("worker", "=core-2:0"), v)

    def test_windows_are_keyed_by_socket_as_well_as_target(self):
        # Two tmux servers on one host can both hold =core-2:1; the same target on two sockets must not
        # share a window (alternating pane identities would split every sample into a new run).
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d) / "ws"; frames = Path(d) / "frames.txt"
            frames.write_text("\n===\n".join(working_frame(i) for i in range(8)))
            tmux = fake_tmux(Path(d), frames)
            counts = []
            for sock in ("/s1", "/s2", "/s1", "/s2"):
                rc, out = self.run_main("probe", "--socket", sock, "--target", "=core-2:1", "--workspace", str(ws), "--tmux", str(tmux))
                counts.append(json.loads(out)["sample_count"])
            self.assertEqual(counts, [1, 1, 2, 2])
            self.assertNotEqual(w.window_slot("/s1", "=core-2:1"), w.window_slot("/s2", "=core-2:1"))
            self.assertEqual(w.window_slot("/s1", "=x"), w.window_slot("/s1", "=x"))
            self.assertTrue(w.window_path(ws, w.window_slot("/s1", "=core-2:1")).exists())
            self.assertTrue(w.window_path(ws, w.window_slot("/s2", "=core-2:1")).exists())
            self.assertFalse(w.window_path(ws).exists())

    def test_work_signal_precedence(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d); wf = Path(d) / "w.json"
            wf.write_text(json.dumps({"outstanding": True, "detail": "flat record"}))
            self.assertEqual(w.work_signal("=t:1", ws, 0.0, work_file=str(wf)), (True, "flat record"))
            self.assertEqual(w.work_signal("=t:1", ws, 0.0, work_file=str(wf), say=False), (False, "caller says nothing outstanding"))
            self.assertEqual(w.work_signal("=t:1", ws, 0.0), (False, "no work signal for an explicit --target"))
            wf.write_text("{not json")
            self.assertIn("unreadable", w.work_signal("=t:1", ws, 0.0, work_file=str(wf))[1])
            # `outstanding` is a JSON boolean or it is no signal: nothing is coerced.
            for val, want in (("false", False), ("true", False), (0, False), (1, False), (None, False), (True, True), (False, False)):
                wf.write_text(json.dumps({"=t:1": {"outstanding": val}}))
                got = w.work_signal("=t:1", ws, 0.0, work_file=str(wf))
                self.assertEqual(got[0], want, (val, got))
                if type(val) is not bool:
                    self.assertIn("is not a boolean", got[1], (val, got))
            wf.write_text(json.dumps({"=t:1": {"detail": "no key"}}))
            self.assertIn("no work signal", w.work_signal("=t:1", ws, 0.0, work_file=str(wf))[1])

    def test_probe_with_unreadable_pane_is_unknown_not_a_warning(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = self.run_main("probe", "--socket", "/x", "--tmux", "/nonexistent/tmux", "--workspace", d)
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out)["kind"], "unknown")

    def test_default_workspace_is_the_configured_one(self):
        # No env fallback: the sanctioned resolver answers (lint-workspace-resolution).
        self.assertIsInstance(w._default_workspace(), Path)
        self.assertNotIn("SUTANDO_WORKSPACE_DIR", (REPO / "src" / "cli_wedge.py").read_text())

    def test_entry_point_runs_as_a_script(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            p.write_text(json.dumps({"ts": 1, "state": "s1", "raw_state": "r1", "patterns": []}) + "\n"
                         + json.dumps({"ts": 2, "state": "s2", "raw_state": "r2", "patterns": []}) + "\n")
            r = subprocess.run([sys.executable, str(REPO / "src" / "cli_wedge.py"), "replay", str(p)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["kind"], "working")

    def test_record_sampler_none_frames_are_skipped(self):
        args = SimpleNamespace(workspace=tempfile.mkdtemp(), label="idle", seconds=3, interval=0, max_samples=5, keep_raw=False)
        t = [0.0]

        def clock():
            t[0] += 1.0
            return t[0]

        calls = [None, IDLE, IDLE]
        path = w.record(args, lambda: calls.pop(0) if calls else IDLE, clock=clock, sleep=lambda s: None)
        lines = [json.loads(l) for l in path.read_text().splitlines()]
        self.assertTrue(lines[-1]["summary"])
        self.assertGreaterEqual(len(lines), 2)
        self.assertNotIn("raw", lines[0])

    def test_replay_skips_corrupt_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            p.write_text("{bad\n" + json.dumps({"ts": 1, "state": "s1", "patterns": []}) + "\n")
            self.assertEqual(w.replay(p)["kind"], "unknown")

    def test_record_stores_no_text_unless_asked(self):
        args = SimpleNamespace(workspace=tempfile.mkdtemp(), label="idle", seconds=3, interval=0, max_samples=3, keep_raw=False)
        t = [0.0]

        def clock():
            t[0] += 1.0
            return t[0]

        path = w.record(args, lambda: IDLE, clock=clock, sleep=lambda s: None)
        first = json.loads(path.read_text().splitlines()[0])
        self.assertEqual(sorted(first), ["patterns", "raw_state", "state", "ts"])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        args2 = SimpleNamespace(**{**vars(args), "keep_normalized": True})
        path2 = w.record(args2, lambda: IDLE, clock=clock, sleep=lambda s: None)
        self.assertIn("normalized", json.loads(path2.read_text().splitlines()[0]))

    def test_replay_classifies_a_hash_only_trace(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.jsonl"
            lines = [json.dumps({"ts": 10.0 * i, "state": "same", "raw_state": f"r{i}", "patterns": ["retrying"]}) for i in range(12)]
            p.write_text("\n".join(lines) + "\n")
            self.assertEqual(w.replay(p, work=True)["kind"], "retry-loop")


class ConcurrentWriters(unittest.TestCase):
    """Two independent writers (the app's health-check and the launchd fallback) must
    not lose each other's sample: the production append_window is the contract."""

    def test_forked_writers_keep_every_sample(self):
        import multiprocessing as mp
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            w.append_window(ws, IDLE, 1.0)
            # Every worker holds the lock only inside append_window; a barrier lines them up
            # at the door so they contend for the same read/modify/write.
            n = 6
            barrier = mp.Barrier(n)

            def worker(i):
                barrier.wait(timeout=20)
                w.append_window(ws, working_frame(i), 10.0 + i)

            ctx = mp.get_context("fork")
            procs = [ctx.Process(target=worker, args=(i,)) for i in range(n)]
            for pr in procs:
                pr.start()
            for pr in procs:
                pr.join(30)
            self.assertEqual([pr.exitcode for pr in procs], [0] * n)
            entries = w.load_window(w.window_path(ws))
            self.assertEqual(len(entries), 1 + n)
            self.assertEqual(sorted(e["ts"] for e in entries), [1.0] + [10.0 + i for i in range(n)])
            self.assertFalse((w.window_path(ws).with_name("window.jsonl.lock")).stat().st_mode & 0o077)

    def test_without_the_lock_the_same_race_loses_a_sample(self):
        # Negative control: the pre-fix shape — load, then append+replace after every
        # sibling has loaded — drops all but one writer's sample.
        import multiprocessing as mp
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            w.append_window(ws, IDLE, 1.0)
            n = 3
            barrier = mp.Barrier(n)

            def racy(i):
                path = w.window_path(ws)
                entries = w.load_window(path)
                barrier.wait(timeout=20)  # everyone has loaded the same 1 entry
                entries.append({"ts": 10.0 + i, "state": "s", "raw_state": "r", "patterns": []})
                w._write_private(path, "".join(json.dumps(e) + "\n" for e in entries))

            ctx = mp.get_context("fork")
            procs = [ctx.Process(target=racy, args=(i,)) for i in range(n)]
            for pr in procs:
                pr.start()
            for pr in procs:
                pr.join(30)
            self.assertEqual(len(w.load_window(w.window_path(ws))), 2)  # 1 + one survivor, not 4


class Confidentiality(unittest.TestCase):
    """The rolling window never carries pane text and every file is owner-only."""

    def test_window_entries_carry_hashes_and_patterns_only(self):
        with tempfile.TemporaryDirectory() as d:
            entries = w.append_window(Path(d), retry_frame(1), 1.0)
            self.assertEqual(sorted(entries[-1]), ["patterns", "raw_state", "state", "ts"])
            text = w.window_path(Path(d)).read_text()
            self.assertNotIn("Retrying", text)
            self.assertNotIn("attempt", text)

    def test_files_are_owner_only_under_a_permissive_umask(self):
        old = os.umask(0o022)
        try:
            with tempfile.TemporaryDirectory() as d:
                w.append_window(Path(d), IDLE, 1.0)
                path = w.window_path(Path(d))
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                # a pre-existing wide mode is normalized on the next write
                os.chmod(path, 0o644)
                w.append_window(Path(d), IDLE, 2.0)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        finally:
            os.umask(old)

    def test_classify_from_ids_matches_classify_from_frames(self):
        frames = [retry_frame(i) for i in range(12)]
        a = w.classify(frames, True, 60)
        b = w.classify_ids([w.state_id(f) for f in frames], False, w.matched_patterns(frames), True, 60)
        self.assertEqual((a["kind"], a["novel_state_count"], a["novelty_rate"]), (b["kind"], b["novel_state_count"], b["novelty_rate"]))


class FailureBoundary(unittest.TestCase):
    """Malformed persisted state is skipped, never raised; unwritable state raises at the
    library edge (the health probe turns that into 'no reading')."""

    def test_valid_json_but_malformed_entries_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = w.window_path(Path(d))
            path.parent.mkdir(parents=True)
            path.write_text("[]\n" + json.dumps({"ts": "not-a-number", "state": "s"}) + "\n" + json.dumps({"ts": 1.0, "state": 5}) + "\n"
                            + json.dumps({"ts": 1.0, "state": "ok", "patterns": []}) + "\n" + "{bad\n")
            entries = w.load_window(path)
            self.assertEqual([e["state"] for e in entries], ["ok"])
            v = w.classify_window([[], {"ts": "x", "state": "s"}, {"ts": 2.0, "state": "ok", "patterns": []}], (False, ""), 3.0)
            self.assertEqual(v["kind"], "unknown")  # one valid entry: too few to compare, no crash

    def test_unwritable_window_raises_at_the_library_edge(self):
        # A read-only dir the process owns is simply re-opened (0700); what cannot be
        # healed is the window path being occupied by a directory — the replace fails.
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            w.window_path(ws).mkdir(parents=True)
            with self.assertRaises(OSError):
                w.append_window(ws, IDLE, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=1)
