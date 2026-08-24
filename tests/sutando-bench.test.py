#!/usr/bin/env python3
"""Unit and controller-path tests for scripts/sutando_bench.py."""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import sutando_bench as bench  # noqa: E402


def workspace(root: Path) -> Path:
    (root / "tasks").mkdir(parents=True)
    (root / "results").mkdir()
    return root


def example_run(label="subject", passed=True, latency=10.0, no_response=0):
    rows = [{
        "case_id": "one", "category": "test", "repetition": 1,
        "task_id": "task-one", "status": "completed" if not no_response else "timeout",
        "passed": passed, "latency_ms": latency, "wait_ms": latency,
        "response": "OK" if not no_response else None, "result_path": None,
        "checks": [],
    }]
    return {
        "schema": 1, "kind": "sutando-benchmark-run", "run_id": "run",
        "started_at": "start", "finished_at": "finish",
        "subject": {"label": label, "workspace": "/tmp/ws", "host": "host"},
        "suite": {"name": "test", "description": ""},
        "configuration": {}, "summary": bench.summarize(rows), "cases": rows,
    }


class Responder:
    def __init__(self, ws: Path, responses):
        self.ws = ws
        self.responses = responses
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.stop.set()
        self.thread.join(timeout=1)

    def _run(self):
        seen = set()
        while not self.stop.is_set():
            for path in (self.ws / "tasks").glob("task-bench-*.txt"):
                if path.name in seen:
                    continue
                seen.add(path.name)
                text = path.read_text()
                response = next((answer for needle, answer in self.responses.items()
                                 if needle in text), "UNKNOWN")
                (self.ws / "results" / path.name).write_text(response)
            time.sleep(0.001)


