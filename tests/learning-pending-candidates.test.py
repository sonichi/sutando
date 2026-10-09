import concurrent.futures
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/learning-window/scripts'))
from pending_candidates import propose
from window_state import _encode


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.receipt = self.root / 'receipt.json'
        self.data = {'scope': 'prod', 'since_ms': 0, 'until_ms': 100, 'membership_count': 1,
                     'rooms': [{'room_id': 'r', 'coverage': 'reached_cutoff', 'errors': [],
                                'messages': [{'event_id': 'e', 'ts': 50, 'body': 'A requested a test.'}]}]}
        self.receipt.write_bytes(_encode(self.data))
        self.proposal = {'store_identity': 'person-1', 'scope': 'prod', 'text': 'Proposed interpretation',
                         'receipt_digest': hashlib.sha256(self.receipt.read_bytes()).hexdigest(),
                         'references': [{'room_id': 'r', 'event_id': 'e', 'excerpt': 'requested a test'}]}

    def write(self, p=None):
        return propose(self.root / 'state', self.receipt, p or self.proposal, 'person-1')

    def test_frozen_candidate_pending_does_not_claim_learning(self):
        row = self.write()
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['document_effect'], 'unverified')
        self.assertEqual(row['semantic_accuracy'], 'unknown')
        self.assertTrue(self.receipt.exists())

    def test_duplicate_is_one_stable_proposal(self):
        self.assertEqual(self.write(), self.write())
        state = json.loads((self.root / 'state/pending-candidates.json').read_text())
        self.assertEqual(len(state['candidates']), 1)

    def test_changed_interpretation_is_distinct_and_not_automatically_true(self):
        first = self.write(); p = {**self.proposal, 'text': 'An incorrect inference'}
        second = self.write(p)
        self.assertNotEqual(first['candidate_id'], second['candidate_id'])
        self.assertEqual(second['semantic_accuracy'], 'unknown')

    def test_cross_scope_person_missing_event_and_invented_excerpt_refused(self):
        variants = [{**self.proposal, 'scope': 'dev'}, {**self.proposal, 'store_identity': 'person-2'},
                    {**self.proposal, 'references': [{'room_id': 'other', 'event_id': 'e', 'excerpt': 'test'}]},
                    {**self.proposal, 'references': [{'room_id': 'r', 'event_id': 'e', 'excerpt': 'invented'}]}]
        for p in variants:
            with self.assertRaises(ValueError): self.write(p)
        self.assertFalse((self.root / 'state').exists())

    def test_corruption_and_partial_coverage_refused_before_storage(self):
        self.receipt.write_bytes(self.receipt.read_bytes() + b' ')
        with self.assertRaises(ValueError): self.write()
        self.data['rooms'][0]['errors'] = ['timeout']
        self.receipt.write_bytes(_encode(self.data))
        self.proposal['receipt_digest'] = hashlib.sha256(self.receipt.read_bytes()).hexdigest()
        with self.assertRaises(ValueError): self.write()
        self.assertFalse((self.root / 'state').exists())

    def test_same_event_in_later_bundle_keeps_one_candidate_and_both_digests(self):
        first = self.write()
        self.data['until_ms'] = 200
        self.receipt.write_bytes(_encode(self.data))
        self.proposal['receipt_digest'] = hashlib.sha256(self.receipt.read_bytes()).hexdigest()
        second = self.write()
        self.assertEqual(first['candidate_id'], second['candidate_id'])
        self.assertEqual(len(second['receipt_digests']), 2)
        self.assertEqual(second['status'], 'pending')
        state = json.loads((self.root / 'state/pending-candidates.json').read_text())
        self.assertEqual(len(state['candidates']), 1)

    def test_malformed_reference_identity_refused_before_storage(self):
        p = copy.deepcopy(self.proposal)
        p['references'][0]['event_id'] = ['e']
        with self.assertRaises(ValueError): self.write(p)
        self.assertFalse((self.root / 'state').exists())

    def test_real_writer_concurrency_preserves_unrelated_candidates(self):
        def call(i): return self.write({**self.proposal, 'text': 'claim ' + str(i)})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(call, range(20)))
        state = json.loads((self.root / 'state/pending-candidates.json').read_text())
        self.assertEqual(len(state['candidates']), 20)
        self.assertEqual(set(state['candidates']), {r['candidate_id'] for r in rows})
        self.assertEqual((self.root / 'state/pending-candidates.json').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__': unittest.main()
