#!/usr/bin/env python3
"""Missing proofs must never complete, deliver, or recover a task result."""
import ast
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
from delivery.readiness import read_ready_result
from delivery.task_dispatch import has_ready_result
from result_publish import publish_text
from result_write import receipt_verifier, receipts_dir_for


class ReceiptBoundary(unittest.TestCase):
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
            p = subprocess.run(argv, capture_output=True, text=True)
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(p.stdout, '')
            publish_text(path, 'paired')
            p = subprocess.run(argv, capture_output=True, text=True)
            self.assertEqual((p.returncode, p.stdout), (0, 'paired'))

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
        self.assertIn('attests = path_verifier(p)', owner)
        self.assertIn('if not attests(', owner)

    def test_raw_voice_reads_are_rejected_by_the_boundary_scan(self):
        import re
        pattern = re.compile(r"readFileSync\((?:path|p|resultPath|full|join\(this.resultDir,)\s*[,)]")
        for name in ['src/task-bridge.ts', 'src/task-delegation.ts',
                     'skills/phone-conversation/scripts/conversation-server.ts']:
            source = (REPO / name).read_text()
            self.assertIn('readVerifiedResult', source)
            # Task header p reads are unrelated to result consumption.
            if name == 'src/task-bridge.ts':
                source = source[:source.index('function _readTaskHeader')] + source[source.index('export function startResultWatcher'):] if 'function _readTaskHeader' in source and 'export function startResultWatcher' in source else source
            self.assertFalse(pattern.search(source), name)
        self.assertTrue(pattern.search("readFileSync(resultPath, 'utf8')"))


if __name__ == '__main__':
    unittest.main()
