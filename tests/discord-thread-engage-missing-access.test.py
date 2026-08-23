#!/usr/bin/env python3
"""Regression guard for the thread-engage seed block in src/discord-bridge.py
silently dropping a thread's first-message seeding when access.json has
genuinely never been created (fresh-install pairing window: no durable
backup yet, no prior write).

Before this fix, `json.loads(ACCESS_FILE.read_text())` and the entire
seed-write logic shared ONE try/except. A missing file raised
FileNotFoundError, which the shared except caught and logged as
"[thread-engage] failed to update access.json: ..." — but that also skipped
the seed logic entirely, so the thread was never added to access.json's
`groups`. Flagged in pending-questions.md (2026-08-10, "[thread-engage]
crashes on missing access.json").

The fix splits the read into its own try/except that degrades to a fresh
`{}` doc on failure (mirroring the graceful-degradation pattern already used
by load_allowed()/load_policy()/load_tier_map() elsewhere in this file),
so the seed-write logic that follows still runs and creates access.json for
the first time instead of silently doing nothing.

Why structural + a narrow executable slice, not a full behavioral test of
on_message: on_message is a single giant handler wired to live discord.py
objects (discord.Thread, client.user, message.channel.*) — mocking it fully
outweighs the fix (same tradeoff proactive-suppression-marker-honored.test.py
already documents for poll_proactive). Instead this test extracts the EXACT
shipped read+fallback lines and executes them standalone against a genuinely
missing file — real production code, real assertion, no discord.py needed —
plus structural checks that the fallback precedes and does not swallow the
seed-write logic.

Run: python3 tests/discord-thread-engage-missing-access.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

# Satisfies scripts/lint-hermetic-bridge-tests.py's contract: it flags any
# exec() sourced from a bridge file, even a narrow extracted fragment like ours.
os.environ["CLAUDE_CONFIG_DIR"] = tempfile.mkdtemp(prefix="ccd-thread-engage-")
_cfg = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "channels" / "discord"
_cfg.mkdir(parents=True, exist_ok=True)
(_cfg / "access.json").write_text('{"allowFrom": []}')

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"

_BLOCK_START = "        if isinstance(message.channel, discord.Thread):"
_BLOCK_END_MARKER = "[thread-engage] failed to update access.json"
_READ_START = (
    "            try:\n"
    "                access_data = json.loads(ACCESS_FILE.read_text())"
)
_SEED_TRY_MARKER = "\n            try:\n                access_groups"


def _extract_thread_engage_block(source: str) -> str:
    start = source.find(_BLOCK_START)
    if start == -1:
        return ""
    end = source.find(_BLOCK_END_MARKER, start)
    if end == -1:
        return ""
    end = source.find("\n", end) + 1
    return source[start:end]


def _extract_read_fallback(block: str) -> str:
    """The ACCESS_FILE-read + fallback sub-block only — two try/except
    statements, no discord.py references, directly executable."""
    start = block.find(_READ_START)
    if start == -1:
        return ""
    end = block.find(_SEED_TRY_MARKER, start)
    if end == -1:
        return ""
    return block[start:end]


class TestThreadEngageMissingAccessFile(unittest.TestCase):
    def setUp(self):
        self.src = BRIDGE.read_text()
        self.block = _extract_thread_engage_block(self.src)
        self.assertTrue(
            self.block, "couldn't locate the thread-engage block in discord-bridge.py"
        )
        self.fallback = _extract_read_fallback(self.block)

    def test_read_has_its_own_except_ahead_of_seed_logic(self):
        """The ACCESS_FILE read is wrapped in ITS OWN try/except (not merged
        with the seed-logic try) so a missing/corrupt file degrades instead
        of skipping seeding entirely."""
        self.assertTrue(
            self.fallback,
            "the read (json.loads(ACCESS_FILE.read_text())) is not isolated "
            "in its own try/except ahead of the seed-logic try — a read "
            "failure will skip seeding entirely instead of degrading to {}",
        )
        self.assertIn(
            "access_data = {}",
            self.fallback,
            "read failure must default access_data to a fresh {} doc",
        )

    def test_read_fallback_executes_and_degrades_to_empty_doc(self):
        """Execute the EXACT extracted read+fallback lines from the shipped
        file against a genuinely-missing ACCESS_FILE and confirm access_data
        comes out as {} (seedable) rather than being left unset."""
        self.assertTrue(self.fallback, "fallback slice missing — see prior test")
        tmp = Path(tempfile.mkdtemp(prefix="dc-thread-engage-")) / "access.json"
        self.assertFalse(tmp.exists(), "precondition: file genuinely missing")
        ns = {"json": json, "ACCESS_FILE": tmp}
        exec(compile(textwrap.dedent(self.fallback), str(BRIDGE), "exec"), ns)
        self.assertEqual(
            ns.get("access_data"),
            {},
            "a missing access.json must degrade access_data to {}, not raise "
            "or leave the name unset",
        )

    def test_seed_logic_runs_after_the_fallback_not_inside_it(self):
        """access_groups = access_data.setdefault(...) must be reachable
        AFTER the read try/except, not nested inside the branch that only
        logs and returns without seeding."""
        idx_fallback = self.block.find("except Exception:\n                # First-run")
        idx_seed = self.block.find("access_groups = access_data.setdefault")
        self.assertNotEqual(idx_fallback, -1, "expected fallback except not found")
        self.assertNotEqual(idx_seed, -1, "expected seed-logic line not found")
        self.assertLess(
            idx_fallback,
            idx_seed,
            "seed logic must run AFTER the read fallback, not be skipped by it",
        )


if __name__ == "__main__":
    _r = unittest.main(exit=False)
    try:
        import coverage

        _cov = coverage.Coverage.current()
        if _cov is not None:
            _cov.save()
    except Exception:
        pass
    sys.exit(0 if _r.result.wasSuccessful() else 1)
