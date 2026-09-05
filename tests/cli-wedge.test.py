#!/usr/bin/env python3
"""cli_wedge: normalization, novelty, the advisory classifier, the persisted
window, the I/O edge with fakes, and the record/replay/probe CLI end to end."""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
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
        n = w.normalize("2026-09-05T04:20:01Z 12:34 PM took 3.5s 12.3k tokens 3/10 45% ⠋ loading... ───── run 42")
        for token in ("<ts>", "<clock>", "<dur>", "<tokens>", "<count>", "<pct>", "<spin>", "<dots>", "<rule>", "#"):
            self.assertIn(token, n, token)
        self.assertNotRegex(n, r"\d")

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
        self.assertEqual((v["kind"], v["warn"]), ("retry-loop", True))
        self.assertIn("quota-limit", v["matched_patterns"])
        self.assertFalse(v["raw_static"])  # the pane moved: this is case 2, not case 1

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
        v = w.classify([retry_frame(i) for i in range(4)], True, 10)
        self.assertEqual((v["kind"], v["confidence"]), ("retry-loop", "medium"))

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
            (ws / "state" / "core-status.json").write_text(json.dumps({"status": "running"}))
            self.assertEqual(w.work_outstanding(ws), (True, "core-status running"))
            (ws / "state" / "core-status.json").write_text("{not json")
            (ws / "tasks" / "task-abc.txt").write_text("task: x\n")
            self.assertEqual(w.work_outstanding(ws), (True, "1 queued task(s)"))

    def test_window_persists_and_measures_the_static_run(self):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "tasks").mkdir()
            (ws / "state").mkdir()
            (ws / "state" / "core-status.json").write_text(json.dumps({"status": "running"}))
            entries = w.append_window(ws, working_frame(0), 1000.0)
            entries = w.append_window(ws, IDLE, 1100.0)
            entries = w.append_window(ws, IDLE, 1400.0)
            self.assertEqual(len(entries), 3)
            v = w.classify_window(entries, w.work_outstanding(ws), 1500.0)
            # the static run started at 1100 (the working frame before it does not count)
            self.assertEqual(v["duration"], 400.0)
            self.assertEqual(v["kind"], "static-with-work")
            self.assertEqual(v["trailing_static_samples"], 2)
            self.assertEqual(v["sample_count"], 3)  # the window is still reported whole
            # a clock-only trailing run is NOT a static run (raw ids differ)
            for i in range(3):
                entries = w.append_window(ws, idle_with_clock(i), 1500.0 + i)
            self.assertNotEqual(w.classify_window(entries, w.work_outstanding(ws), 1600.0)["kind"], "static-with-work")
            # a corrupt line is skipped, not fatal; the cap holds
            with w.window_path(ws).open("a") as fh:
                fh.write("{not json\n")
            for i in range(30):
                entries = w.append_window(ws, working_frame(i % 26), 2000.0 + i, keep=20)
            self.assertEqual(len(entries), 20)
            self.assertEqual(w.classify_window([], (False, ""), 0.0)["kind"], "unknown")


def fake_tmux(dir_: Path, frames_file: Path) -> Path:
    """A stand-in tmux binary: each capture-pane prints the next frame from a file."""
    script = dir_ / "tmux"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, pathlib\n"
        f"ff = pathlib.Path({str(frames_file)!r}); idx = pathlib.Path({str(frames_file)!r} + '.idx')\n"
        "frames = ff.read_text().split('\\n===\\n')\n"
        "i = int(idx.read_text()) if idx.exists() else 0\n"
        "idx.write_text(str(i + 1))\n"
        "sys.stdout.write(frames[min(i, len(frames) - 1)])\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


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
            self.assertEqual(json.loads(out)["kind"], "retry-loop")

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
            lines = [json.dumps({"ts": float(i), "state": "same", "raw_state": f"r{i}", "patterns": ["retrying"]}) for i in range(12)]
            p.write_text("\n".join(lines) + "\n")
            self.assertEqual(w.replay(p, work=True)["kind"], "retry-loop")


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
