#!/usr/bin/env python3
"""Missing proofs must never complete, deliver, or recover a task result."""
import ast
import contextlib
import io
from unittest import mock
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _helpers.hermetic_env import isolated_child_env, isolate_os_environ
isolate_os_environ()
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
from delivery.readiness import read_ready_result
from delivery.task_dispatch import has_ready_result
from result_publish import publish_text
from result_write import receipt_verifier, receipts_dir_for


class ReceiptBoundary(unittest.TestCase):
    def test_staged_no_replace_preserves_first_published_answer(self):
        from result_publish import publish_staged
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "results" / "task-conflict.txt"
            path.parent.mkdir()
            staged = path.parent / "answer.tmp"
            staged.write_text("first answer")
            publish_staged(staged, path, no_replace=True)
            self.assertFalse(staged.exists())
            self.assertEqual(read_ready_result(path), "first answer")
            staged.write_text("second answer")
            with self.assertRaises(FileExistsError):
                publish_staged(staged, path, no_replace=True)
            self.assertEqual(read_ready_result(path), "first answer")
            self.assertEqual(staged.read_text(), "second answer")

    def test_read_cli_returns_only_verified_body_and_handles_read_failure(self):
        from result_write import _read_cli
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "results" / "task-read.txt"
            path.parent.mkdir()
            path.write_text("unpaired")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(_read_cli(path), 1)
                self.assertEqual(output.getvalue(), "")
                publish_text(path, "verified answer")
                self.assertEqual(_read_cli(path), 0)
            self.assertEqual(output.getvalue(), "verified answer")
            with mock.patch("delivery.readiness.read_ready_result", side_effect=OSError("unreadable")):
                self.assertEqual(_read_cli(path), 1)

    def test_unresolvable_receipt_namespace_fails_closed(self):
        import result_write
        with mock.patch.object(result_write, "result_context", return_value=None):
            self.assertFalse(result_write.path_verifier("task-a.txt")("answer"))

    def test_adapter_refuses_non_directory_pairing_state(self):
        from task_lifecycle import prepare_work_dir
        with tempfile.TemporaryDirectory() as td:
            work = Path(td) / "adapter"
            work.mkdir()
            self.assertIsNone(prepare_work_dir(str(work)))
            (work / "state" / "result-pairing").write_text("unexpected file")
            self.assertIn("result-pairing is not a plain directory",
                          prepare_work_dir(str(work), check_only=True))

    def test_history_keeps_task_working_when_result_is_unpaired(self):
        from task_workstreams import scan_task_history
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            tasks = workspace / "tasks"
            results = workspace / "results"
            tasks.mkdir()
            results.mkdir()
            (tasks / "task-history.txt").write_text(
                "id: task-history\nsource: discord\naccess_tier: owner\ntask: Explain the result\n")
            path = results / "task-history.txt"
            path.write_text("unverified answer")
            rows = scan_task_history(workspace)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, "working")
            self.assertEqual(rows[0].result, "")
            publish_text(path, "verified answer")
            rows = scan_task_history(workspace)
            self.assertEqual(rows[0].status, "done")
            self.assertEqual(rows[0].result, "verified answer")

    def test_unpaired_live_and_archive_are_not_completion(self):
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / 'results'
            for relative in ('task-a.txt', 'archive/task-a.txt', 'archive/2026-11/task-a-123.txt',
                             'archive-2026-11-06/task-a.txt'):
                path = results / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('answer\n')
                self.assertIsNone(read_ready_result(path))
                self.assertFalse(has_ready_result(results, 'task-a.txt'))
            self.assertFalse(receipt_verifier(receipts_dir_for(td), 'a')('answer\n'))

    def test_published_result_and_renamed_archive_are_verified(self):
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / 'results'
            path = results / 'task-a.txt'
            publish_text(path, 'answer\r\n')
            self.assertEqual(read_ready_result(path), 'answer')
            archive = results / 'archive' / 'task-a-123.txt'
            archive.parent.mkdir()
            path.rename(archive)
            publish_text(path, 'new answer\n')
            self.assertEqual(read_ready_result(archive), 'answer')
            self.assertEqual(read_ready_result(path), 'new answer')
            path.write_text('substituted\n')
            self.assertIsNone(read_ready_result(path))

    def test_read_cli_never_returns_unverified_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'results' / 'voice-a.txt'
            path.parent.mkdir()
            path.write_text('unpaired')
            argv = [sys.executable, str(REPO / 'src/result_write.py'), 'read', str(path)]
            p = subprocess.run(argv, capture_output=True, text=True, env=isolated_child_env())
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(p.stdout, '')
            publish_text(path, 'paired')
            p = subprocess.run(argv, capture_output=True, text=True, env=isolated_child_env())
            self.assertEqual((p.returncode, p.stdout), (0, 'paired'))

    def test_renamed_tagged_archives_verify_the_original_task(self):
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / "results"
            live = results / "task-a.txt"
            archive = results / "archive"
            archive.mkdir(parents=True)
            for suffix in ("-123", "-123-late-duplicate", "-123-late-duplicate.456", "-123.456"):
                publish_text(live, "original answer")
                target = archive / f"task-a{suffix}.txt"
                live.rename(target)
                publish_text(live, "new answer")
                self.assertEqual(read_ready_result(target), "original answer")

    def test_archive_numeric_suffix_cannot_complete_a_shorter_id(self):
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / "results"
            longer = results / "task-a-123.txt"
            publish_text(longer, "longer task answer")
            archive = results / "archive"
            archive.mkdir()
            longer.rename(archive / longer.name)
            self.assertFalse(has_ready_result(results, "task-a.txt"))
            self.assertTrue(has_ready_result(results, "task-a-123.txt"))

    def test_cli_custom_result_root_matches_default_reader(self):
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / "custom-output"
            process = subprocess.run(
                [sys.executable, str(REPO / "src/result_write.py"), "write", "a",
                 "--results-dir", str(results)], input="task: a\ncustom answer",
                capture_output=True, text=True, env=isolated_child_env())
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(read_ready_result(results / "task-a.txt"), "custom answer")

    def test_custom_result_root_pins_keep_their_receipt_namespace(self):
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / "custom-output"
            path = results / "task-a.txt"
            publish_text(path, "answer\r\n", pair_result=True)
            pin = results / ".delivering" / "task-a.pin"
            pin.parent.mkdir()
            path.rename(pin)
            publish_text(path, "new answer", pair_result=True)
            self.assertEqual(read_ready_result(pin), "answer")

    def test_notifier_completion_delegates_to_shared_dispatch(self):
        for provider in ("claude", "codex"):
            source = (REPO / f"src/agent/{provider}/cli/task-notifier.sh").read_text()
            self.assertIn("has_result()", source)
            self.assertIn("task_dispatch", source)
            self.assertNotRegex(source, r"cat [^\n]*(?:RESULTS_DIR|result_file)")
        connector = (REPO / "packages/ag2-sparrow/ag2_sparrow/adapters/codex/codex_connector.py").read_text()
        self.assertIn("read_ready_result(os.path.join(self.args.results, n))", connector)
        self.assertNotIn("os.path.exists(os.path.join(self.args.results, n))", connector)

    def test_sparrow_verifier_is_the_same_owner(self):
        for source, target in [('result_write.py', 'result_write.py'),
                               ('delivery/readiness.py', 'result_ready.py')]:
            self.assertEqual((REPO / 'src' / source).read_bytes(),
                             (REPO / 'packages/ag2-sparrow/ag2_sparrow' / target).read_bytes())

    def test_delivery_consumers_cannot_decode_raw_snapshots(self):
        consumers = ['src/discord-bridge.py', 'src/slack-bridge.py', 'src/telegram-bridge.py',
                     'packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py']
        for consumer in consumers:
            tree = ast.parse((REPO / consumer).read_text())
            calls = [n.func for n in ast.walk(tree) if isinstance(n, ast.Call)]
            self.assertFalse(any(isinstance(f, ast.Name) and f.id == 'ready_body_of' for f in calls), consumer)
            self.assertTrue(any(isinstance(f, ast.Name) and f.id == 'read_ready_result' for f in calls), consumer)
        owner = (REPO / 'src/delivery/readiness.py').read_text()
        self.assertIn('attests = path_verifier(p, task_id=task_id)', owner)
        self.assertIn('if not attests(', owner)

    def test_completion_functions_delegate_to_receipt_verification(self):
        inventory = {
            "src/health_snapshot.py": {"_has_result": "has_ready_result"},
            "src/task_workstreams.py": {"scan_task_history": "read_ready_result",
                                         "_exact_result": "read_ready_result"},
            "src/dedup_recovery.py": {"plan_dedup_recovery": "read_ready_result"},
            "src/telegram-bridge.py": {"poll_progress": "read_ready_result"},
            "src/discord-bridge.py": {"poll_progress": "read_ready_result"},
            "skills/agent-activity/hooks/activity-hook.py": {
                "cached_answered": "read_ready_result", "consolidated_into": "read_ready_result",
                "handle": "read_ready_result"},
            "skills/task-orphan-check/scripts/classify.py": {"completion_marker": "read_ready_result"},
            "packages/ag2-sparrow/ag2_sparrow/adapters/hatch/cli.py": {
                "task_settled": "read_ready_result", "_line_state": "_scan_line", "_scan_line": "read_ready_result"},
            "packages/ag2-sparrow/ag2_sparrow/adapters/hatch/task_fields.py": {
                "list_open_tasks": "read_ready_result"},
            "packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py": {
                "_delivered_copy_exists": "read_ready_result", "_watch_dispatch": "read_ready_result",
                "_resolve_stop": "read_ready_result", "_settle_as_handled": "read_ready_result",
                "_proactive_dispatch_veto": "read_ready_result",
                "_publish_task_steps_snapshot": "read_ready_result",
                "_holder_delivery_state": "read_ready_result"},
        }

        def delegates(function, owner):
            return any(isinstance(node, ast.Call) and
                       ((isinstance(node.func, ast.Name) and node.func.id == owner) or
                        (isinstance(node.func, ast.Attribute) and node.func.attr == owner))
                       for node in ast.walk(function))

        for name, expected in inventory.items():
            functions = {node.name: node for node in ast.walk(ast.parse((REPO / name).read_text()))
                         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for function, owner in expected.items():
                with self.subTest(file=name, function=function):
                    self.assertIn(function, sorted(functions))
                    self.assertTrue(delegates(functions[function], owner))
        for raw in ("return path.read_text()", "return result.exists()"):
            bypass = ast.parse("def completed(path, result):\n    " + raw).body[0]
            self.assertFalse(delegates(bypass, "read_ready_result"))

    def test_no_raw_result_reads_in_consumer_inventory(self):
        import re
        def python_bypasses(source):
            found = []
            for n in ast.walk(ast.parse(source)):
                if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
                    continue
                receiver = n.func.value
                if (n.func.attr in {"read_text", "read_bytes"} and isinstance(receiver, ast.Name)
                        and re.search(r"result|reply|rfile|archived", receiver.id, re.I)):
                    found.append(n.lineno)
            return found
        for name in ['src/agent-api.py', 'src/discord-bridge.py', 'src/slack-bridge.py',
                     'src/telegram-bridge.py', 'src/obsidian-mirror.py', 'src/task_workstreams.py',
                     'packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py']:
            self.assertEqual(python_bypasses((REPO / name).read_text()), [], name)
        self.assertTrue(python_bypasses("body = result_file.read_text()"))
        pattern = re.compile(r"readFileSync\((?:path|resultPath|full|join\(this.resultDir,)\s*[,)]")
        for name in ['src/task-bridge.ts', 'src/task-delegation.ts',
                     'skills/phone-conversation/scripts/conversation-server.ts']:
            source = (REPO / name).read_text()
            self.assertIn('readVerifiedResult', source)
            self.assertFalse(pattern.search(source), name)
        self.assertTrue(pattern.search("readFileSync(resultPath, 'utf8')"))


if __name__ == '__main__':
    unittest.main()
