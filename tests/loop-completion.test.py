"""The production completion writer isolates hosts and publishes whole records."""
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from loop_completion import completion_path, read_completion, write_completion


class LoopCompletionTest(unittest.TestCase):
    def test_hosts_do_not_heal_each_other(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            write_completion(ws, "alpha", now=100)
            self.assertEqual(read_completion(ws, "alpha"), 100)
            self.assertIsNone(read_completion(ws, "beta"))

    def test_concurrent_completions_never_regress(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda ts: write_completion(ws, "alpha", now=ts), range(100)))
            self.assertEqual(read_completion(ws, "alpha"), 99)
            self.assertEqual(json.loads(completion_path(ws, "alpha").read_text())["host"], "alpha")

    def test_bad_records_do_not_prove_completion(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            path = completion_path(ws, "alpha")
            path.parent.mkdir(parents=True)
            for value in ([], {}, {"version": True, "host": "alpha", "completed_at": 1}, {"version": 1, "host": "other", "completed_at": 1},
                          {"version": 1, "host": "alpha", "completed_at": True},
                          {"version": 1, "host": "alpha", "completed_at": 10**400}):
                path.write_text(json.dumps(value))
                self.assertIsNone(read_completion(ws, "alpha"))

    def test_host_and_timestamp_bounds(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            for host in (None, 3, "", "..", "../other", "other/host"):
                with self.assertRaises(ValueError):
                    write_completion(ws, host)
            for ts in (True, -1, float("inf"), float("nan"), 10**400):
                with self.assertRaises(ValueError):
                    write_completion(ws, "alpha", now=ts)


if __name__ == "__main__":
    unittest.main()
