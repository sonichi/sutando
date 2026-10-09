import json
import contextlib
import io
from unittest.mock import patch
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/learning-window/scripts"))
from window_state import record_collection
from receipt_status import load_summaries
from collection_claims import summarize_population, validate_claims


class ClaimsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.receipt = {"scope": "prod", "since_ms": 1791178590546.104, "until_ms": 1791187259284.915,
                        "membership_count": 1, "rooms": [{"room_id": "!same", "errors": [],
                        "coverage": "server_no_cursor", "messages": [{"event_id": "$one", "ts": 1791187259000}]}]}

    def rows(self):
        record_collection(self.path, self.receipt["scope"], self.receipt, self.receipt["since_ms"])
        return load_summaries(sorted((self.path / "receipts").glob("*.json")))

    def test_receipt_report_cli_and_bounded_population_refuse_corruption(self):
        import receipt_status
        rows = self.rows()
        paths = sorted((self.path / 'receipts').glob('*.json'))
        claims = self.path / 'claims.json'
        claims.write_text(json.dumps(rows))
        output = io.StringIO()
        with patch.object(sys, 'argv', ['receipt_status', str(paths[0]), '--claims', str(claims)]), contextlib.redirect_stdout(output):
            receipt_status.main()
        self.assertEqual(json.loads(output.getvalue()), rows)
        self.assertEqual(receipt_status.load_population(paths)['unique_scope_room_events'], 1)
        for invalid in ([], None, [str(paths[0])] * 1001):
            with self.assertRaises(ValueError):
                receipt_status.load_population(invalid)
        wrong = self.path / 'wrong.json'
        wrong.write_bytes(paths[0].read_bytes())
        with self.assertRaises(ValueError):
            receipt_status.load_population([wrong])
        oversized = self.path / 'oversized.json'
        with oversized.open('wb') as stream:
            stream.truncate(4000001)
        with self.assertRaises(ValueError):
            receipt_status.load_population([oversized])

    def test_invalid_summaries_cannot_be_claimed_complete(self):
        from collection_claims import summarize
        for change in ({'membership_count': 2}, {'scope': ''}, {'rooms': [{'room_id': 'r', 'messages': None}]},
                       {'rooms': [{'room_id': 'r', 'messages': [{'event_id': '', 'ts': 1}]}]}):
            receipt = {**self.receipt, **change}
            with self.assertRaises(ValueError):
                summarize(receipt, 'digest')
        rows = self.rows()
        with self.assertRaises(ValueError):
            validate_claims(rows * 2, rows * 2)

    def test_real_writer_summary_uses_exact_utc_window(self):
        row = self.rows()[0]
        self.assertEqual(row["until_iso"], "2026-10-05T08:00:59.284915+00:00")
        self.assertEqual(row["since_iso"], "2026-10-05T05:36:30.546104+00:00")
        self.assertEqual(row["unique_events"], 1)
        self.assertEqual(row["learning_outcome"], "unverified")

    def test_observed_twenty_minute_error_rejected(self):
        rows = self.rows()
        claims = json.loads(json.dumps(rows))
        claims[0]["until_iso"] = "2026-10-05T07:40:59.284915+00:00"
        with self.assertRaises(ValueError):
            validate_claims(rows, claims)

    def test_empty_omitted_scope_refused(self):
        rows = self.rows()
        self.receipt.update(scope="dev", membership_count=0, rooms=[])
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        with self.assertRaises(ValueError):
            validate_claims(rows, rows[:1])
        self.assertTrue(validate_claims(rows, list(reversed(rows))))

    def test_corruption_refused(self):
        self.rows()
        path = next((self.path / "receipts").glob("*.json"))
        path.write_text(path.read_text() + " ")
        with self.assertRaises(ValueError):
            load_summaries([path])

    def test_partial_receipt_is_not_full_coverage(self):
        self.receipt["rooms"][0].update(coverage="read_failed", errors=["TimeoutError"])
        row = self.rows()[0]
        self.assertEqual(row["unread_rooms"], ["!same"])
        self.assertFalse(row["complete_available_history"])

    def test_collection_does_not_accept_learning_success(self):
        rows = self.rows()
        claims = json.loads(json.dumps(rows))
        claims[0]["learning_outcome"] = "success"
        with self.assertRaises(ValueError):
            validate_claims(rows, claims)

    def test_population_retains_partial_history_and_room_identity(self):
        first = json.loads(json.dumps(self.receipt))
        second = json.loads(json.dumps(self.receipt))
        second['rooms'][0].update(room_id='!other', coverage='read_failed', errors=['TimeoutError'])
        result = summarize_population([(first, 'first'), (second, 'second')])
        self.assertEqual(result['message_rows'], 2)
        self.assertEqual(result['unique_scope_room_events'], 2)
        self.assertEqual(result['by_scope']['prod']['incomplete_receipt_count'], 1)
        self.assertEqual(result['by_scope']['prod']['unknown_sender_rows'], 2)
        self.assertEqual(result['learning_outcome'], 'unverified')
        with self.assertRaises(ValueError):
            summarize_population([(first, 'same'), (second, 'same')])


if __name__ == "__main__":
    unittest.main()
