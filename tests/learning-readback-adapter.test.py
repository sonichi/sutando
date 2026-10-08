import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/learning-window/scripts'))
from readback_adapter import capture_person, parse_person, capture_inventory
from unittest.mock import patch
from document_effect import digest, compare_inventory
NOW = 1791200000


def response(doc='old context'):
    return {'ok': True, 'body': {'person': {'id': 'adapter-id', 'slug': 'person', 'doc': doc}}}


class ReadbackTests(unittest.TestCase):
    def test_wrapped_full_utf8_document_preserves_hash_and_unknown_truth(self):
        row = parse_person(response('中文\nfull text'), 'person', 'adapter-id', NOW)
        self.assertEqual(row['document_sha256'], digest('中文\nfull text'))
        self.assertEqual(row['semantic_accuracy'], 'unknown')
        self.assertEqual(row['authority'], 'unknown')

    def test_list_and_missing_or_null_document_do_not_become_empty(self):
        cases = [{'ok': True, 'body': {'people': []}}, response(None),
                 {'ok': True, 'body': {'person': {'id': 'adapter-id', 'slug': 'person'}}}]
        for value in cases:
            with self.assertRaises(ValueError): parse_person(value, 'person', 'adapter-id', NOW)

    def test_cross_person_transport_identity_is_refused(self):
        for key in ['id', 'slug']:
            value = response(); value['body']['person'][key] = 'other'
            with self.assertRaises(ValueError): parse_person(value, 'person', 'adapter-id', NOW)

    def test_failed_truncated_or_malformed_envelope_is_refused(self):
        for value in [{'ok': False, 'body': {}}, {**response(), 'truncated': True},
                      {'ok': True, 'body': {'doc': 'flat'}},
                      {'ok': True, 'body': {'person': {**response()['body']['person'], 'truncated': True}}}]:
            with self.assertRaises(ValueError): parse_person(value, 'person', 'adapter-id', NOW)

    def test_readbacks_delegate_to_effect_classifier_without_fact_success(self):
        before = {'readback': 'verified', **parse_person(response(), 'person', 'adapter-id', NOW - 1)}
        after = {'readback': 'verified', **parse_person(response('old context\nnew claim'), 'person', 'adapter-id', NOW)}
        effect = compare_inventory({'person': before}, {'person': after})['person']
        self.assertEqual(effect['physical_retention'], 'changed_unattributed')
        self.assertEqual(effect['semantic_accuracy'], 'unknown')

    def test_retention_refuses_stale_reversed_or_missing_observations(self):
        first = {'readback': 'verified', **parse_person(response(), 'person', 'adapter-id', NOW)}
        for clock in [NOW - 1, NOW + 301]:
            last = {'readback': 'verified', **parse_person(response(), 'person', 'adapter-id', clock)}
            self.assertEqual(compare_inventory({'person': first}, {'person': last})['person']['physical_retention'], 'unknown')
        self.assertEqual(compare_inventory({'person': first}, {})['person']['physical_retention'], 'unknown')

    def test_partial_inventory_retains_good_hash_without_plaintext_or_success(self):
        def read(config, key):
            if key == 'bad': raise ValueError('unknown')
            return parse_person(response(), 'person', 'adapter-id', NOW)
        with patch('readback_adapter.capture_person', side_effect=read):
            got = capture_inventory({'proposal_stores': {'person': 'adapter-id', 'bad': 'other'}})
        self.assertEqual(got['bad']['readback'], 'unknown')
        self.assertEqual(got['person']['readback'], 'verified')
        self.assertNotIn('document', got['person'])

    def test_actual_injected_command_capture_is_read_only_and_inventory_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / 'fixture.py'
            command.write_text('import json,sys\nassert sys.argv[1]=="person"\nprint('+repr(json.dumps(response()))+')\n')
            config = {'document_readback_argv': [sys.executable, str(command)], 'proposal_stores': {'person': 'adapter-id'}}
            self.assertEqual(capture_person(config, 'person')['document'], 'old context')
            with self.assertRaises(ValueError): capture_person(config, 'other')

    def test_actual_command_failure_and_oversize_response_stay_unknown(self):
        for code in ['import sys;sys.exit(1)', 'print("x"*2000001)']:
            config = {'document_readback_argv': [sys.executable, '-c', code], 'proposal_stores': {'person': 'adapter-id'}}
            with self.assertRaises(ValueError): capture_person(config, 'person')


if __name__ == '__main__': unittest.main()
