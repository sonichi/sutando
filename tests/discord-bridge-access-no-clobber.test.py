#!/usr/bin/env python3
"""Behavioral test: writing access.json must NOT clobber a corrupt file.

Root cause (2026-07-21): the Discord bridge's pairing branch read access.json
with a bare `try/except` that fell back to an empty-allowFrom default, then
WROTE that default back to disk. So a single transient read glitch on
access.json permanently wiped the real config — the owner was dropped from
`allowFrom`, every sender then got a pairing prompt, and pairing codes leaked
into channels (incl. #dev). The owner was silently de-authorized mid-session.

Fix (PR #3318, qingyun-wu review): every writer of access.json — tier-map
seeding, thread-engage seeding, and pairing-code issuance — now routes
through `access_store.mutate_access_file`, the single locked owner. Its read
side, `read_access_for_transaction(path)`, distinguishes the three cases —
  - present + valid  → the parsed dict,
  - genuinely ABSENT → a fresh default (first-run onboarding is fine to seed),
  - present but CORRUPT → None, signalling the caller to bail and NOT overwrite.

`discord-bridge.py` previously carried its own duplicate of this exact
function (`read_access_for_seed`) — a second copy of the same absent-vs-
corrupt policy. It has been deleted; this file's dead-code guard keeps it
from being revived as a copy-paste landmine beside the real owner.

This test imports `access_store` directly (no `discord` dependency — see its
own module docstring) and exercises the real function against REAL temp
files, plus structural guards that the pairing branch actually routes
through `mutate_access_file` and bails on `None` instead of regressing to
the destructive bare-except write.
"""
from pathlib import Path
import json
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"
sys.path.insert(0, str(REPO / "src"))

from access_store import read_access_for_transaction  # noqa: E402


