import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/learning-window/scripts'))
from consumer_return import consume
from window_state import _encode


class ReturnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.receipt = self.root / 'receipt.json'; self.output = self.root / 'output.json'
        receipt = {'scope': 'prod', 'since_ms': 0, 'until_ms': 100, 'membership_count': 1,
                   'rooms': [{'room_id': 'r', 'coverage': 'reached_cutoff', 'errors': [],
                              'messages': [{'event_id': 'e', 'ts': 50, 'body': 'Asked to test'}]}]}
        self.receipt.write_bytes(_encode(receipt))
        self.row = {'person_key': 'person', 'scope': 'prod', 'text': 'Candidate only',
                    'receipt_digest': hashlib.sha256(self.receipt.read_bytes()).hexdigest(),
                    'references': [{'room_id': 'r', 'event_id': 'e', 'excerpt': 'Asked to test'}]}

    def run_return(self, rows):
        self.output.write_text(json.dumps({'schema': 1, 'proposals': rows}))
        return consume(self.output, self.root / 'pending', [self.receipt], {'person': 'adapter-id'})

    def test_frozen_consumer_return_persists_real_pending_without_fact_success(self):
        result = self.run_return([self.row])
        self.assertEqual(result['proposal_return'], 'persisted')
        self.assertEqual(result['document_writes'], 0)
        state = json.loads((self.root / 'pending/pending-candidates.json').read_text())
        row = next(iter(state['candidates'].values()))
        self.assertEqual(row['store_identity'], 'adapter-id')
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(result['learning_outcome'], 'unverified')

    def test_model_success_and_store_identity_fields_not_accepted(self):
        for field in ['success', 'store_identity', 'document_sha256']:
            result = self.run_return([{**self.row, field: 'claimed'}])
            self.assertEqual(result['accepted_candidate_ids'], [])
        self.assertFalse((self.root / 'pending').exists())

    def test_cross_person_cross_receipt_cross_scope_rejected(self):
        for changed in [{'person_key': 'other'}, {'receipt_digest': '0' * 64}, {'scope': 'dev'}]:
            result = self.run_return([{**self.row, **changed}])
            self.assertEqual(result['proposal_return'], 'partial')
            self.assertEqual(result['accepted_candidate_ids'], [])

    def test_partial_return_retry_does_not_duplicate_accepted_candidate(self):
        result = self.run_return([self.row, {**self.row, 'person_key': 'other'}])
        self.assertEqual(len(result['accepted_candidate_ids']), 1)
        self.assertEqual(result['proposal_return'], 'partial')
        again = self.run_return([self.row])
        self.assertEqual(again['accepted_candidate_ids'], result['accepted_candidate_ids'])
        state = json.loads((self.root / 'pending/pending-candidates.json').read_text())
        self.assertEqual(len(state['candidates']), 1)

    def test_invalid_output_schema_size_and_symlink_refused(self):
        for data in [{'success': True}, {'schema': 1, 'proposals': [] , 'learning_outcome': 'success'}]:
            self.output.write_text(json.dumps(data))
            with self.assertRaises(ValueError): consume(self.output, self.root / 'pending', [self.receipt], {})
        self.output.write_text(' ' * 256001)
        with self.assertRaises(ValueError): consume(self.output, self.root / 'pending', [self.receipt], {})
        self.output.unlink(); self.output.symlink_to(self.receipt)
        with self.assertRaises(ValueError): consume(self.output, self.root / 'pending', [self.receipt], {})

    def test_observed_category_scope_and_status_return_refused_precisely(self):
        malformed = {**self.row, 'scope': 'role', 'status': 'pending_frozen_dossier'}
        result = self.run_return([malformed])
        self.assertIn('fields must be exactly', result['rejected'][0]['reason'])
        malformed.pop('status')
        result = self.run_return([malformed])
        self.assertIn('scope or coverage differs', result['rejected'][0]['reason'])
        self.assertFalse((self.root / 'pending').exists())

    def test_nonregular_output_refused_without_blocking(self):
        os.mkfifo(self.output)
        with self.assertRaises(ValueError):
            consume(self.output, self.root / 'pending', [self.receipt], {})


if __name__ == '__main__': unittest.main()
