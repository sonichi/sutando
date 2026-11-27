#!/usr/bin/env python3
"""Helper-owned hermetic HOME and coverage directories follow their creator process."""
from __future__ import annotations

import ast
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"


def module_calls(tree: ast.AST, name: str) -> "list[int]":
    lines: "list[int]" = []

    class Calls(ast.NodeVisitor):
        depth = 0

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_Lambda = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            dotted = (f"{func.value.id}.{func.attr}" if isinstance(func, ast.Attribute)
                      and isinstance(func.value, ast.Name) else getattr(func, "id", ""))
            if not self.depth and dotted == name:
                lines.append(node.lineno)
            self.generic_visit(node)

    Calls().visit(tree)
    return lines


class TempLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="hermetic-lifecycle-")
        self.root = Path(self.temp.name)
        self.coverage_source = self.root / "coverage-package"
        self.coverage_source.mkdir()
        self.explicit_home = self.root / "explicit-home"
        self.explicit_home.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_child(self, body: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(body)],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def assert_removed(self, *paths: str) -> None:
        for path in paths:
            self.assertFalse(Path(path).exists(), path)

    def test_normal_exit_removes_owned_directories_only(self) -> None:
        proc = self.run_child(f"""
            import json
            import sys
            sys.path.insert(0, {str(TESTS)!r})
            from _helpers.hermetic_env import coverage_bootstrap, hermetic_env

            payload = {{
                "homes": [hermetic_env()["HOME"] for _ in range(2)],
                "coverage": coverage_bootstrap({str(self.coverage_source)!r}),
                "explicit": hermetic_env({str(self.explicit_home)!r})["HOME"],
            }}
            print(json.dumps(payload), flush=True)
        """)
        payload = json.loads(proc.stdout)
        self.assert_removed(*payload["homes"], payload["coverage"])
        self.assertTrue(self.explicit_home.is_dir())

    def test_explicit_cleanup_supports_an_immediate_hard_exit(self) -> None:
        proc = self.run_child(f"""
            import json
            import os
            import sys
            sys.path.insert(0, {str(TESTS)!r})
            from _helpers.hermetic_env import cleanup_hermetic_temp_dirs, coverage_bootstrap, hermetic_env

            payload = {{
                "home": hermetic_env()["HOME"],
                "coverage": coverage_bootstrap({str(self.coverage_source)!r}),
                "explicit": hermetic_env({str(self.explicit_home)!r})["HOME"],
            }}
            print(json.dumps(payload), flush=True)
            cleanup_hermetic_temp_dirs()
            os._exit(0)
        """)
        payload = json.loads(proc.stdout)
        self.assert_removed(payload["home"], payload["coverage"])
        self.assertTrue(self.explicit_home.is_dir())

    def test_each_directory_keeps_its_atexit_order(self) -> None:
        record = self.root / "atexit-order.json"
        proc = self.run_child(f"""
            import atexit
            import json
            import sys
            from pathlib import Path
            sys.path.insert(0, {str(TESTS)!r})
            from _helpers.hermetic_env import hermetic_env

            first = hermetic_env()["HOME"]
            second = None
            def record_order():
                Path({str(record)!r}).write_text(json.dumps({{
                    "first_exists": Path(first).exists(),
                    "second_exists": Path(second).exists(),
                }}))
            atexit.register(record_order)
            second = hermetic_env()["HOME"]
            print(json.dumps({{"first": first, "second": second}}), flush=True)
        """)
        payload = json.loads(proc.stdout)
        self.assertEqual(json.loads(record.read_text()),
                         {"first_exists": True, "second_exists": False})
        self.assert_removed(payload["first"], payload["second"])

    @unittest.skipUnless(hasattr(os, "fork"), "requires os.fork")
    def test_forked_child_cannot_remove_the_parent_home(self) -> None:
        child_record = self.root / "fork-child.json"
        proc = self.run_child(f"""
            import json
            import os
            import sys
            from pathlib import Path
            sys.path.insert(0, {str(TESTS)!r})
            from _helpers.hermetic_env import hermetic_env

            parent_home = hermetic_env()["HOME"]
            pid = os.fork()
            if pid == 0:
                child_home = hermetic_env()["HOME"]
                Path({str(child_record)!r}).write_text(json.dumps({{"child": child_home}}))
                raise SystemExit(0)
            _, status = os.waitpid(pid, 0)
            payload = json.loads(Path({str(child_record)!r}).read_text())
            assert os.waitstatus_to_exitcode(status) == 0
            assert not Path(payload["child"]).exists()
            assert Path(parent_home).is_dir()
            print(json.dumps({{"parent": parent_home, **payload}}), flush=True)
        """)
        payload = json.loads(proc.stdout)
        self.assert_removed(payload["parent"], payload["child"])

    @unittest.skipUnless(hasattr(os, "fork"), "requires os.fork")
    def test_sentinel_hard_exit_cleans_a_fork_child(self) -> None:
        audit_record = self.root / "audit.json"
        child_record = self.root / "sentinel-child-home"
        code = f"""
import os
import sys
from pathlib import Path
sys.path.insert(0, {str(TESTS)!r})
from _helpers.hermetic_env import hermetic_env

pid = os.fork()
if pid == 0:
    home = hermetic_env()["HOME"]
    Path({str(child_record)!r}).write_text(home)
    os._exit(0)
_, status = os.waitpid(pid, 0)
assert os.waitstatus_to_exitcode(status) == 0
assert not Path(Path({str(child_record)!r}).read_text()).exists()
"""
        proc = subprocess.run(
            [sys.executable, str(TESTS / "_helpers" / "sentinel_audit.py"),
             str(audit_record), "-c", code],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(audit_record.is_file())
        self.assert_removed(child_record.read_text())

    def test_multiprocessing_workers_remove_their_directories(self) -> None:
        methods = [method for method in ("spawn", "fork")
                   if method in multiprocessing.get_all_start_methods()]
        script = self.root / "multiprocessing-worker.py"
        script.write_text(textwrap.dedent(f"""
            import json
            import multiprocessing
            import sys
            from pathlib import Path

            sys.path.insert(0, {str(TESTS)!r})
            from _helpers.hermetic_env import coverage_bootstrap, hermetic_env

            def worker(out, source):
                payload = {{"home": hermetic_env()["HOME"],
                           "coverage": coverage_bootstrap(source)}}
                Path(out).write_text(json.dumps(payload))

            if __name__ == "__main__":
                context = multiprocessing.get_context(sys.argv[1])
                process = context.Process(target=worker, args=(sys.argv[2], sys.argv[3]))
                process.start()
                process.join(30)
                if process.is_alive():
                    process.kill()
                    process.join()
                    raise RuntimeError("worker did not exit")
                if process.exitcode != 0:
                    raise RuntimeError(f"worker exited {{process.exitcode}}")
                print(Path(sys.argv[2]).read_text(), flush=True)
        """))
        for method in methods:
            with self.subTest(method=method):
                record = self.root / f"{method}.json"
                proc = subprocess.run(
                    [sys.executable, str(script), method, str(record), str(self.coverage_source)],
                    cwd=REPO,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                payload = json.loads(proc.stdout)
                self.assert_removed(payload["home"], payload["coverage"])

    def test_module_hard_exits_clean_up_first(self) -> None:
        failures = []
        for path in TESTS.rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            imports_helper = any(isinstance(node, ast.ImportFrom)
                                 and node.module == "_helpers.hermetic_env" for node in tree.body)
            exits = module_calls(tree, "os._exit") if imports_helper else []
            cleanups = module_calls(tree, "cleanup_hermetic_temp_dirs")
            for line in exits:
                if not any(cleanup < line for cleanup in cleanups):
                    failures.append(f"{path.relative_to(REPO)}:{line}")
        self.assertEqual(failures, [], "module hard exits without hermetic cleanup: " + ", ".join(failures))


if __name__ == "__main__":
    unittest.main()
