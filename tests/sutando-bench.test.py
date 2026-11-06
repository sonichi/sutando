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
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import sutando_bench as bench  # noqa: E402


def workspace(root: Path) -> Path:
    (root / "tasks").mkdir(parents=True)
    (root / "results").mkdir()
    return root


def runtime_descriptor(ws: Path, revision="a" * 40, dirty=False,
                       source="git", tree_sha="b" * 40, tree_digest=None):
    return {
        "workspace": str(ws.resolve()), "repo": "/engine/sutando", "runtimeId": "primary",
        "code": {
            "revision": revision, "commit": revision[:7], "branch": "main",
            "describe": revision[:7], "tree_sha": tree_sha,
            "tree_digest": tree_digest, "dirty": dirty, "source": source,
            "built_at": "2026-08-24T00:00:00Z" if source == "engine-manifest" else None,
        },
    }


def runtime_snapshot(ws: Path, **kwargs):
    return bench.runtime_identity(runtime_descriptor(ws, **kwargs), ws)


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
        "subject": {"label": label, "workspace": "/tmp/ws", "host": "host",
                    "runtime": runtime_snapshot(Path("/tmp/ws")), "version_stable": True},
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

    def test_runtime_identity_is_workspace_bound_and_content_addressed(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            identity = runtime_snapshot(ws)
            self.assertTrue(identity["exact"])
            self.assertIn("git:" + "a" * 40, identity["version_key"])
            packaged = runtime_snapshot(
                ws, source="engine-manifest", tree_sha=None,
                tree_digest="sha256:" + "c" * 64,
            )
            self.assertTrue(packaged["exact"])
            self.assertIn("sha256:", packaged["version_key"])
            dirty = runtime_snapshot(ws, dirty=True)
            self.assertFalse(dirty["exact"])
            self.assertTrue(dirty["version_key"].endswith(":dirty"))
            wrong = runtime_descriptor(ws)
            wrong["workspace"] = str(ws / "other")
            with self.assertRaisesRegex(ValueError, "different workspace"):
                bench.runtime_identity(wrong, ws)
            missing = runtime_descriptor(ws)
            missing["code"]["revision"] = None
            with self.assertRaisesRegex(ValueError, "unattributed"):
                bench.runtime_identity(missing, ws)

    def test_runtime_identity_rejects_each_malformed_descriptor_shape(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            with self.assertRaisesRegex(ValueError, "must be a JSON object"):
                bench.runtime_identity(["not", "a", "dict"], ws)
            no_ws = runtime_descriptor(ws)
            del no_ws["workspace"]
            with self.assertRaisesRegex(ValueError, "has no workspace"):
                bench.runtime_identity(no_ws, ws)
            bad_ws_type = runtime_descriptor(ws)
            bad_ws_type["workspace"] = 17
            with self.assertRaisesRegex(ValueError, "has no workspace"):
                bench.runtime_identity(bad_ws_type, ws)
            no_code = runtime_descriptor(ws)
            no_code["code"] = "a-string-is-not-an-identity"
            with self.assertRaisesRegex(ValueError, "has no code identity"):
                bench.runtime_identity(no_code, ws)
            short = runtime_descriptor(ws, revision="abc")
            with self.assertRaisesRegex(ValueError, "unattributed"):
                bench.runtime_identity(short, ws)
            nonhex = runtime_descriptor(ws, revision="z" * 40)
            with self.assertRaisesRegex(ValueError, "unattributed"):
                bench.runtime_identity(nonhex, ws)
            bad_source = runtime_descriptor(ws)
            bad_source["code"]["source"] = "hearsay"
            with self.assertRaisesRegex(ValueError, "unattributed"):
                bench.runtime_identity(bad_source, ws)

    def test_compare_flags_every_attribution_gap(self):
        # An unattributed or drifting build must not read as a clean comparison:
        # the regression verdict is only as good as the identity behind it.
        baseline = example_run()
        candidate = example_run()
        baseline["subject"]["runtime"] = None
        candidate["subject"]["runtime"] = "not-a-descriptor"
        baseline["subject"]["version_stable"] = False
        candidate["subject"]["version_stable"] = None
        data, _ = bench.compare_runs(baseline, candidate)
        self.assertEqual(
            sorted(data["attribution"]["warnings"]),
            sorted(["baseline_unattributed", "candidate_unattributed",
                    "baseline_version_not_stable", "candidate_version_not_stable"]),
        )
        self.assertFalse(data["attribution"]["same_version"])

        dirty_base = example_run()
        dirty_cand = example_run()
        dirty_base["subject"]["runtime"] = runtime_snapshot(Path("/tmp/ws"), dirty=True)
        dirty_cand["subject"]["runtime"] = runtime_snapshot(Path("/tmp/ws"), dirty=True)
        data, _ = bench.compare_runs(dirty_base, dirty_cand)
        self.assertIn("baseline_version_not_exact", data["attribution"]["warnings"])
        self.assertIn("candidate_version_not_exact", data["attribution"]["warnings"])
        self.assertFalse(data["attribution"]["same_version"])

    def test_cli_shim_imports_and_exposes_main(self):
        # scripts/sutando-bench.py is the installed entry point; nothing else
        # asserts it resolves the sibling module and re-exports a callable main.
        import importlib.util
        shim_path = REPO / "scripts" / "sutando-bench.py"
        self.assertTrue(shim_path.is_file())
        spec = importlib.util.spec_from_file_location("sutando_bench_cli", shim_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(callable(module.main))
        self.assertIs(module.main, bench.main)

    def test_probe_runtime_parses_and_validates_descriptor(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            script = root / "scripts" / "sutando-config.sh"
            script.parent.mkdir()
            script.write_text("#!/bin/sh\n")
            descriptor = runtime_descriptor(root)
            completed = mock.Mock(returncode=0, stdout=json.dumps(descriptor), stderr="")
            with mock.patch.object(bench.subprocess, "run", return_value=completed) as run:
                identity = bench.probe_runtime(script, root)
            self.assertEqual(identity["code"]["revision"], "a" * 40)
            self.assertEqual(run.call_args.args[0], ["bash", str(script.resolve()), "runtime"])
            completed.stdout = "not-json"
            with mock.patch.object(bench.subprocess, "run", return_value=completed):
                with self.assertRaisesRegex(RuntimeError, "did not return JSON"):
                    bench.probe_runtime(script, root)

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

    def test_wait_for_result_does_not_consume_a_partial_write(self):
        with tempfile.TemporaryDirectory() as td:
            ws = workspace(Path(td))
            path = ws / "results" / "task-partial.txt"

            def write_in_parts():
                path.write_text("O")
                time.sleep(0.002)
                with path.open("a") as handle:
                    handle.write("K")

            writer = threading.Thread(target=write_in_parts)
            writer.start()
            response, found, _ = bench._wait_for_result(
                ws / "results", "task-partial", 1, 0.001)
            writer.join()
            self.assertEqual((response, found), ("OK", path))

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
            runtime = runtime_snapshot(ws)
            with Responder(ws, {"say token": "OK", "do math": "7"}):
                run = bench.run_suite(ws, suite, 2, 1, 0.001, "HEAD", runtime)
            run["subject"]["version_stable"] = True
            self.assertEqual(run["summary"]["passed"], 4)
            self.assertEqual(run["summary"]["pass_rate"], 1)
            self.assertEqual(run["cases"][1]["category"], "reasoning")
            out = Path(td) / "out"
            json_path, report_path = bench.write_run(run, out)
            self.assertEqual(bench._load_run(out), run)
            self.assertIn("4/4", report_path.read_text())
            self.assertIn("Version:", report_path.read_text())
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
        self.assertTrue(data["attribution"]["same_version"])
        self.assertEqual(data["attribution"]["warnings"], [])
        self.assertIn("Regressions: none", report)
        self.assertIn("Baseline version:", report)
        slow = example_run("slow", False, 20, no_response=1)
        data, _ = bench.compare_runs(baseline, slow)
        self.assertEqual(data["regressions"],
                         ["pass_rate", "no_response", "cases_regressed"])
        # Completed-but-slow isolates the p95 threshold.
        slow = example_run("slow", True, 20)
        data, _ = bench.compare_runs(baseline, slow)
        self.assertEqual(data["regressions"], ["latency_p95"])
        other = example_run("other")
        other["suite"]["name"] = "different"
        with self.assertRaises(ValueError):
            bench.compare_runs(baseline, other)

    def test_compare_reports_per_case_transitions(self):
        def run(label, outcomes):
            rows = [{
                "case_id": cid, "category": "test", "repetition": 1,
                "task_id": f"task-{cid}", "status": "completed", "passed": ok,
                "latency_ms": 10.0, "wait_ms": 10.0, "response": "OK",
                "result_path": None, "checks": [],
            } for cid, ok in outcomes.items()]
            doc = example_run(label)
            doc["cases"] = rows
            doc["summary"] = bench.summarize(rows)
            return doc

        # The case this exists for: identical pass rates, one case broken and
        # another recovered, which every aggregate metric reports as unchanged.
        baseline = run("base", {"a": True, "b": False})
        candidate = run("cand", {"a": False, "b": True})
        data, report = bench.compare_runs(baseline, candidate)
        self.assertEqual(data["metrics"]["pass_rate"]["baseline"],
                         data["metrics"]["pass_rate"]["candidate"])
        self.assertEqual(data["cases"]["regressed"], ["a"])
        self.assertEqual(data["cases"]["recovered"], ["b"])
        self.assertIn("cases_regressed", data["regressions"])
        self.assertIn("`a`", report)

        # Control: an unchanged run must NOT report a regression, or the check
        # above passes for every input.
        data, _ = bench.compare_runs(baseline, run("same", {"a": True, "b": False}))
        self.assertEqual(data["cases"]["regressed"], [])
        self.assertNotIn("cases_regressed", data["regressions"])
        self.assertEqual(data["cases"]["transitions"],
                         {"pass_to_pass": 1, "pass_to_fail": 0,
                          "fail_to_pass": 0, "fail_to_fail": 1})

    def test_compare_case_needs_every_repetition(self):
        def run(label, reps):
            rows = [{
                "case_id": "flaky", "category": "test", "repetition": i + 1,
                "task_id": f"task-{i}", "status": "completed", "passed": ok,
                "latency_ms": 10.0, "wait_ms": 10.0, "response": "OK",
                "result_path": None, "checks": [],
            } for i, ok in enumerate(reps)]
            doc = example_run(label)
            doc["cases"] = rows
            doc["summary"] = bench.summarize(rows)
            return doc

        data, _ = bench.compare_runs(run("base", [True, True]), run("cand", [True, False]))
        self.assertEqual(data["cases"]["regressed"], ["flaky"])

    def test_compare_names_cases_present_on_one_side_only(self):
        baseline = example_run("base")
        candidate = example_run("cand")
        candidate["cases"] = [dict(candidate["cases"][0], case_id="two")]
        candidate["summary"] = bench.summarize(candidate["cases"])
        data, _ = bench.compare_runs(baseline, candidate)
        self.assertEqual(data["cases"]["only_in_baseline"], ["one"])
        self.assertEqual(data["cases"]["only_in_candidate"], ["two"])
        # No shared cases, so nothing can have regressed.
        self.assertEqual(data["cases"]["regressed"], [])

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
            identity = runtime_snapshot(ws)
            with Responder(ws, {"answer-me": "OK"}), contextlib.redirect_stdout(stdout), \
                    mock.patch.object(bench, "probe_runtime", side_effect=[identity, identity]):
                rc = bench.main(["run", "--workspace", str(ws), "--suite", str(suite_path),
                                 "--output", str(output), "--timeout", "1", "--poll", "0.001"])
            self.assertEqual(rc, 0)
            self.assertTrue(bench._load_run(output)["subject"]["version_stable"])
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

    def test_main_writes_diagnostic_run_and_exits_two_on_version_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = workspace(root / "ws")
            suite_path = root / "suite.json"
            suite_path.write_text(json.dumps({
                "schema": 1, "name": "drift", "cases": [
                    {"id": "one", "prompt": "answer-me", "expect": {"equals": "OK"}}
                ]
            }))
            output = root / "output"
            before = runtime_snapshot(ws, revision="a" * 40)
            after = runtime_snapshot(ws, revision="c" * 40)
            with Responder(ws, {"answer-me": "OK"}), \
                    mock.patch.object(bench, "probe_runtime", side_effect=[before, after]), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = bench.main([
                    "run", "--workspace", str(ws), "--suite", str(suite_path),
                    "--output", str(output), "--timeout", "1", "--poll", "0.001",
                ])
            self.assertEqual(rc, 2)
            run = bench._load_run(output)
            self.assertFalse(run["subject"]["version_stable"])
            self.assertEqual(run["subject"]["runtime_end"], after)

    def test_main_refuses_to_run_without_version_attribution(self):
        with tempfile.TemporaryDirectory() as td:
            ws = workspace(Path(td) / "ws")
            stderr = io.StringIO()
            with mock.patch.object(bench, "probe_runtime", side_effect=ValueError("unattributed")), \
                    contextlib.redirect_stderr(stderr):
                rc = bench.main(["run", "--workspace", str(ws)])
            self.assertEqual(rc, 2)
            self.assertIn("cannot attribute runtime version", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
