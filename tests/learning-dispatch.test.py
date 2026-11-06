#!/usr/bin/env python3
import json
import hashlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/learning-window/scripts"))
import dispatch_collection as dispatcher


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state"
        self.config = {"consumer_argv": ["consumer", "-p"], "consumer_prompt": "Learn evidence", "bootstrap_ms": 0,
                       "capabilities": {"prod": ["prod"], "dev": ["dev"]}}
        self.calls = []

    def runner(self, argv, **kwargs):
        if argv[0] == "consumer":
            self.calls.append(argv)
            self.assertEqual(len(list((self.path / "receipts").glob("*.json"))), 2)
            self.assertIn("Do not run the legacy sweep", argv[-1])
            return SimpleNamespace(returncode=0)
        value = {"ok": True, "rooms": ["!room"]}
        if argv[2] == "history":
            value.update(scope=argv[0], membership_count=1, since_ms=0, until_ms=100,
                         rooms=[{"room_id": "!room", "messages": [], "errors": [], "coverage": "server_no_cursor"}])
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))

    def test_bad_store_or_checker_config_has_no_state_effect(self):
        for change in ({'proposal_stores': []}, {'proposal_stores': {'person': ''}},
                       {'proposal_check_argv': ['checker']}, {'proposal_stores': {'person': 'id'}, 'proposal_check_argv': []}):
            with self.assertRaises(ValueError):
                dispatcher.dispatch({**self.config, **change}, self.path, 100, self.runner)
            self.assertFalse(self.path.exists())
        self.assertEqual(self.calls, [])

    def test_malformed_optional_readback_config_fails_before_state_or_consumer(self):
        for argv, stores in [([], {'person': 'id'}), (['read'], {}), ('read', {'person': 'id'})]:
            config = {**self.config, 'document_readback_argv': argv, 'proposal_stores': stores}
            with self.assertRaises(ValueError): dispatcher.dispatch(config, self.path, 100, self.runner)
            self.assertFalse(self.path.exists())
        self.assertEqual(self.calls, [])

    def test_malformed_optional_checker_config_fails_before_state_or_consumer(self):
        for argv, stores in [([], {'person': 'id'}), (['check'], {}), ('check', {'person': 'id'}), ([1], {'person': 'id'})]:
            config = {**self.config, 'proposal_check_argv': argv, 'proposal_stores': stores}
            with self.assertRaises(ValueError): dispatcher.dispatch(config, self.path, 100, self.runner)
            self.assertFalse(self.path.exists())
        self.assertEqual(self.calls, [])

    def test_receipts_exist_before_real_dispatch_consumer_boundary(self):
        got = dispatcher.dispatch(self.config, self.path, 100, self.runner)
        self.assertEqual(got["phase"], "consumer_exited")
        self.assertEqual(got["learning_outcome"], "unverified")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual({row["scope"] for row in got["collection_summaries"]}, {"prod", "dev"})
        self.assertTrue(all(row["until_iso"] == "1970-01-01T00:00:00.100000+00:00" for row in got["collection_summaries"]))

    def test_optional_real_writer_return_persists_frozen_candidate(self):
        self.config['proposal_stores'] = {'person': 'adapter-store'}
        def run(argv, **kwargs):
            if argv[0] == 'consumer':
                contract = json.loads(next(line[26:] for line in argv[-1].splitlines() if line.startswith('Proposal return contract: ')))
                receipt = next(p for p in (self.path / 'receipts').glob('*.json') if json.loads(p.read_text())['scope'] == 'prod')
                row = {'person_key': 'person', 'scope': 'prod', 'receipt_digest': hashlib.sha256(receipt.read_bytes()).hexdigest(),
                       'text': 'Proposed pending fact', 'references': [{'room_id': '!room', 'event_id': 'e', 'excerpt': 'Asked'}]}
                Path(contract['path']).write_text(json.dumps({'schema': 1, 'proposals': [row]}))
                return SimpleNamespace(returncode=0)
            result = self.runner(argv, **kwargs)
            if argv[2] == 'history':
                value = json.loads(result.stdout)
                value['rooms'][0]['messages'] = [{'event_id': 'e', 'ts': 50, 'body': 'Asked for a test'}]
                result.stdout = json.dumps(value)
            return result
        got = dispatcher.dispatch(self.config, self.path, 100, run)
        self.assertEqual(got['proposal_return']['proposal_return'], 'persisted')
        self.assertEqual(got['learning_outcome'], 'unverified')
        state = json.loads((self.path / 'pending-facts/pending-candidates.json').read_text())
        self.assertEqual(next(iter(state['candidates'].values()))['status'], 'pending')

    def test_old_or_absent_return_is_not_a_new_consumer_success(self):
        self.config['proposal_stores'] = {'person': 'adapter-store'}
        old = self.path / 'consumer-returns/old.json'
        old.parent.mkdir(parents=True)
        old.write_text(json.dumps({'schema': 1, 'proposals': []}))
        got = dispatcher.dispatch(self.config, self.path, 100, self.runner)
        self.assertNotEqual(got['proposal_output_path'], str(old))
        self.assertEqual(got['proposal_return']['proposal_return'], 'unknown')
        self.assertEqual(got['learning_outcome'], 'unverified')
        self.assertFalse((self.path / 'pending-facts').exists())

    def test_model_claimed_success_return_is_refused(self):
        self.config['proposal_stores'] = {'person': 'adapter-store'}
        def run(argv, **kwargs):
            if argv[0] == 'consumer':
                contract = json.loads(next(line[26:] for line in argv[-1].splitlines() if line.startswith('Proposal return contract: ')))
                Path(contract['path']).write_text(json.dumps({'success': True}))
                return SimpleNamespace(returncode=0)
            return self.runner(argv, **kwargs)
        got = dispatcher.dispatch(self.config, self.path, 100, run)
        self.assertEqual(got['proposal_return']['proposal_return'], 'unknown')
        self.assertEqual(got['learning_outcome'], 'unverified')

    def test_incomplete_scope_never_starts_consumer(self):
        def run(argv, **kwargs):
            if argv[0] == "dev":
                raise TimeoutError()
            return self.runner(argv, **kwargs)
        got = dispatcher.dispatch(self.config, self.path, 100, run)
        self.assertEqual(got["phase"], "collection_incomplete")
        self.assertEqual(self.calls, [])
        self.assertEqual(len(list((self.path / "receipts").glob("*.json"))), 1)

    def test_room_timeout_persists_canonical_incomplete_scope_before_refusal(self):
        def run(argv, **kwargs):
            result = self.runner(argv, **kwargs)
            if argv[0] == "prod" and argv[2] == "history":
                value = json.loads(result.stdout)
                value['rooms'][0].update(errors=['TimeoutError'], coverage='read_failed')
                result.stdout = json.dumps(value)
            return result
        got = dispatcher.dispatch(self.config, self.path, 100, run)
        self.assertEqual(got['phase'], 'collection_incomplete')
        self.assertEqual(self.calls, [])
        self.assertFalse(got['consumer_started'])
        summaries = {r['scope']: r for r in got['collection_summaries']}
        self.assertEqual(summaries['prod']['unread_rooms'], ['!room'])
        self.assertFalse(summaries['prod']['complete_available_history'])
        self.assertTrue(summaries['dev']['complete_available_history'])
        self.assertEqual(summaries['prod']['until_iso'], '1970-01-01T00:00:00.100000+00:00')
        self.assertEqual(json.loads((self.path / 'dispatch-state.json').read_text())['collection_summaries'], got['collection_summaries'])
        self.assertEqual(len(list((self.path / 'receipts').glob('*.json'))), 2)

    def test_consumer_failure_retains_all_unacknowledged_receipts(self):
        def run(argv, **kwargs):
            if argv[0] == "consumer":
                raise TimeoutError()
            return self.runner(argv, **kwargs)
        got = dispatcher.dispatch(self.config, self.path, 100, run)
        self.assertEqual(got["phase"], "failed")
        self.assertEqual(got["learning_outcome"], "unverified")
        self.assertIsNone(got["consumer_started"])
        self.assertEqual(len(list((self.path / "receipts").glob("*.json"))), 2)

    def test_parallel_dispatch_is_refused(self):
        with patch.object(dispatcher.fcntl, "flock", side_effect=BlockingIOError()):
            got = dispatcher.dispatch(self.config, self.path, 100, self.runner)
        self.assertEqual(got["phase"], "already_running")
        self.assertEqual(self.calls, [])

    def test_old_unacknowledged_receipts_are_supplied_on_later_pass(self):
        bundles = self.path / "receipts"
        bundles.mkdir(parents=True)
        for scope in ("prod", "dev"):
            data = dispatcher._encode({"scope": scope, "since_ms": 0, "until_ms": 50,
                                       "membership_count": 0, "rooms": []})
            (bundles / (hashlib.sha256(data).hexdigest() + ".json")).write_bytes(data)
        def run(argv, **kwargs):
            if argv[0] == "consumer":
                self.assertEqual(len(list(bundles.glob("*.json"))), 4)
                for bundle in bundles.glob("*.json"):
                    self.assertIn(str(bundle), argv[-1])
                return SimpleNamespace(returncode=0)
            return self.runner(argv, **kwargs)
        got = dispatcher.dispatch(self.config, self.path, 100, run)
        self.assertEqual(len(got["receipt_paths"]), 4)
        self.assertEqual({row["until_ms"] for row in got["collection_summaries"]}, {50, 100})
        self.assertEqual(got["learning_outcome"], "unverified")

    def test_corrupted_retained_bundle_refuses_consumer_start(self):
        bundles = self.path / "receipts"
        bundles.mkdir(parents=True)
        (bundles / ("0" * 64 + ".json")).write_text("corrupted")
        got = dispatcher.dispatch(self.config, self.path, 100, self.runner)
        self.assertEqual(got["phase"], "failed")
        self.assertFalse(got["consumer_started"])
        self.assertEqual(self.calls, [])

    def test_valid_digest_but_invalid_evidence_refuses_consumer_start(self):
        bundles = self.path / "receipts"
        bundles.mkdir(parents=True)
        data = dispatcher._encode({"scope": "old", "since_ms": 0, "until_ms": 50,
                                   "membership_count": 1, "rooms": [{"room_id": "!room", "errors": [],
                                   "coverage": "server_no_cursor", "messages": [{"event_id": "$bad", "ts": 51}]}]})
        (bundles / (hashlib.sha256(data).hexdigest() + ".json")).write_bytes(data)
        got = dispatcher.dispatch(self.config, self.path, 100, self.runner)
        self.assertEqual(got["phase"], "failed")
        self.assertEqual(self.calls, [])
        self.assertEqual(len(list(bundles.glob("*.json"))), 3)

    def test_window_report_is_persisted_before_consumer_attempt(self):
        def run(argv, **kwargs):
            if argv[0] == "consumer":
                state = json.loads((self.path / "dispatch-state.json").read_text())
                self.assertEqual(state["phase"], "consumer_starting")
                self.assertEqual(len(state["collection_summaries"]), 2)
                self.assertTrue(all(row["until_ms"] == 100 for row in state["collection_summaries"]))
                raise TimeoutError()
            return self.runner(argv, **kwargs)
        got = dispatcher.dispatch(self.config, self.path, 100, run)
        self.assertEqual(got["phase"], "failed")
        self.assertEqual(len(got["collection_summaries"]), 2)
        self.assertIsNone(got["consumer_started"])

    def test_actual_detached_launcher_collects_both_scopes_before_consumer(self):
        base = Path(self.tmp.name)
        capability = base / "capability.py"
        capability.write_text('''import sys,json,datetime
scope=sys.argv[1];verb=sys.argv[3]
value={"ok":True,"rooms":["!room"]}
if verb=="history":
 a=sys.argv[4:];start=datetime.datetime.fromisoformat(a[a.index("--since")+1]).timestamp()*1000
 end=datetime.datetime.fromisoformat(a[a.index("--until")+1]).timestamp()*1000
 value.update(scope=scope,since_ms=start,until_ms=end,membership_count=1,
 rooms=[{"room_id":"!room","messages":[],"errors":[],"coverage":"server_no_cursor"}])
print(json.dumps(value))
''')
        marker = base / "consumer.json"
        consumer = base / "consumer.py"
        consumer.write_text('''import sys,json,pathlib
root=pathlib.Path(sys.argv[1]);files=list((root/"state"/"receipts").glob("*.json"))
assert len(files)==2
(root/"consumer.json").write_text(json.dumps({"receipts":len(files),"prompt":sys.argv[2]}))
''')
        config = {**self.config, "bootstrap_ms": 0,
                  "capabilities": {s: [sys.executable, str(capability), s] for s in ("prod", "dev")},
                  "consumer_argv": [sys.executable, str(consumer), str(base)]}
        manifest = base / "manifest.json"
        manifest.write_text(json.dumps({"config": config}))
        launcher = ROOT / "skills/learning-window/scripts/launch.sh"
        result = subprocess.run(["bash", str(launcher), str(manifest), str(self.path), str(base/"job.log")], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn("consumer and learning outcome pending", result.stdout)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            path = self.path / "dispatch-state.json"
            if path.exists() and json.loads(path.read_text()).get("phase") == "consumer_exited":
                break
            time.sleep(.02)
        state = json.loads(path.read_text())
        self.assertEqual(state["phase"], "consumer_exited", (base/"job.log").read_text())
        self.assertEqual(state["learning_outcome"], "unverified")
        self.assertEqual(json.loads(marker.read_text())["receipts"], 2)
        self.assertEqual({row["scope"] for row in state["collection_summaries"]}, {"prod", "dev"})
        self.assertTrue(all(row["learning_outcome"] == "unverified" for row in state["collection_summaries"]))


if __name__ == "__main__":
    unittest.main()
