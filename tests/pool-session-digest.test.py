#!/usr/bin/env python3
"""Contract for scripts/pool-session-digest.py.

The age column is the load-bearing part: an operator reads it to decide whether
a session is wedged. Transcripts stamp UTC, so reading them with a local-time
parser reports an age that is wrong by the UTC offset -- and correcting with
time.timezone rather than the DST-aware offset lands exactly one hour out, which
is precisely the shape of "stale" an operator is looking for. The first version
of this script shipped that bug and reported 3-second-old activity as 60m.
"""
import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "pool-session-digest.py"

_spec = importlib.util.spec_from_file_location("digest_mod", SCRIPT)
digest_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(digest_mod)


def utc_iso(offset_secs: int = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                         time.gmtime(time.time() - offset_secs))


class AgeTest(unittest.TestCase):
    def test_recent_utc_stamp_reads_as_seconds_not_hours(self):
        """A local-time parse of a UTC stamp is off by the whole UTC offset."""
        self.assertTrue(digest_mod.age(utc_iso(3)).endswith("s ago"),
                        "a 3s-old event must not read as minutes or hours")

    def test_age_scales(self):
        self.assertTrue(digest_mod.age(utc_iso(600)).endswith("m ago"))
        self.assertTrue(digest_mod.age(utc_iso(3 * 3600)).endswith("h ago"))

    def test_age_is_never_negative(self):
        """Clock skew must not render a future stamp as a huge negative age."""
        self.assertEqual(digest_mod.age(utc_iso(-120)), "0s ago")

    def test_unparsable_stamp_is_reported_not_raised(self):
        self.assertEqual(digest_mod.age("not-a-timestamp"), "?")
        self.assertEqual(digest_mod.age(""), "?")


class DigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s.jsonl"

    def _write(self, records):
        self.path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    def _rec(self, blocks):
        return {"type": "assistant", "timestamp": utc_iso(1),
                "message": {"content": blocks}}

    def test_counts_blocks_and_tolerates_junk_lines(self):
        self.path.write_text(
            json.dumps(self._rec([{"type": "text", "text": "hello"}])) + "\n"
            + "{ not json\n\n"
            + json.dumps(self._rec([{"type": "thinking", "thinking": "hmm"}])) + "\n")
        d = digest_mod.digest(self.path, keep=10, width=80, want_thinking=True)
        self.assertEqual(d["records"], 2, "a malformed line must not abort the scan")
        self.assertEqual(d["blocks"]["text"], 1)
        self.assertEqual(d["blocks"]["thinking"], 1)

    def test_thinking_is_counted_but_hidden_unless_requested(self):
        self._write([self._rec([{"type": "thinking", "thinking": "private"}])])
        d = digest_mod.digest(self.path, keep=10, width=80, want_thinking=False)
        self.assertEqual(d["blocks"]["thinking"], 1, "still counted")
        self.assertEqual(d["tail"], [], "reasoning must not leak without --thinking")

    def test_tail_is_bounded_to_keep(self):
        self._write([self._rec([{"type": "text", "text": f"m{i}"}]) for i in range(50)])
        d = digest_mod.digest(self.path, keep=5, width=80, want_thinking=False)
        self.assertEqual(len(d["tail"]), 5, "tail must stay bounded on huge transcripts")
        self.assertEqual(d["tail"][-1][2], "m49", "must keep the NEWEST events")

    def test_tool_use_summarises_the_command(self):
        self._write([self._rec([{"type": "tool_use", "name": "Bash",
                                 "input": {"command": "git status"}}])])
        d = digest_mod.digest(self.path, keep=5, width=80, want_thinking=False)
        _, kind, body = d["tail"][0]
        self.assertEqual(kind, "BASH")
        self.assertEqual(body, "git status")

    def test_long_summary_is_truncated_to_width(self):
        self._write([self._rec([{"type": "text", "text": "x" * 500}])])
        d = digest_mod.digest(self.path, keep=5, width=40, want_thinking=False)
        self.assertLessEqual(len(d["tail"][0][2]), 41)


if __name__ == "__main__":
    unittest.main()
