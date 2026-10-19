"""The routine and Codex adapters behave exactly as at the merge base, e36e6fb7.

adapter_differential_driver.py runs each through its command line in fixed scenarios (a full inbox,
a restart, a pre-existing result, a 429, a timeout, a provider error, a full cap; a Codex restart that
resumes its thread and a turn timeout). Every provider call, every result byte and every state
file's real values must equal what the same driver recorded at the merge base.
Run:
    python3 packages/ag2-sparrow/tests/test_adapter_differential.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]
DRIVER = Path(__file__).resolve().parent / "adapter_differential_driver.py"

# Recorded with this driver at the merge base named in the file (the previous head gives the same).
RECORDED = json.loads((Path(__file__).resolve().parent / "adapter_differential_recorded.json").read_text())


class DifferentialTest(unittest.TestCase):
    def run_driver(self, adapter):
        with tempfile.TemporaryDirectory() as base:
            env = dict(os.environ, PYTHONPATH=str(PKG_ROOT), TMPDIR="/tmp")  # a stable gettempdir() on every host
            out = subprocess.run([sys.executable, str(DRIVER), adapter, base], env=env, capture_output=True,
                                 text=True, timeout=180)
            self.assertEqual(out.returncode, 0, out.stderr)
            return json.loads(out.stdout)

    def compare(self, adapter):
        got, want = self.run_driver(adapter), RECORDED[adapter]
        self.assertEqual(sorted(got), sorted(want), "the same scenarios")
        for scenario in want:
            with self.subTest(scenario=scenario):
                self.assertEqual(got[scenario]["provider"], want[scenario]["provider"], "every provider call")
                self.assertEqual(got[scenario]["results"], want[scenario]["results"], "every result, byte for byte")
                self.assertEqual(got[scenario]["state"], want[scenario]["state"], "every state file's values")
                self.assertEqual(got[scenario]["rc"], want[scenario]["rc"])

    def test_routine_is_unchanged(self):
        self.compare("routine")

    def test_codex_is_unchanged(self):
        self.compare("codex")

    def test_every_state_file_is_compared_on_its_own(self):
        spec = importlib.util.spec_from_file_location("adapter_differential_driver", DRIVER)
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        with tempfile.TemporaryDirectory() as base:
            work = Path(base)
            threads = work / "state" / "threads"
            threads.mkdir(parents=True)
            names = ["a" * 64, "b" * 64, "default"]
            for name in names:
                (threads / name).write_text("thr-%s\n!room:ag2.space\n" % name[0])
            clean = driver.read_state(work, work)
            self.assertEqual(sorted(k for k in clean if k.startswith("threads/")), ["threads/" + n for n in names])
            for name in names:
                with self.subTest(thread=name):
                    (threads / name).write_text("corrupted\n")
                    self.assertNotEqual(driver.read_state(work, work), clean)
                    (threads / name).write_text("thr-%s\n!room:ag2.space\n" % name[0])
            self.assertEqual(driver.read_state(work, work), clean)

    def driver(self):
        spec = importlib.util.spec_from_file_location("adapter_differential_driver", DRIVER)
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        return driver

    def synthetic(self, base):
        work = Path(base) / "case"
        (work / "results").mkdir(parents=True)
        (work / "state" / "threads").mkdir(parents=True)
        (work / "results" / "task-1.txt").write_text("may or may not (TimeoutError); not retried\n")
        (work / "state" / "done").write_text("task-1\n")
        (work / "state" / "fires.json").write_text(json.dumps([1, 2]))
        (work / "state" / "metrics.jsonl").write_text(json.dumps({"task": "task-1", "status": "ok",
                                                                   "chars": 120, "turn_s": 0.5}) + "\n")
        (work / "state" / "threads" / "a").write_text("thr-a\n")
        calls = [{"method": "turn/start", "params": {"text": "hello"}}]
        return work, calls

    def test_each_observable_mutation_changes_the_summary(self):
        driver = self.driver()
        with tempfile.TemporaryDirectory() as base:
            work, calls = self.synthetic(base)
            clean = driver.summary(Path(base), work, calls, "codex")
            mutations = {
                "a result byte": lambda: (work / "results" / "task-1.txt").write_text("may or may not (TimeoutError); not retried!\n"),
                "a JSON state value": lambda: (work / "state" / "fires.json").write_text(json.dumps([1, 3])),
                "the chars value": lambda: (work / "state" / "metrics.jsonl").write_text(json.dumps(
                    {"task": "task-1", "status": "ok", "chars": 121, "turn_s": 0.5}) + "\n"),
                "a metrics status": lambda: (work / "state" / "metrics.jsonl").write_text(json.dumps(
                    {"task": "task-1", "status": "timeout", "chars": 120, "turn_s": 0.5}) + "\n"),
                "a thread file": lambda: (work / "state" / "threads" / "a").write_text("thr-b\n"),
                "the done ledger": lambda: (work / "state" / "done").write_text("task-2\n"),
            }
            for name, mutate in mutations.items():
                with self.subTest(mutation=name):
                    work, calls = self.synthetic(tempfile.mkdtemp(dir=base))
                    clean = driver.summary(Path(base), work, calls, "codex")
                    mutate()
                    self.assertNotEqual(driver.summary(Path(base), work, calls, "codex"), clean)
            with self.subTest(mutation="a provider call field"):
                work, calls = self.synthetic(tempfile.mkdtemp(dir=base))
                clean = driver.summary(Path(base), work, calls, "codex")
                calls[0]["params"]["text"] = "hello!"
                self.assertNotEqual(driver.summary(Path(base), work, calls, "codex"), clean)
            with self.subTest(mutation="a timing value is ignored"):
                work, calls = self.synthetic(tempfile.mkdtemp(dir=base))
                clean = driver.summary(Path(base), work, calls, "codex")
                (work / "state" / "metrics.jsonl").write_text(json.dumps(
                    {"task": "task-1", "status": "ok", "chars": 120, "turn_s": 9.0}) + "\n")
                self.assertEqual(driver.summary(Path(base), work, calls, "codex"), clean)

    def test_the_timeout_alias_compares_equal_only_in_the_routine_result_body(self):
        driver = self.driver()

        def summary(adapter, where, token):
            with tempfile.TemporaryDirectory() as base:
                work, calls = self.synthetic(base)
                if where == "result":
                    (work / "results" / "task-1.txt").write_text("may or may not %s; not retried\n" % token)
                elif where == "state":
                    (work / "state" / "done").write_text("task-1 %s\n" % token)
                else:
                    calls[0]["params"]["text"] = token
                return driver.summary(Path(base), work, calls, adapter)
        self.assertEqual(summary("routine", "result", "(timeout)"), summary("routine", "result", "(TimeoutError)"))
        self.assertNotEqual(summary("routine", "result", "(OSError)"), summary("routine", "result", "(TimeoutError)"))
        self.assertNotEqual(summary("routine", "result", "timeout"), summary("routine", "result", "TimeoutError"),
                            "only the exact token (timeout)")
        for adapter, where in (("codex", "result"), ("routine", "state"), ("routine", "provider"),
                               ("codex", "state")):
            with self.subTest(adapter=adapter, where=where):
                self.assertNotEqual(summary(adapter, where, "(timeout)"), summary(adapter, where, "(TimeoutError)"))

    def test_the_recording_is_from_the_merge_base_and_covers_the_cases(self):
        self.assertEqual(RECORDED["merge_base"], "e36e6fb7c391580dedce73908b5d090a95137aa4")
        self.assertIn("single-flock contract", RECORDED["recorded_because"])
        self.assertEqual(sorted(RECORDED["routine"]), ["429 then ok", "basic", "cap 1 per hour, full",
                                                       "pre-existing result", "provider error", "restart",
                                                       "timeout"])
        self.assertEqual(sorted(RECORDED["codex"]), ["basic", "pre-existing result", "restart", "turn timeout"])
        routine = RECORDED["routine"]
        self.assertEqual(len(routine["basic"]["provider"]), 2, "two owner tasks with a room reach the routine")
        self.assertEqual(routine["cap 1 per hour, full"]["provider"], [], "a full cap fires nothing")
        self.assertEqual(len(routine["429 then ok"]["provider"]), 2, "the 429 is retried once")
        self.assertEqual(len(routine["pre-existing result"]["provider"]), 1, "an answered task is not fired")
        methods = [c["method"] for c in RECORDED["codex"]["restart"]["provider"]]
        self.assertIn("thread/resume", methods, "a restart resumes the channel's thread")


if __name__ == "__main__":
    unittest.main()