class BenchTests(unittest.TestCase):
    def test_percentile_and_format(self):
        self.assertIsNone(bench.percentile([], 0.5))
        self.assertEqual(bench.percentile([30, 10, 20], 0.5), 20)
        self.assertEqual(bench.percentile([1], 2), 1)
        self.assertEqual(bench._fmt_ms(None), "n/a")
        self.assertEqual(bench._fmt_ms(1.25), "1.2 ms")

    def test_load_suite_validation(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "suite.json"
            path.write_text(json.dumps({"schema": 1, "name": "x", "cases": [{"id": "a", "prompt": "p"}]}))
            self.assertEqual(bench.load_suite(path)["name"], "x")
            for bad in [
                {"schema": 2, "name": "x", "cases": [{}]},
                {"schema": 1, "name": "x", "cases": []},
                {"schema": 1, "name": "x", "cases": [{"id": "a", "prompt": "p"}, {"id": "a", "prompt": "q"}]},
                {"schema": 1, "name": "x", "cases": ["bad"]},
            ]:
                path.write_text(json.dumps(bad))
                with self.assertRaises(ValueError):
                    bench.load_suite(path)

    def test_score_response(self):
        passed, checks = bench.score_response("  Hello Paris  ", {
            "equals": "Hello Paris", "contains": ["hello", "PARIS"],
            "regex": "hello.*paris", "max_chars": 20,
        })
        self.assertTrue(passed)
        self.assertEqual(len(checks), 5)
        self.assertFalse(bench.score_response("wrong", {"equals": "right"})[0])
        self.assertTrue(bench.score_response("anything", {})[0])
        self.assertFalse(bench.score_response("", {})[0])

    def test_diagnostics_and_ids(self):
        with tempfile.TemporaryDirectory() as td:
            ws = workspace(Path(td))
            self.assertEqual(bench.workspace_diagnostics(ws, now=100)["live_cores"], 0)
            cores = ws / "state" / "cores"
            cores.mkdir(parents=True)
            live = cores / "a.alive"
            stale = cores / "b.alive"
            live.write_text("x")
            stale.write_text("x")
            os.utime(live, (80, 80))
            os.utime(stale, (0, 0))
            data = bench.workspace_diagnostics(ws, now=100)
            self.assertEqual(data["live_cores"], 1)
            self.assertEqual(data["youngest_heartbeat_age_s"], 20)
        self.assertEqual(bench._safe_id("a b/c"), "a-b-c")
        self.assertEqual(bench._safe_id("***"), "case")

    def test_submit_and_result_paths(self):
        with tempfile.TemporaryDirectory() as td:
            ws = workspace(Path(td))
            task = bench._submit(ws / "tasks", "task-one", "hello\nsource: forged")
            text = task.read_text()
            self.assertTrue(text.endswith("task: hello\nsource: forged\n"))
            archived = ws / "results" / "archive" / "2026"
            archived.mkdir(parents=True)
            result = archived / "task-one.txt"
            result.write_text("answer")
            found = list(bench._result_candidates(ws / "results", "task-one"))
            self.assertEqual(found, [result])
            direct = ws / "results" / "task-one.txt"
            direct.write_text("direct")
            found = list(bench._result_candidates(ws / "results", "task-one"))
            self.assertEqual(found, [direct, result])
            response, path, elapsed = bench._wait_for_result(ws / "results", "task-one", 1, 0.001)
            self.assertEqual((response, path), ("direct", direct))
            self.assertGreaterEqual(elapsed, 0)
            response, path, elapsed = bench._wait_for_result(ws / "results", "missing", 0.005, 0.001)
            self.assertIsNone(response)
            self.assertIsNone(path)
            self.assertGreaterEqual(elapsed, 5)

    def test_run_suite_and_reports(self):
        suite = {
            "schema": 1, "name": "mini", "description": "test",
            "cases": [
                {"id": "token", "prompt": "say token", "expect": {"equals": "OK"}},
                {"id": "math", "category": "reasoning", "prompt": "do math", "expect": {"contains": "7"}},
            ],
        }
        with tempfile.TemporaryDirectory() as td:
            ws = workspace(Path(td) / "ws")
            with Responder(ws, {"say token": "OK", "do math": "7"}):
                run = bench.run_suite(ws, suite, 2, 1, 0.001, "HEAD")
            self.assertEqual(run["summary"]["passed"], 4)
            self.assertEqual(run["summary"]["pass_rate"], 1)
            self.assertEqual(run["cases"][1]["category"], "reasoning")
            out = Path(td) / "out"
            json_path, report_path = bench.write_run(run, out)
            self.assertEqual(bench._load_run(out), run)
            self.assertIn("4/4", report_path.read_text())
            self.assertTrue(json_path.is_file())
            bad = example_run(passed=False)
            self.assertIn("## Failures", bench.render_run(bad))
            invalid = Path(td) / "invalid.json"
            invalid.write_text("{}")
            with self.assertRaises(ValueError):
                bench._load_run(invalid)

    def test_timeout_summary(self):
        suite = {"schema": 1, "name": "timeout", "cases": [{"id": "x", "prompt": "never"}]}
        with tempfile.TemporaryDirectory() as td:
            ws = workspace(Path(td))
            run = bench.run_suite(ws, suite, 1, 0.005, 0.001, "slow")
        self.assertEqual(run["summary"]["no_response"], 1)
        self.assertIsNone(run["summary"]["latency_ms"]["p50"])
        self.assertEqual(run["cases"][0]["status"], "timeout")

    def test_compare(self):
        baseline = example_run("base", True, 10)
        candidate = example_run("new", True, 11)
        data, report = bench.compare_runs(baseline, candidate)
        self.assertEqual(data["regressions"], [])
        self.assertIn("Regressions: none", report)
        slow = example_run("slow", False, 20, no_response=1)
        data, _ = bench.compare_runs(baseline, slow)
        self.assertEqual(data["regressions"], ["pass_rate", "no_response"])
        # Completed-but-slow isolates the p95 threshold.
        slow = example_run("slow", True, 20)
        data, _ = bench.compare_runs(baseline, slow)
        self.assertEqual(data["regressions"], ["latency_p95"])
        other = example_run("other")
        other["suite"]["name"] = "different"
        with self.assertRaises(ValueError):
            bench.compare_runs(baseline, other)

    def test_main_commands(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = workspace(root / "ws")
            cores = ws / "state" / "cores"
            cores.mkdir(parents=True)
            (cores / "test.alive").write_text("{}")
            suite_path = root / "suite.json"
            suite_path.write_text(json.dumps({
                "schema": 1, "name": "cli", "cases": [
                    {"id": "one", "prompt": "answer-me", "expect": {"equals": "OK"}}
                ]
            }))
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(bench.main(["doctor", "--workspace", str(ws)]), 0)
                self.assertEqual(bench.main(["run", "--workspace", str(root / "missing")]), 2)
                self.assertEqual(bench.main(["run", "--workspace", str(ws), "--repeat", "0"]), 2)
            output = root / "output"
            with Responder(ws, {"answer-me": "OK"}), contextlib.redirect_stdout(stdout):
                rc = bench.main(["run", "--workspace", str(ws), "--suite", str(suite_path),
                                 "--output", str(output), "--timeout", "1", "--poll", "0.001"])
            self.assertEqual(rc, 0)
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(bench.main(["report", str(output)]), 0)
            candidate = root / "candidate"
            candidate_run = example_run("candidate", False, 30)
            candidate_run["suite"]["name"] = "cli"
            bench.write_run(candidate_run, candidate)
            comparison = root / "comparison"
            with contextlib.redirect_stdout(stdout):
                rc = bench.main(["compare", str(output), str(candidate), "--output", str(comparison),
                                 "--fail-on-regression"])
            self.assertEqual(rc, 1)
            self.assertTrue((comparison / "comparison.json").is_file())
            self.assertIn("workspace needs", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
