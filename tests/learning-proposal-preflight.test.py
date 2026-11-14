import hashlib
import contextlib
import io
from unittest.mock import patch
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/learning-window/scripts'))
import check_return
from consumer_return import consume
from window_state import _encode
ENTRY = ROOT / 'skills/learning-window/scripts/check_return.py'


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.receipt = self.root / 'receipt.json'; self.output = self.root / 'output.json'; self.context = self.root / 'context.json'
        self.receipt.write_bytes(_encode({'scope': 'prod', 'since_ms': 0, 'until_ms': 100, 'membership_count': 1,
            'rooms': [{'room_id': 'r', 'coverage': 'reached_cutoff', 'errors': [], 'messages': [{'event_id': 'e', 'ts': 50, 'body': 'Asked for a test. Then reviewed.'}]}]}))
        self.row = {'person_key': 'person', 'scope': 'prod', 'text': 'Untrusted candidate',
            'receipt_digest': hashlib.sha256(self.receipt.read_bytes()).hexdigest(),
            'references': [{'room_id': 'r', 'event_id': 'e', 'excerpt': 'Asked for a test.'}]}
        self.context.write_text(json.dumps({'output_path': str(self.output), 'receipt_paths': [str(self.receipt)], 'stores': {'person': 'adapter-id'}}))

    def call(self, rows):
        self.output.write_text(json.dumps({'schema': 1, 'proposals': rows}))
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        result = subprocess.run([sys.executable, str(ENTRY), '--context', str(self.context)], capture_output=True, text=True, timeout=5)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        output = io.StringIO()
        with patch.object(sys, 'argv', [str(ENTRY), '--context', str(self.context)]), contextlib.redirect_stdout(output):
            code = check_return.main()
        self.assertEqual(code, result.returncode)
        self.assertEqual(json.loads(output.getvalue()), json.loads(result.stdout))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        return result.returncode, json.loads(result.stdout)

    def test_missing_or_wrong_context_is_unknown_without_writes(self):
        for context in (None, {}, {'output_path': 'missing', 'receipt_paths': [], 'stores': {}}):
            self.context.write_text(json.dumps(context))
            output = io.StringIO()
            with patch.object(sys, 'argv', [str(ENTRY), '--context', str(self.context)]), contextlib.redirect_stdout(output):
                code = check_return.main()
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())['pending_writes'], 0)
            self.assertFalse((self.root / 'pending').exists())

    def test_actual_cli_valid_preflight_does_not_persist_or_acknowledge(self):
        code, result = self.call([self.row])
        self.assertEqual(code, 0); self.assertEqual(result['valid_indexes'], [0])
        self.assertEqual(result['pending_writes'], 0); self.assertEqual(result['learning_outcome'], 'unverified')

    def test_actual_cli_elided_quote_and_wrong_event_match_production_rejections(self):
        bad = {**self.row, 'references': [{'room_id': 'r', 'event_id': 'e', 'excerpt': 'Asked ... Then reviewed.'}]}
        wrong = {**self.row, 'references': [{'room_id': 'r', 'event_id': 'other', 'excerpt': 'Asked for a test.'}]}
        code, result = self.call([self.row, bad, wrong])
        self.assertEqual(code, 2); self.assertEqual(result['valid_indexes'], [0])
        written = consume(self.output, self.root / 'pending', [self.receipt], {'person': 'adapter-id'})
        self.assertEqual(result['rejected'], written['rejected'])
        self.assertEqual(len(written['accepted_candidate_ids']), 1)

    def test_actual_cli_reports_overlap_separately_from_unique_events(self):
        first = json.loads(self.receipt.read_text())
        first['rooms'][0]['messages'][0]['sender'] = '@account:prod'
        self.receipt.write_bytes(_encode(first))
        self.row['receipt_digest'] = hashlib.sha256(self.receipt.read_bytes()).hexdigest()
        overlapping = self.root / 'overlap.json'
        second = json.loads(json.dumps(first)); second['since_ms'] = 1
        overlapping.write_bytes(_encode(second))
        other = self.root / 'other-scope.json'
        third = json.loads(json.dumps(first)); third['scope'] = 'dev'
        other.write_bytes(_encode(third))
        empty = self.root / 'empty.json'
        empty.write_bytes(_encode({'scope': 'dev', 'since_ms': 0, 'until_ms': 100,
                                  'membership_count': 0, 'rooms': []}))
        paths = []
        for path in [self.receipt, overlapping, other, empty]:
            named = self.root / (hashlib.sha256(path.read_bytes()).hexdigest() + '.json')
            path.rename(named); paths.append(str(named))
        self.context.write_text(json.dumps({'output_path': str(self.output),
            'receipt_paths': paths,
            'stores': {'person': 'adapter-id'}}))
        code, result = self.call([self.row])
        self.assertEqual(code, 0)
        population = result['receipt_population']
        self.assertEqual(population['receipt_count'], 4)
        self.assertEqual(population['message_rows'], 3)
        self.assertEqual(population['unique_scope_room_events'], 2)
        self.assertEqual(population['by_scope']['prod']['message_rows'], 2)
        self.assertEqual(population['by_scope']['prod']['unique_room_events'], 1)
        self.assertEqual(population['by_scope']['dev']['receipt_count'], 2)
        self.assertEqual(population['by_scope']['prod']['sender_message_rows'], {'@account:prod': 2})
        self.assertEqual(population['by_scope']['prod']['sender_unique_room_events'], {'@account:prod': 1})
        self.assertEqual(population['semantic_accuracy'], 'unknown')
        self.assertEqual(result['pending_writes'], 0)

    def test_unverified_population_does_not_change_proposal_acceptance(self):
        code, result = self.call([self.row])
        self.assertEqual(code, 0)
        self.assertEqual(result['valid_indexes'], [0])
        self.assertEqual(result['receipt_population']['status'], 'unknown')
        self.assertNotIn('message_rows', result['receipt_population'])

    def test_unknown_context_and_output_are_bounded_and_symlink_refused(self):
        self.context.write_text(' ' * 256001)
        result = subprocess.run([sys.executable, str(ENTRY), '--context', str(self.context)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1); self.assertEqual(json.loads(result.stdout)['proposal_validation'], 'unknown')
        self.context.unlink(); self.context.symlink_to(self.receipt)
        result = subprocess.run([sys.executable, str(ENTRY), '--context', str(self.context)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)


if __name__ == '__main__': unittest.main()
