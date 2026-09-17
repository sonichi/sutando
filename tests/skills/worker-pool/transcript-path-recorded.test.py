#!/usr/bin/env python3
"""A session record points at its transcript, or says nothing.

`sessions.json` has carried a `transcript.path` field since it was written, and
no caller has ever supplied one -- so every record advertises a locator it does
not hold. That is worse than omitting the field: a reader that trusts it finds
"" and concludes the transcript is gone, when in fact the file is on disk under
a name derived from the session id. Tonight that cost a lookup: three workers
had to be matched to their transcripts by scanning, because their own records
could not say where they were.

So: when the transcript file exists, the record names it; when it does not, the
field stays empty rather than pointing at a file nobody wrote.

Run: python3 tests/skills/worker-pool/transcript-path-recorded.test.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "skills/worker-pool/scripts"))

import worker_identity as wi  # noqa: E402


class TranscriptPathIsRecorded(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        self.sid = "11111111-2222-3333-4444-555555555555"
        self.cwd = "/Users/someone/src/checkout"
        # Claude Code's own layout: one directory per project, slug-encoded from
        # the cwd, holding <session-id>.jsonl.
        self.proj = (self.ws / ".claude-sutando" / "projects"
                     / "-Users-someone-src-checkout")

    def _transcript(self):
        self.proj.mkdir(parents=True, exist_ok=True)
        f = self.proj / f"{self.sid}.jsonl"
        f.write_text('{"type":"user"}\n')
        return f

    def test_an_existing_transcript_is_named_in_the_record(self):
        f = self._transcript()
        rec = wi.create_worker(self.ws, runtime="claude", cwd=self.cwd,
                               session_id=self.sid)
        row = wi.sessions(self.ws, rec["worker_id"])[0]
        self.assertEqual(row["transcript"]["path"], str(f),
                         "the record still advertises a locator it does not hold")

    def test_a_missing_transcript_leaves_the_field_empty(self):
        """A path to a file nobody wrote is worse than no path: it turns 'not
        started yet' into 'lost'."""
        rec = wi.create_worker(self.ws, runtime="claude", cwd=self.cwd,
                               session_id=self.sid)
        row = wi.sessions(self.ws, rec["worker_id"])[0]
        self.assertEqual(row["transcript"]["path"], "",
                         "a nonexistent transcript was recorded as if it existed")

    def test_an_explicit_path_still_wins(self):
        self._transcript()
        rec = wi.create_worker(self.ws, runtime="claude", cwd=self.cwd,
                               session_id=self.sid,
                               transcript_path="/somewhere/else.jsonl")
        row = wi.sessions(self.ws, rec["worker_id"])[0]
        self.assertEqual(row["transcript"]["path"], "/somewhere/else.jsonl",
                         "the caller's explicit path was overwritten by the guess")

    def test_the_resolver_is_addressable_on_its_own(self):
        f = self._transcript()
        self.assertEqual(wi.transcript_path_for(self.ws, self.cwd, self.sid), str(f))
        self.assertEqual(
            wi.transcript_path_for(self.ws, self.cwd, "no-such-session"), "",
            "the resolver invented a path for a session with no transcript")


if __name__ == "__main__":
    unittest.main(verbosity=2)
