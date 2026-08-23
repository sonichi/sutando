#!/usr/bin/env python3
"""Regression guard for the thread-engage seed block in src/discord-bridge.py
silently dropping a thread's first-message seeding when access.json is
genuinely missing — and, separately, guarding against a PRESENT-BUT-CORRUPT
access.json being treated the same way and clobbered by the seed write.

History: the original fix (this file's v1) shared ONE try/except across the
read and the seed-write, so a missing file raised FileNotFoundError, the
shared except caught it and logged "[thread-engage] failed to update
access.json: ...", and the seed logic never ran — the thread was never added
to access.json's `groups`. Flagged in pending-questions.md (2026-08-10,
"[thread-engage] crashes on missing access.json").

v1's naive fix split the read into its own try/except but caught ALL
exceptions (`except Exception`) and defaulted to `{}` — which conflated
"genuinely absent" with "present but corrupt/unreadable". A transient read
error or a corrupt-but-present file then fell through to the seed-write path,
which replaces the live access.json with just `{"groups": {...}}`, silently
erasing allowFrom/tierMap/dmPolicy/sibling-bot policy/every existing group.
Caught in PR #3318 review (qingyun-wu, 2026-08-23): executing the exact
shipped block against a present corrupt file de-authorized the owner.

The current fix mirrors the absent-vs-corrupt contract read_access_for_seed()
(:909-928) already uses: FileNotFoundError → seedable `{}` default;
any other read failure → None, and the seed-write path is skipped entirely
(guarded by `if access_data is not None:`) so a present corrupt file is left
byte-for-byte untouched.

Why structural + a narrow executable slice, not a full behavioral test of
on_message: on_message is a single giant handler wired to live discord.py
objects (discord.Thread, client.user, message.channel.*) — mocking it fully
outweighs the fix (same tradeoff proactive-suppression-marker-honored.test.py
already documents for poll_proactive). Instead this test extracts the EXACT
shipped read+fallback lines and executes them standalone against both a
genuinely missing file and a genuinely corrupt one — real production code,
real assertions, no discord.py needed — plus structural checks that the
fallback precedes and gates the seed-write logic.

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
_SEED_GUARD_MARKER = "\n            if access_data is not None:"


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
    """The ACCESS_FILE-read + absent-vs-corrupt fallback sub-block only —
    two `except` clauses, no discord.py references, directly executable."""
    start = block.find(_READ_START)
    if start == -1:
        return ""
    end = block.find(_SEED_GUARD_MARKER, start)
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

    def test_read_has_its_own_except_clauses_ahead_of_seed_logic(self):
        """The ACCESS_FILE read is wrapped in its OWN try/except (not merged
        with the seed-logic try), and distinguishes FileNotFoundError
        (genuinely absent) from any other read failure (present-but-corrupt)
        — mirroring read_access_for_seed()'s absent-vs-corrupt contract."""
        self.assertTrue(
            self.fallback,
            "the read (json.loads(ACCESS_FILE.read_text())) is not isolated "
            "in its own try/except ahead of the seed-guard — a read failure "
            "won't be distinguished from a missing file",
        )
        self.assertIn(
            "except FileNotFoundError",
            self.fallback,
            "a genuinely missing file must be caught specifically, not lumped "
            "in with every other read failure",
        )
        self.assertIn(
            "access_data = {}",
            self.fallback,
            "a genuinely missing file must default access_data to a fresh {} doc",
        )
        self.assertIn(
            "access_data = None",
            self.fallback,
            "a present-but-unreadable file must NOT default to {} (that would "
            "let the seed-write below erase it) — it must set access_data to "
            "None so the seed-write guard skips it",
        )

    def test_read_fallback_degrades_missing_file_to_empty_doc(self):
        """Execute the EXACT extracted read+fallback lines from the shipped
        file against a genuinely-missing ACCESS_FILE and confirm access_data
        comes out as {} (seedable) rather than being left unset."""
        self.assertTrue(self.fallback, "fallback slice missing — see prior test")
        tmp = Path(tempfile.mkdtemp(prefix="dc-thread-engage-missing-")) / "access.json"
        self.assertFalse(tmp.exists(), "precondition: file genuinely missing")
        ns = {"json": json, "ACCESS_FILE": tmp}
        exec(compile(textwrap.dedent(self.fallback), str(BRIDGE), "exec"), ns)
        self.assertEqual(
            ns.get("access_data"),
            {},
            "a missing access.json must degrade access_data to {}, not raise "
            "or leave the name unset",
        )

    def test_read_fallback_leaves_corrupt_file_untouched(self):
        """Write-path regression (qingyun-wu, PR #3318 review): execute the
        EXACT extracted read+fallback lines against a PRESENT but corrupt
        access.json and confirm (a) access_data comes out None, so the
        seed-write guard below skips the write entirely, and (b) the file's
        bytes on disk are byte-for-byte unchanged — a transient read error
        must never erase allowFrom/tierMap/dmPolicy/existing groups."""
        self.assertTrue(self.fallback, "fallback slice missing — see prior test")
        tmp = Path(tempfile.mkdtemp(prefix="dc-thread-engage-corrupt-")) / "access.json"
        corrupt_bytes = '{"allowFrom":["owner"'  # truncated/invalid JSON
        tmp.write_text(corrupt_bytes)
        ns = {"json": json, "ACCESS_FILE": tmp}
        exec(compile(textwrap.dedent(self.fallback), str(BRIDGE), "exec"), ns)
        self.assertIsNone(
            ns.get("access_data"),
            "a present-but-corrupt access.json must set access_data to None "
            "(not {}) so the seed-write guard skips the write",
        )
        self.assertEqual(
            tmp.read_text(),
            corrupt_bytes,
            "the read fallback itself must never write to ACCESS_FILE — bytes "
            "on disk must be untouched after a corrupt read",
        )

    def test_seed_logic_gated_on_access_data_not_none(self):
        """access_groups = access_data.setdefault(...) must be reachable only
        inside `if access_data is not None:` — i.e. AFTER and GATED BY the
        read fallback, so a corrupt-file None never reaches the seed-write."""
        idx_guard = self.block.find(_SEED_GUARD_MARKER.strip())
        idx_seed = self.block.find("access_groups = access_data.setdefault")
        self.assertNotEqual(idx_guard, -1, "expected seed guard `if access_data is not None:` not found")
        self.assertNotEqual(idx_seed, -1, "expected seed-logic line not found")
        self.assertLess(
            idx_guard,
            idx_seed,
            "seed logic must be gated by `if access_data is not None:`, not "
            "reachable unconditionally after the read fallback",
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