class TestReadAccessForTransaction(unittest.TestCase):
    def test_present_and_valid_returns_parsed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            good = {"dmPolicy": "allowlist", "allowFrom": ["123"], "pending": {}}
            p.write_text(json.dumps(good))
            self.assertEqual(read_access_for_transaction(p), good)

    def test_absent_returns_default_seed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"  # never created
            out = read_access_for_transaction(p)
            self.assertEqual(out, {"dmPolicy": "pairing", "allowFrom": [], "pending": {}})

    def test_corrupt_present_returns_none(self):
        """The load-bearing case: a present-but-unparseable file → None (do not clobber)."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            p.write_text('{"dmPolicy": "allowlist", "allowFrom": ["123"')  # truncated JSON
            self.assertIsNone(read_access_for_transaction(p))

    def test_empty_file_returns_none(self):
        """A zero-byte access.json (partial-write crash) is corrupt, not absent → None."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            p.write_text("")
            self.assertIsNone(read_access_for_transaction(p))

    def test_corrupt_file_left_untouched(self):
        """The helper never writes; the real config bytes survive the read attempt."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            original = '{"dmPolicy": "allowlist", "allowFrom": ["owner-id"'  # corrupt
            p.write_text(original)
            read_access_for_transaction(p)
            self.assertEqual(p.read_text(), original)  # unchanged — no clobber


class TestReadAccessForSeedRemoved(unittest.TestCase):
    """Dead-code guard: read_access_for_seed() duplicated
    access_store.read_access_for_transaction's exact contract (#3318). Once
    the pairing branch migrated off it, it became unused duplicate policy —
    exactly what CLAUDE.md's "one writer contract" rule forbids leaving
    behind. Must stay deleted."""

    def setUp(self):
        self.src = BRIDGE.read_text()

    def test_function_definition_removed(self):
        self.assertNotIn(
            "def read_access_for_seed", self.src,
            "read_access_for_seed() must stay deleted — it duplicated "
            "access_store.read_access_for_transaction's absent-vs-corrupt "
            "contract; do not revive it",
        )

    def test_no_remaining_call_sites(self):
        self.assertNotIn(
            "read_access_for_seed(", self.src,
            "a call site still references the deleted read_access_for_seed — "
            "this is a dangling NameError waiting to happen at runtime",
        )


class TestPairingBranchUsesSharedOwner(unittest.TestCase):
    """Structural guard: the pairing branch must route through
    access_store.mutate_access_file — the single locked writer every
    access.json mutator shares (#3318) — and bail cleanly on a corrupt
    file instead of regressing to a bare read-then-write that can clobber
    it."""

    def setUp(self):
        self.src = BRIDGE.read_text()

    def test_pairing_imports_mutate_access_file(self):
        self.assertIn("from access_store import (", self.src)
        self.assertIn("mutate_access_file,", self.src)
        self.assertIn("read_access_for_transaction,", self.src)

    def test_pairing_uses_mutate_access_file(self):
        self.assertIn(
            "code = mutate_access_file(ACCESS_FILE, _pairing_mutator, backup=_backup_access_to_disk)",
            self.src,
        )

    def test_pairing_bails_on_none(self):
        call = self.src.find("code = mutate_access_file(ACCESS_FILE, _pairing_mutator")
        self.assertNotEqual(call, -1, "pairing branch no longer calls mutate_access_file")
        none_guard = self.src.find("if code is None:", call)
        deliver = self.src.find("route = await _deliver_pairing_prompt(", call)
        self.assertNotEqual(none_guard, -1, "missing `if code is None:` bail guard")
        self.assertNotEqual(deliver, -1, "missing pairing-prompt delivery call")
        self.assertLess(none_guard, deliver, "None-guard must precede prompt delivery")
        ret = self.src.find("return", none_guard)
        self.assertNotEqual(ret, -1, "the None-guard must return, not fall through")
        self.assertLess(ret, deliver, "the None-guard's return must precede prompt delivery")

    def test_pairing_mutator_never_writes_directly(self):
        """_pairing_mutator only builds the new dict and returns it — the
        actual write (locking, atomicity, backup) belongs solely to
        mutate_access_file. A hand-rolled write here would bypass the lock
        and reintroduce the lost-update race this PR closes."""
        start = self.src.find("def _pairing_mutator(access):")
        end = self.src.find("route = await _deliver_pairing_prompt(", start)
        self.assertNotEqual(start, -1, "could not locate _pairing_mutator")
        branch = self.src[start:end]
        self.assertNotIn("os.replace(", branch)
        self.assertNotIn("ACCESS_FILE.write_text(", branch)
        self.assertNotIn("write_private_text(", branch)

    def test_no_bare_except_default_write(self):
        # The old destructive pattern must be gone from the pairing branch.
        self.assertNotIn(
            'access = {"dmPolicy": "pairing", "allowFrom": [], "pending": {}}\n        code =',
            self.src,
        )

    def test_dead_destructive_allowlist_writer_removed(self):
        """save_to_allowlist() carried the IDENTICAL bare-except → empty-default →
        write pattern this PR removes from the pairing branch: on a corrupt read it
        would persist an allowFrom containing only the just-approved sender, wiping
        every other authorized user (same wipe class). It was dead code (zero callers
        repo-wide; the live approval path is poll_approved + the /discord:access
        skill), i.e. a copy-paste landmine sitting beside the fixed path. Deleting it
        (flagged by qingyun-wu on #2260) keeps the pattern from being revived into a
        live path."""
        self.assertNotIn("def save_to_allowlist", self.src,
                         "dead destructive save_to_allowlist() must stay deleted — do not revive")
        self.assertNotIn(
            '{"dmPolicy": "pairing", "allowFrom": [], "groups": {}, "pending": {}}',
            self.src,
            "the save_to_allowlist bare-except empty default must not reappear",
        )


if __name__ == "__main__":
    _r = unittest.main(exit=False)
    # Flush coverage before the hard exit (os._exit skips coverage's atexit
    # writer → the gate would see zero data). See reference note 2026-07-21.
    try:
        import coverage
        _cov = coverage.Coverage.current()
        if _cov is not None:
            _cov.save()
    except Exception:
        pass
    import os
    os._exit(0 if _r.result.wasSuccessful() else 1)
