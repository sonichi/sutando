#!/usr/bin/env python3
"""Unit and controller-path tests for scripts/sutando_bench.py."""
import contextlib
import copy
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


def duplicate_key_suites():
    prefix = '{"schema":1,"name":"duplicates","cases":[{"id":"one","prompt":"reply","expect":'
    suffix = '}]}'
    suites = [(label, prefix + expected + suffix) for label, expected in [
        ("expected conflict", '{"json_equals":{"ok":true,"ok":false}}'),
        ("expected repeated value", '{"json_equals":{"ok":false,"ok":false}}'),
        ("expected escaped key", '{"json_equals":{"ok":true,"\\u006fk":false}}'),
        ("expected nested array", '{"json_equals":[{"ok":true,"ok":false}]}'),
        ("expected nested escaped key", '{"json_equals":{"nested":[{"ok":true,"\\u006fk":false}]}}'),
        ("expected overwritten invalid value", '{"json_equals":{"ok":NaN,"ok":false}}'),
        ("assertion", '{"json_equals":true,"json_equals":false}'),
        ("escaped assertion", '{"json_equals":true,"json_\\u0065quals":false}'),
        ("text assertion", '{"equals":"RIGHT","equals":"false"}'),
    ]]
    valid = prefix + '{"json_equals":false}' + suffix
    for label, old, new in [
        ("suite schema", '"schema":1', '"schema":1,"schema":1'),
        ("suite name", '"name":"duplicates"', '"name":"first","name":"duplicates"'),
        ("escaped suite name", '"name":"duplicates"', '"name":"first","na\\u006de":"duplicates"'),
        ("suite cases", '"cases":[', '"cases":[],"cases":['),
        ("case id", '"id":"one"', '"id":"first","id":"one"'),
        ("case prompt", '"prompt":"reply"', '"prompt":"first","prompt":"reply"'),
        ("case expect", '"expect":', '"expect":{"equals":"WRONG"},"expect":'),
        ("other nested object", '"cases":[', '"metadata":{"tag":1,"tag":2},"cases":['),
    ]:
        suites.append((label, valid.replace(old, new)))
    return suites


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

    def test_load_suite_rejects_duplicate_suite_keys(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "suite.json"
            for label, body in duplicate_key_suites():
                with self.subTest(label=label):
                    path.write_text(body)
                    with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
                        bench.load_suite(path)

    def test_main_rejects_duplicate_suite_keys_before_probe_or_dispatch(self):
        for label, body in duplicate_key_suites():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ws = workspace(root / "ws")
                suite = root / "suite.json"
                suite.write_text(body)
                stderr = io.StringIO()
                with Responder(ws, {"reply": "false"}), \
                        mock.patch.object(bench, "probe_runtime", return_value=runtime_snapshot(ws)) as probe, \
                        mock.patch.object(bench, "_submit", wraps=bench._submit) as submit, \
                        contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                    rc = bench.main(["run", "--workspace", str(ws), "--suite", str(suite),
                                     "--output", str(root / "output"), "--timeout", "0.1", "--poll", "0.001"])
                self.assertEqual(rc, 2)
                self.assertIn("invalid suite", stderr.getvalue())
                self.assertIn("duplicate JSON object key", stderr.getvalue())
                self.assertIn("fix the suite", stderr.getvalue())
                probe.assert_not_called()
                submit.assert_not_called()
                self.assertEqual(list((ws / "tasks").iterdir()), [])
                self.assertEqual(list((ws / "results").iterdir()), [])
                self.assertFalse((root / "output").exists())

    def test_unique_suite_keys_preserve_text_and_json_controller_semantics(self):
        cases = [
            {"id": "text", "prompt": "text reply", "expect": {
                "equals": "Paris", "contains": "PARIS", "regex": "^paris$", "max_chars": 5}},
            {"id": "text mismatch", "prompt": "wrong text", "expect": {"equals": "Paris"}},
            {"id": "json", "prompt": "json reply", "expect": {"json_equals": {
                "first": {"ok": True}, "second": {"ok": False}, "number": 1}}},
            {"id": "json mismatch", "prompt": "wrong json", "expect": {"json_equals": True}},
        ]
        responses = {"text reply": "Paris\n", "wrong text": "Rome",
                     "json reply": '{"number":1.0,"second":{"ok":false},"first":{"ok":true}}',
                     "wrong json": "1"}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = workspace(root / "ws")
            suite = root / "suite.json"
            data = {"schema": 1, "name": "unique keys", "cases": cases}
            suite.write_text(json.dumps(data))
            self.assertEqual(bench.load_suite(suite), data)
            with Responder(ws, responses), \
                    mock.patch.object(bench, "probe_runtime", return_value=runtime_snapshot(ws)) as probe, \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = bench.main(["run", "--workspace", str(ws), "--suite", str(suite),
                                 "--output", str(root / "output"), "--timeout", "1", "--poll", "0.001"])
            self.assertEqual(rc, 1)
            self.assertEqual(probe.call_count, 2)
            run = bench._load_run(root / "output")
            self.assertEqual([row["passed"] for row in run["cases"]], [True, False, True, False])
            self.assertEqual(run["summary"]["passed"], 2)
            self.assertEqual(run["summary"]["no_response"], 0)
            self.assertEqual([check["kind"] for check in run["cases"][0]["checks"]],
                             ["equals", "contains", "regex", "max_chars"])
            self.assertEqual(run["cases"][2]["checks"], [{"kind": "json_equals",
                                                         "expected": cases[2]["expect"]["json_equals"],
                                                         "passed": True}])

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

    def test_json_equals_matches_structural_values(self):
        for response, wanted in [
            (" null \n", None), ("true", True), ("false", False),
            ("0", 0), ("-0.0", 0), ("1.0", 1), ("1", 1.0),
            ("1e2", 100), ("1e308", 1e308), ("5e-324", 5e-324),
            ('"Paris"', "Paris"), ('""', ""), ('"\\u00e9"', "é"),
            ("[]", []), ("{}", {}),
            (' {"b": [true, null, 2.0], "a": {"x": "ok"}}\n',
             {"a": {"x": "ok"}, "b": [True, None, 2]}),
        ]:
            with self.subTest(response=response, wanted=wanted):
                passed, checks = bench.score_response(response, {"json_equals": wanted})
                self.assertTrue(passed)
                self.assertEqual(checks, [{"kind": "json_equals", "expected": wanted,
                                           "passed": True}])

    def test_json_equals_compares_accepted_deep_arrays_objects_and_mixed_values(self):
        depth = sys.getrecursionlimit() * 2 // 3
        for shape in ["arrays", "objects", "mixed"]:
            with self.subTest(shape=shape, depth=depth), tempfile.TemporaryDirectory() as td:
                wanted = {"leaf": [True, 1, 2], "number": 1}
                responses = ['{"number":1.0,"leaf":[true,1.0,2]}',
                             '{"number":1.0,"leaf":[1,true,2]}',
                             '{"number":1.0,"leaf":[true,2,1]}']
                for level in range(depth):
                    if shape == "arrays" or (shape == "mixed" and level % 2 == 0):
                        wanted = [wanted]
                        responses = ["[" + response + "]" for response in responses]
                    else:
                        wanted = {"nested": wanted}
                        responses = ['{"nested":' + response + "}" for response in responses]
                path = Path(td) / "suite.json"
                path.write_text(json.dumps({"schema": 1, "name": "deep", "cases": [
                    {"id": "one", "prompt": "p", "expect": {"json_equals": wanted}}]}))
                loaded = bench.load_suite(path)
                expected = loaded["cases"][0]["expect"]
                passed, checks = bench.score_response(responses[0], expected)
                self.assertTrue(passed)
                self.assertIs(checks[0]["expected"], expected["json_equals"])
                for response in responses[1:]:
                    self.assertFalse(bench.score_response(response, expected)[0])

    def test_json_equals_preserves_types_and_array_order(self):
        for response, wanted in [
            ("true", 1), ("1", True), ("false", 0), ("0.0", False),
            ('"1"', 1), ('"True"', True), ('"Paris"', "paris"),
            ("null", "null"), ("[]", {}), ("{}", []),
            ("[1, 2]", [2, 1]), ("[1]", [1, 2]),
            ('{"x": 1}', {"x": 1, "y": 2}),
            ('{"x": 1, "extra": true}', {"x": 1}),
            ('{"a": {"x": 1, "extra": true}}', {"a": {"x": 1}}),
            ('{"x": [true]}', {"x": [1]}),
            ('{"x": [{"y": 0}]}', {"x": [{"y": False}]}),
            ("9007199254740993", 9007199254740992.0),
        ]:
            with self.subTest(response=response, wanted=wanted):
                passed, checks = bench.score_response(response, {"json_equals": wanted})
                self.assertFalse(passed)
                self.assertEqual(checks, [{"kind": "json_equals", "expected": wanted,
                                           "passed": False}])

    def test_json_equals_rejects_non_json_responses(self):
        for response in [
            "", " \n\t", "{", "[1,]", '{"x": 1,}', "{'x': 1}",
            '"line\nbreak"', "01", "+1", "undefined", "NULL",
            "null trailing prose", "null\nnull", "Here is JSON: null",
            "```json\nnull\n```", "```\nnull\n```", "\ufeffnull",
            "NaN", "Infinity", "-Infinity", "1e309", "-1e309",
            '[1, {"x": NaN}]', '{"x": [1e309]}',
        ]:
            with self.subTest(response=response):
                passed, checks = bench.score_response(response, {"json_equals": None})
                self.assertFalse(passed)
                self.assertEqual(checks, [{"kind": "json_equals", "expected": None,
                                           "passed": False}])
        for response, wanted in [
            ('{"x": 1, "x": 1}', {"x": 1}),
            ('{"x": 1, "\\u0078": 2}', {"x": 2}),
            ('{"outer": [{"x": 1, "x": 2}]}', {"outer": [{"x": 2}]}),
        ]:
            with self.subTest(response=response):
                self.assertFalse(bench.score_response(response, {"json_equals": wanted})[0])
        for response in ["NaN", "Infinity", "-Infinity", "1e309", "-1e309",
                         '[1, {"x": NaN}]', '{"x": [1e309]}']:
            with self.subTest(response=response), \
                    mock.patch.object(bench, "_json_values_equal", return_value=True) as compare:
                self.assertFalse(bench.score_response(response, {"json_equals": None})[0])
                compare.assert_not_called()

    def test_json_equals_handles_parser_value_and_recursion_failures(self):
        depth = sys.getrecursionlimit() * 10
        for response in ["[" * depth + "0" + "]" * depth, "1" * 5000]:
            with self.subTest(response_length=len(response)):
                self.assertFalse(bench.score_response(response, {"json_equals": None})[0])
        for error in [ValueError("unsupported value"), OverflowError("overflow"),
                      RecursionError("nesting")]:
            with self.subTest(error=error), mock.patch.object(bench.json, "loads", side_effect=error):
                self.assertFalse(bench.score_response("null", {"json_equals": None})[0])

    def test_json_equals_combines_with_existing_assertions(self):
        response = ' {"answer": "Paris"} \n'
        expected = {"equals": response.strip(), "contains": ["ANSWER", "paris"],
                    "regex": '"answer":.*"Paris"', "max_chars": len(response.strip()),
                    "json_equals": {"answer": "Paris"}}
        passed, checks = bench.score_response(response, expected)
        self.assertTrue(passed)
        self.assertEqual([check["kind"] for check in checks],
                         ["equals", "contains", "contains", "regex", "max_chars", "json_equals"])
        for field, value in [("equals", "wrong"), ("contains", "wrong"),
                             ("regex", "wrong"), ("max_chars", 0),
                             ("json_equals", {"answer": "Rome"})]:
            with self.subTest(field=field):
                passed, checks = bench.score_response(response, dict(expected, **{field: value}))
                self.assertFalse(passed)
                self.assertEqual([check["kind"] for check in checks if not check["passed"]], [field])

    def test_validate_json_equals_accepts_plain_json_values(self):
        shared = {"nested": [None, True, False, 1, 1.5, "text"]}
        for wanted in [None, True, False, 0, 10 ** 100, 1.5, "", [], {},
                       {"first": shared, "second": shared}]:
            with self.subTest(wanted=wanted):
                bench.validate_suite({"schema": 1, "name": "json", "cases": [
                    {"id": "one", "prompt": "p", "expect": {"json_equals": wanted}}]})

    def test_run_suite_rejects_invalid_json_equals_before_submission(self):
        class CustomInt(int):
            pass

        class CustomList(list):
            pass

        for wanted in [float("nan"), float("inf"), float("-inf"),
                       {"nested": [float("inf")]}, {1: "number key"},
                       {"nested": {False: "boolean key"}}, (1, 2), {1, 2}, b"json",
                       object(), CustomInt(1), CustomList([1])]:
            with self.subTest(wanted=wanted), tempfile.TemporaryDirectory() as td:
                ws = workspace(Path(td))
                suite = {"schema": 1, "name": "json", "cases": [
                    {"id": "first", "prompt": "p"},
                    {"id": "second", "prompt": "p", "expect": {"json_equals": wanted}}]}
                with mock.patch.object(bench, "_submit") as submit:
                    with self.assertRaisesRegex(ValueError, "case 2.*json_equals"):
                        bench.run_suite(ws, suite, 1, 0.01, 0.001, "json")
                submit.assert_not_called()
                self.assertEqual(list((ws / "tasks").iterdir()), [])

    def test_json_equals_validation_reports_cycles_and_excessive_nesting(self):
        cyclic_list = []
        cyclic_list.append(cyclic_list)
        cyclic_dict = {}
        cyclic_dict["self"] = cyclic_dict
        nested = None
        for _ in range(sys.getrecursionlimit() + 100):
            nested = [nested]
        for wanted in [cyclic_list, cyclic_dict, nested]:
            with tempfile.TemporaryDirectory() as td:
                ws = workspace(Path(td))
                suite = {"schema": 1, "name": "json", "cases": [
                    {"id": "one", "prompt": "p", "expect": {"json_equals": wanted}}]}
                with self.assertRaisesRegex(ValueError, "case 1.*json_equals"):
                    bench.run_suite(ws, suite, 1, 0.01, 0.001, "json")
                self.assertEqual(list((ws / "tasks").iterdir()), [])

    def test_main_rejects_invalid_json_equals_before_probe_or_dispatch(self):
        for wanted in ["NaN", "Infinity", "-Infinity", "1e309", '{"nested": [NaN]}']:
            with self.subTest(wanted=wanted), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ws = workspace(root / "ws")
                suite = root / "suite.json"
                body = {"schema": 1, "name": "json", "cases": [
                    {"id": "first", "prompt": "p"},
                    {"id": "second", "prompt": "p", "expect": {"json_equals": "VALUE"}}]}
                suite.write_text(json.dumps(body).replace('"VALUE"', wanted))
                stderr = io.StringIO()
                with mock.patch.object(bench, "probe_runtime") as probe, \
                        mock.patch.object(bench, "_submit") as submit, \
                        mock.patch.object(bench, "_wait_for_result", side_effect=AssertionError(
                            "invalid suite reached result waiting")) as wait, \
                        contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                    rc = bench.main(["run", "--workspace", str(ws), "--suite", str(suite),
                                     "--output", str(root / "output"), "--timeout", "0.1", "--poll", "0.001"])
                self.assertEqual(rc, 2)
                self.assertIn("case 2", stderr.getvalue())
                self.assertIn("json_equals", stderr.getvalue())
                self.assertIn("fix the suite", stderr.getvalue())
                probe.assert_not_called()
                submit.assert_not_called()
                wait.assert_not_called()
                self.assertEqual(list((ws / "tasks").iterdir()), [])
                self.assertFalse((root / "output").exists())

    def test_main_reports_supported_nesting_from_expected_validator(self):
        nested = None
        for _ in range(sys.getrecursionlimit() + 100):
            nested = [nested]
        data = {"schema": 1, "name": "deep", "cases": [
            {"id": "one", "prompt": "p", "expect": {"json_equals": nested}}]}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = workspace(root / "ws")
            suite = root / "suite.json"
            suite.write_text("{}")
            stderr = io.StringIO()
            with mock.patch.object(bench.json, "loads", return_value=data) as decode, \
                    mock.patch.object(bench, "probe_runtime") as probe, \
                    mock.patch.object(bench, "_submit") as submit, \
                    mock.patch.object(bench, "_wait_for_result", side_effect=AssertionError(
                        "invalid suite reached result waiting")) as wait, \
                    contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                rc = bench.main(["run", "--workspace", str(ws), "--suite", str(suite),
                                 "--output", str(root / "output"), "--timeout", "0.1", "--poll", "0.001"])
            self.assertEqual(rc, 2)
            self.assertIn("case 1", stderr.getvalue())
            self.assertIn("json_equals", stderr.getvalue())
            self.assertIn("supported nesting", stderr.getvalue())
            self.assertIn("fix the suite", stderr.getvalue())
            decode.assert_called_once()
            probe.assert_not_called()
            submit.assert_not_called()
            wait.assert_not_called()
            self.assertEqual(list((ws / "tasks").iterdir()), [])
            self.assertFalse((root / "output").exists())
        with self.assertRaisesRegex(ValueError, "case 1.*json_equals.*supported nesting") as error:
            bench.validate_suite(data)
        self.assertIsInstance(error.exception.__cause__, RecursionError)

    def test_main_saves_json_equals_checks_from_disposable_responder(self):
        cases = [
            {"id": "object", "prompt": "object response",
             "expect": {"json_equals": {"a": 1, "b": [True, None]}}},
            {"id": "null", "prompt": "null response", "expect": {"json_equals": None}},
            {"id": "boolean", "prompt": "boolean response", "expect": {"json_equals": True}},
            {"id": "prose", "prompt": "prose response", "expect": {"json_equals": {}}},
        ]
        responses = {"object response": ' {"b": [true, null], "a": 1.0}\n',
                     "null response": "null", "boolean response": "1",
                     "prose response": "{} done"}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = workspace(root / "ws")
            suite = root / "suite.json"
            suite.write_text(json.dumps({"schema": 1, "name": "json", "cases": cases}))
            output = root / "output"
            with Responder(ws, responses), \
                    mock.patch.object(bench, "probe_runtime", return_value=runtime_snapshot(ws)) as probe, \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = bench.main(["run", "--workspace", str(ws), "--suite", str(suite),
                                 "--output", str(output), "--timeout", "1", "--poll", "0.001"])
            self.assertEqual(rc, 1)
            self.assertEqual(probe.call_count, 2)
            run = bench._load_run(output)
            self.assertEqual(run["schema"], 1)
            self.assertEqual(run["kind"], "sutando-benchmark-run")
            self.assertEqual(run["summary"]["attempts"], 4)
            self.assertEqual(run["summary"]["passed"], 2)
            self.assertEqual(run["summary"]["no_response"], 0)
            for row, case, passed in zip(run["cases"], cases, [True, True, False, False]):
                self.assertEqual(row["status"], "completed")
                self.assertEqual(row["passed"], passed)
                self.assertEqual(row["response"], responses[case["prompt"]])
                self.assertEqual(row["checks"], [{"kind": "json_equals",
                                                 "expected": case["expect"]["json_equals"],
                                                 "passed": passed}])
            self.assertIn("2/4", (output / "report.md").read_text())

    def test_main_rejects_excessively_nested_json_equals_before_probe_or_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = workspace(root / "ws")
            suite = root / "suite.json"
            body = {"schema": 1, "name": "json", "cases": [
                {"id": "one", "prompt": "p", "expect": {"json_equals": "VALUE"}}]}
            depth = sys.getrecursionlimit() * 10
            nested = "[" * depth + "null" + "]" * depth
            suite.write_text(json.dumps(body).replace('"VALUE"', nested))
            stderr = io.StringIO()
            with mock.patch.object(bench, "probe_runtime") as probe, \
                    mock.patch.object(bench, "_submit") as submit, \
                    contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                rc = bench.main(["run", "--workspace", str(ws), "--suite", str(suite),
                                 "--output", str(root / "output")])
            self.assertEqual(rc, 2)
            self.assertIn("invalid suite", stderr.getvalue())
            self.assertIn("nesting", stderr.getvalue())
            self.assertIn("fix the suite", stderr.getvalue())
            probe.assert_not_called()
            submit.assert_not_called()
            self.assertEqual(list((ws / "tasks").iterdir()), [])
            self.assertFalse((root / "output").exists())

    def test_load_suite_rejects_malformed_case_shapes(self):
        valid = {"schema": 1, "name": "validation", "cases": [{"id": "one", "prompt": "p"}]}
        invalid = [
            ([], "suite"),
            (dict(valid, schema=True), "schema"),
            (dict(valid, name=42), "name"),
            (dict(valid, name="  "), "name"),
            (dict(valid, cases=None), "cases"),
        ]
        for field, value in [("id", []), ("id", 1), ("id", " "),
                             ("prompt", []), ("prompt", False), ("prompt", "\n")]:
            case = dict(valid["cases"][0], **{field: value})
            invalid.append((dict(valid, cases=[case]), field))
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "suite.json"
            for data, field in invalid:
                with self.subTest(data=data):
                    path.write_text(json.dumps(data))
                    with self.assertRaisesRegex(ValueError, field):
                        bench.load_suite(path)

    def test_load_suite_rejects_malformed_assertions(self):
        invalid = [
            (None, "expect"), ([], "expect"), ("equals", "expect"),
            ({"equal": "RIGHT"}, "equal"),
            ({"equals": "RIGHT", "typo": "ignored"}, "typo"),
            ({"equals": 1}, "equals"), ({"equals": None}, "equals"),
            ({"contains": []}, "contains"), ({"contains": 1}, "contains"),
            ({"contains": ["right", False]}, "contains"),
            ({"contains": ""}, "contains"), ({"contains": [""]}, "contains"),
            ({"contains": ["right", ""]}, "contains"),
            ({"regex": 1}, "regex"), ({"regex": "["}, "regex"),
            ({"regex": ""}, "regex"),
            ({"regex": "a{99999999999999999999}"}, "regex"),
            ({"regex": "(" * 2000 + ")" * 2000}, "regex"),
            ({"max_chars": -1}, "max_chars"), ({"max_chars": True}, "max_chars"),
            ({"max_chars": 2.5}, "max_chars"), ({"max_chars": "20"}, "max_chars"),
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "suite.json"
            for expected, field in invalid:
                with self.subTest(expect=expected):
                    path.write_text(json.dumps({"schema": 1, "name": "validation", "cases": [
                        {"id": "one", "prompt": "p", "expect": expected}]}))
                    with self.assertRaisesRegex(ValueError, "case 1.*" + field):
                        bench.load_suite(path)

    def test_valid_assertions_retain_grading_semantics(self):
        expectations = [({}, True), ({"equals": ""}, False), ({"equals": "Paris"}, True),
                        ({"contains": "PARIS"}, True), ({"contains": ["p", "ris"]}, True),
                        ({"regex": "^paris$"}, True), ({"regex": ".*"}, True),
                        ({"contains": " "}, False), ({"regex": " "}, False),
                        ({"max_chars": 0}, False),
                        ({"equals": "Paris", "contains": ["Par", "is"],
                          "regex": "par.*", "max_chars": 5}, True)]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "suite.json"
            for expected, passed in expectations:
                with self.subTest(expect=expected):
                    data = {"schema": 1, "name": "validation", "cases": [
                        {"id": "one", "prompt": "p", "expect": expected}]}
                    path.write_text(json.dumps(data))
                    loaded = bench.load_suite(path)
                    self.assertEqual(loaded, data)
                    self.assertEqual(bench.score_response("Paris", loaded["cases"][0]["expect"])[0], passed)
            self.assertEqual(bench.load_suite(bench.DEFAULT_SUITE)["name"], "smoke")

    def test_explicit_empty_response_expectations_remain_valid(self):
        for expected in [{"equals": ""}, {"regex": r"\A\Z"}, {"max_chars": 0}]:
            with self.subTest(expect=expected):
                bench.validate_suite({"schema": 1, "name": "empty response", "cases": [
                    {"id": "one", "prompt": "p", "expect": expected}]})
                self.assertTrue(bench.score_response("", expected)[0])
                for response in ["WRONG", "WRONG\n", "WRONG\n\nMORE", "\nWRONG", "WRONG\r\n"]:
                    with self.subTest(response=response):
                        self.assertFalse(bench.score_response(response, expected)[0])

    def test_empty_response_checks_preserve_whitespace_semantics(self):
        for response in [" ", "\t", "\n", "\r\n", "\n\n", " \n\t"]:
            for expected, passed in [({"equals": ""}, True), ({"max_chars": 0}, True),
                                     ({"regex": r"\A\Z"}, False)]:
                with self.subTest(response=response, expect=expected):
                    self.assertEqual(bench.score_response(response, expected)[0], passed)

    def test_regex_line_anchors_keep_multiline_semantics(self):
        for response in ["WRONG\n", "WRONG\n\nMORE", "\nWRONG"]:
            with self.subTest(response=response):
                self.assertTrue(bench.score_response(response, {"regex": "^$"})[0])

    def test_run_suite_rejects_invalid_later_case_before_submission(self):
        for expected in [{"regex": "["}, {"regex": ""}, {"contains": ""},
                         {"contains": [""]}, {"contains": ["right", ""]}]:
            with self.subTest(expect=expected), tempfile.TemporaryDirectory() as td:
                suite = {"schema": 1, "name": "validation", "cases": [
                    {"id": "first", "prompt": "p"},
                    {"id": "second", "prompt": "p", "expect": expected}]}
                ws = workspace(Path(td))
                with self.assertRaisesRegex(ValueError, "case 2.*" + next(iter(expected))):
                    bench.run_suite(ws, suite, 1, 0.01, 0.001, "validation")
                self.assertEqual(list((ws / "tasks").iterdir()), [])

    def test_main_rejects_bad_grading_before_probe_or_dispatch(self):
        for expected in [{"equal": "RIGHT"}, {"regex": "["}, {"max_chars": -1},
                         {"regex": ""}, {"contains": ""}, {"contains": [""]},
                         {"contains": ["right", ""]}]:
            with self.subTest(expect=expected), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                ws = workspace(root / "ws")
                suite = root / "suite.json"
                suite.write_text(json.dumps({"schema": 1, "name": "validation", "cases": [
                    {"id": "first", "prompt": "first"},
                    {"id": "second", "prompt": "second", "expect": expected}]}))
                stderr = io.StringIO()
                with Responder(ws, {"first": "WRONG", "second": "WRONG"}), \
                        mock.patch.object(bench, "probe_runtime", return_value=runtime_snapshot(ws)) as probe, \
                        contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                    rc = bench.main(["run", "--workspace", str(ws), "--suite", str(suite),
                                     "--output", str(root / "output"), "--timeout", "0.1", "--poll", "0.001"])
                self.assertEqual(rc, 2)
                self.assertIn("case 2", stderr.getvalue())
                self.assertIn("fix the suite", stderr.getvalue())
                probe.assert_not_called()
                self.assertEqual(list((ws / "tasks").iterdir()), [])
                self.assertFalse((root / "output").exists())

    def test_main_reports_unreadable_or_invalid_suite_as_input_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = workspace(root / "ws")
            suite = root / "suite.json"
            for body in [None, "{", "null"]:
                with self.subTest(body=body):
                    if body is not None:
                        suite.write_text(body)
                    stderr = io.StringIO()
                    with mock.patch.object(bench, "probe_runtime") as probe, \
                            contextlib.redirect_stderr(stderr):
                        rc = bench.main(["run", "--workspace", str(ws), "--suite", str(suite)])
                    self.assertEqual(rc, 2)
                    self.assertIn("invalid suite", stderr.getvalue())
                    self.assertIn("fix the suite", stderr.getvalue())
                    probe.assert_not_called()

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

    def test_run_suite_isolates_colliding_case_names(self):
        names = ["plan / verify", "plan ? verify", "***", "???", "case", "1-case"]
        cases = [{"id": name, "prompt": f"answer-{index}",
                  "expect": {"equals": f"reply-{index}"}}
                 for index, name in enumerate(names)]
        suite = {"schema": 1, "name": "collisions", "cases": cases}
        with tempfile.TemporaryDirectory() as td:
            ws = workspace(Path(td) / "ws")
            suite_path = Path(td) / "suite.json"
            suite_path.write_text(json.dumps(suite))
            loaded = bench.load_suite(suite_path)
            responses = {case["prompt"]: case["expect"]["equals"] for case in cases}
            with Responder(ws, responses):
                run = bench.run_suite(ws, loaded, 2, 1, 0.001, "collisions")
            rows = run["cases"]
            self.assertEqual(len(rows), 12)
            self.assertEqual(len({row["task_id"] for row in rows}), 12)
            self.assertEqual(run["summary"]["passed"], 12)
            for row, case in zip(rows, cases * 2):
                self.assertEqual(row["case_id"], case["id"])
                self.assertEqual(row["response"], case["expect"]["equals"])
                task = ws / "tasks" / (row["task_id"] + ".txt")
                result = ws / "results" / (row["task_id"] + ".txt")
                self.assertIn("task: " + case["prompt"] + "\n", task.read_text())
                self.assertEqual(result.read_text(), row["response"])
                self.assertEqual(row["result_path"], str(result))
                self.assertRegex(row["task_id"], r"^[A-Za-z0-9_-]+$")
            output = Path(td) / "output"
            bench.write_run(run, output)
            self.assertEqual(bench._load_run(output), run)

    def test_colliding_case_without_reply_times_out(self):
        suite = {"schema": 1, "name": "timeout-collision", "cases": [
            {"id": "a/b", "prompt": "answered", "expect": {"equals": "OK"}},
            {"id": "a b", "prompt": "unanswered", "expect": {"equals": "OK"}},
        ]}
        submit = bench._submit

        def publish_first(tasks, task_id, prompt):
            path = submit(tasks, task_id, prompt)
            if prompt == "answered":
                (tasks.parent / "results" / path.name).write_text("OK")
            return path

        with tempfile.TemporaryDirectory() as td:
            ws = workspace(Path(td))
            with mock.patch.object(bench, "_submit", side_effect=publish_first):
                run = bench.run_suite(ws, suite, 1, 0.03, 0.001, "timeout")
        self.assertEqual([row["status"] for row in run["cases"]], ["completed", "timeout"])
        self.assertEqual([row["passed"] for row in run["cases"]], [True, False])
        self.assertIsNone(run["cases"][1]["response"])
        self.assertIsNone(run["cases"][1]["result_path"])
        self.assertEqual(run["summary"]["no_response"], 1)

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


class CaseDefinitionTests(unittest.TestCase):
    def suite(self):
        return {"schema": 1, "name": "definitions", "cases": [
            {"id": "one", "prompt": "Reply OK", "expect": {"equals": "OK"}},
        ]}

    def run_fixture(self, suite=None, repeat=1):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(bench, "_submit"), \
                mock.patch.object(bench, "_wait_for_result", return_value=("OK", None, 1)):
            return bench.run_suite(Path(td), suite or self.suite(), repeat, 1, .001, "fixture")

    def test_changed_prompts_and_grading_cannot_be_called_regressions(self):
        baseline = self.run_fixture()
        for field, value in [
            ("prompt", "Reply OTHER"), ("expect", {"equals": "OTHER"}),
            ("expect", {"contains": "OK"}), ("expect", {"max_chars": 0}),
            ("expect", {}), ("expect", {"json_equals": "OK"}),
        ]:
            with self.subTest(field=field, value=value):
                changed = self.suite()
                changed["cases"][0][field] = value
                with self.assertRaisesRegex(ValueError, "case definitions differ.*'one'"):
                    bench.compare_runs(baseline, self.run_fixture(changed))

    def test_canonical_objects_metadata_order_and_repetitions_preserve_identity(self):
        suite = self.suite()
        suite["cases"][0]["expect"] = {"json_equals": {"a": [1, {"b": True}], "c": None}}
        suite["cases"].append({"id": "two", "prompt": "é\ud800", "expect": {}})
        changed = copy.deepcopy(suite)
        changed["description"] = "new description"
        changed["cases"][0]["category"] = "new category"
        changed["cases"][0]["expect"] = {"json_equals": {"c": None, "a": [1, {"b": True}]}}
        changed["cases"].reverse()
        baseline = self.run_fixture(suite)
        candidate = self.run_fixture(changed, repeat=2)
        data, report = bench.compare_runs(baseline, candidate)
        self.assertTrue(data["suite_compatibility"]["shared_case_definitions_verified"])
        self.assertEqual(data["suite_compatibility"]["warnings"], [])
        self.assertIn("Shared case definitions verified: yes", report)
        self.assertEqual(baseline["suite"]["case_fingerprints"], candidate["suite"]["case_fingerprints"])

    def test_omitted_expectation_and_empty_object_have_same_identity(self):
        suite = self.suite()
        suite["cases"][0].pop("expect")
        baseline = self.run_fixture(suite)
        suite["cases"][0]["expect"] = {}
        data, _ = bench.compare_runs(baseline, self.run_fixture(suite))
        self.assertTrue(data["suite_compatibility"]["shared_case_definitions_verified"])

    def test_definition_identity_is_conservative_about_equivalent_assertions(self):
        for first, second in [
            ({"json_equals": 1}, {"json_equals": 1.0}),
            ({"contains": "OK"}, {"contains": ["OK"]}),
        ]:
            with self.subTest(first=first, second=second):
                suite = self.suite()
                suite["cases"][0]["expect"] = first
                baseline = self.run_fixture(suite)
                suite["cases"][0]["expect"] = second
                with self.assertRaisesRegex(ValueError, "case definitions differ"):
                    bench.compare_runs(baseline, self.run_fixture(suite))

    def test_saved_run_contains_fingerprints_without_adding_raw_prompts(self):
        suite = self.suite()
        suite["cases"][0]["prompt"] = "fictional prompt not for saved artifacts"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = workspace(root / "ws")
            with Responder(ws, {suite["cases"][0]["prompt"]: "OK"}):
                run = bench.run_suite(ws, suite, 2, 1, .001, "saved")
            json_path, _ = bench.write_run(run, root / "output")
            saved = bench._load_run(json_path)
            self.assertEqual(set(saved["suite"]["case_fingerprints"]), {"one"})
            self.assertRegex(saved["suite"]["case_fingerprints"]["one"], r"\Asha256:[0-9a-f]{64}\Z")
            self.assertNotIn(suite["cases"][0]["prompt"], json_path.read_text())
            self.assertEqual(saved["summary"]["passed"], 2)
            self.assertEqual(len(list((ws / "tasks").iterdir())), 2)

    def test_legacy_and_mixed_runs_explicitly_report_unknown_definitions(self):
        known = self.run_fixture()
        legacy = copy.deepcopy(known)
        legacy["suite"].pop("case_fingerprints")
        for baseline, candidate, warnings in [
            (legacy, known, ["baseline_case_definitions_unknown"]),
            (known, legacy, ["candidate_case_definitions_unknown"]),
            (legacy, legacy, ["baseline_case_definitions_unknown", "candidate_case_definitions_unknown"]),
        ]:
            with self.subTest(warnings=warnings):
                data, report = bench.compare_runs(baseline, candidate)
                self.assertEqual(data["suite_compatibility"], {
                    "shared_case_definitions_verified": False, "warnings": warnings})
                self.assertIn("Shared case definitions verified: no", report)
                self.assertIn("Suite warnings: " + ", ".join(warnings), report)
                self.assertEqual(data["cases"]["transitions"]["pass_to_pass"], 1)

    def test_invalid_maps_are_not_treated_as_legacy_on_either_side(self):
        known = self.run_fixture()
        legacy = copy.deepcopy(known)
        legacy["suite"].pop("case_fingerprints")
        digest = known["suite"]["case_fingerprints"]["one"]
        for value in [None, [], "", {}, {"other": digest}, {"one": digest, "extra": digest},
                      {"one": None}, {"one": 7}, {"one": "a" * 64},
                      {"one": "sha256:" + "A" * 64}, {"one": "sha256:" + "g" * 64},
                      {"one": digest + "\n"}, {"one": digest[:-1]}]:
            malformed = copy.deepcopy(known)
            malformed["suite"]["case_fingerprints"] = value
            for other in (legacy, known):
                for baseline, candidate in ((malformed, other), (other, malformed)):
                    with self.subTest(value=value, legacy=other is legacy,
                                      malformed_baseline=baseline is malformed):
                        with self.assertRaisesRegex(ValueError, "invalid suite.case_fingerprints"):
                            bench.compare_runs(baseline, candidate)

    def test_case_additions_and_removals_keep_shared_definition_checks(self):
        suite = self.suite()
        baseline = self.run_fixture(suite)
        suite["cases"].append({"id": "two", "prompt": "second"})
        candidate = self.run_fixture(suite)
        data, _ = bench.compare_runs(baseline, candidate)
        self.assertTrue(data["suite_compatibility"]["shared_case_definitions_verified"])
        self.assertEqual(data["suite_compatibility"]["warnings"], ["case_set_changed"])
        self.assertEqual(data["cases"]["only_in_candidate"], ["two"])
        suite["cases"] = suite["cases"][1:]
        data, _ = bench.compare_runs(baseline, self.run_fixture(suite))
        self.assertFalse(data["suite_compatibility"]["shared_case_definitions_verified"])
        self.assertEqual(data["cases"]["only_in_baseline"], ["one"])
        self.assertEqual(data["cases"]["only_in_candidate"], ["two"])
        suite["cases"].insert(0, {"id": "one", "prompt": "changed"})
        with self.assertRaisesRegex(ValueError, "case definitions differ"):
            bench.compare_runs(baseline, self.run_fixture(suite))

    def test_cli_refuses_incompatible_runs_without_writing_artifacts(self):
        baseline = self.run_fixture()
        suite = self.suite()
        suite["cases"][0]["prompt"] += " changed"
        candidate = self.run_fixture(suite)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            base_path, _ = bench.write_run(baseline, root / "baseline")
            cand_path, _ = bench.write_run(candidate, root / "candidate")
            for flags in ([], ["--fail-on-regression"]):
                output = root / "comparison"
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    rc = bench.main(["compare", str(base_path), str(cand_path),
                                     "--output", str(output)] + flags)
                self.assertEqual(rc, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("rerun with matching prompts and expectations", stderr.getvalue())
                self.assertFalse(output.exists())
            output.mkdir()
            for name in ("comparison.json", "comparison.md"):
                (output / name).write_text("keep existing artifact")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(bench.main(["compare", str(base_path), str(cand_path),
                                             "--output", str(output)]), 2)
            self.assertTrue(all(p.read_text() == "keep existing artifact" for p in output.iterdir()))

    def test_cli_input_errors_are_not_regressions(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "input.json"
            for body in (None, "not JSON", '{"kind":"other"}'):
                with self.subTest(body=body):
                    if body is not None:
                        path.write_text(body)
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        self.assertEqual(bench.main(["compare", str(path), str(path)]), 2)
                    self.assertIn("cannot compare runs", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
