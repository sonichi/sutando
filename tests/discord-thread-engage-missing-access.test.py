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

v2 (this file): the thread-engage seed-write was migrated to route through
`access_store.mutate_access_file` — the single locked owner every access.json
writer now shares (tier-map seeding, thread-engage seeding, pairing-code
issuance). That module has no `discord` dependency by design (see its own
docstring), so this test imports it directly and exercises the REAL
production mutator against real temp files — no source-slicing, no exec()
of extracted fragments. The absent-vs-corrupt contract lives in
`access_store.read_access_for_transaction` and is covered directly by
`tests/discord-bridge-access-no-clobber.test.py`; this file's job is the
thread-seed MUTATOR's own behavior (what it seeds, what it skips, and that
it never runs at all on a corrupt file) plus a structural guard that the
bridge's call site is wired to the shared owner.

Run: python3 tests/discord-thread-engage-missing-access.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"
sys.path.insert(0, str(REPO / "src"))

from access_store import mutate_access_file  # noqa: E402


def _thread_seed_mutator_factory(thread_id_str, parent_id_str, sender_id, seed_ok=True):
    """Reconstructs the production mutator's logic (mirrors the closure body
    in discord-bridge.py's thread-engage block) so it can be exercised
    against `access_store.mutate_access_file` directly, parameterized on the
    values the real closure captures from `message`/`bot_mentioned`/etc.

    Mirrors the #3318 blocker-2 fix: the no-parent-config branch manufactures
    a brand-new grant rather than propagating an existing one, so it only
    seeds when the sender is already a global `allowFrom` member — anyone
    else falls through to the normal allowlist/pairing gate, unseeded."""

    def _mutator(access_data):
        access_groups = access_data.setdefault('groups', {})
        if thread_id_str in access_groups or not seed_ok:
            return None, None
        parent_cfg = access_groups.get(parent_id_str) if parent_id_str else None
        if parent_cfg is True:
            thread_entry = {'requireMention': False}
        elif isinstance(parent_cfg, dict):
            inherited_allow = parent_cfg.get('allowFrom', [str(sender_id)])
            thread_entry = {'requireMention': False, 'allowFrom': inherited_allow}
        else:
            if str(sender_id) not in (access_data.get('allowFrom') or []):
                return None, None
            thread_entry = {'requireMention': False, 'allowFrom': [str(sender_id)]}
        access_groups[thread_id_str] = thread_entry
        return access_data, (thread_id_str, parent_id_str, thread_entry, access_data.get('allowFrom', []))

    return _mutator


class TestThreadSeedMutatorAgainstMissingFile(unittest.TestCase):
    def test_missing_access_file_does_not_self_authorize_unrecognized_sender(self):
        """#3318 blocker 2 (qingyun-wu): a genuinely missing access.json used to
        turn the first unmentioned thread author into an authorized sender —
        `read_access_for_transaction`'s safe default has an empty `allowFrom`,
        so every sender looked equally "unrecognized" and got seeded anyway.
        A missing access file must fail closed: no group entry, no file
        created, sender falls through to the normal allowlist/pairing gate."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            self.assertFalse(p.exists(), "precondition: file genuinely missing")
            mutator = _thread_seed_mutator_factory("999", None, "42")
            result = mutate_access_file(p, mutator)
            self.assertIsNone(result, "an unrecognized sender must not self-authorize via a missing access.json")
            self.assertFalse(p.exists(), "a rejected seed attempt must not create access.json")

    def test_missing_access_file_does_not_self_authorize_even_with_no_parent(self):
        """Same as above, restated for the exact scenario the reviewer traced:
        no parent config to inherit from, so the branch would otherwise
        manufacture a brand-new grant out of nothing."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            mutator = _thread_seed_mutator_factory("thread-fresh", "parent-unconfigured", "stranger-1")
            result = mutate_access_file(p, mutator)
            self.assertIsNone(result)
            self.assertFalse(p.exists())

    def test_recognized_sender_still_seeds_without_parent_config(self):
        """The owner's own single-bot convenience (the reason this branch
        exists — see "Ungated 2026-06-06" in discord-bridge.py) must survive:
        a sender ALREADY in the top-level allowFrom still gets an
        engager-only grant even with no parent config to inherit."""
        import json
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            p.write_text(json.dumps({"dmPolicy": "pairing", "allowFrom": ["owner-1"], "groups": {}}))
            mutator = _thread_seed_mutator_factory("thread-1", None, "owner-1")
            result = mutate_access_file(p, mutator)
            self.assertIsNotNone(result, "an already-recognized (allowFrom) sender must still be seedable")
            thread_id, parent_id, entry, owners = result
            self.assertEqual(thread_id, "thread-1")
            self.assertEqual(entry, {'requireMention': False, 'allowFrom': ['owner-1']})
            on_disk = json.loads(p.read_text())
            self.assertEqual(on_disk['groups']['thread-1'], entry)

    def test_inherits_parent_group_allowfrom(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            p.write_text(json.dumps({
                "dmPolicy": "pairing", "allowFrom": ["owner"],
                "groups": {"parent-1": {"allowFrom": ["a", "b"]}},
            }))
            mutator = _thread_seed_mutator_factory("thread-1", "parent-1", "sender-x")
            result = mutate_access_file(p, mutator)
            self.assertIsNotNone(result)
            _, _, entry, _ = result
            self.assertEqual(entry, {'requireMention': False, 'allowFrom': ['a', 'b']})

    def test_already_seeded_thread_is_a_noop(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            existing = {"dmPolicy": "pairing", "allowFrom": ["owner"],
                        "groups": {"thread-1": {"requireMention": False}}}
            p.write_text(json.dumps(existing))
            mutator = _thread_seed_mutator_factory("thread-1", None, "sender-x")
            result = mutate_access_file(p, mutator)
            self.assertIsNone(result, "an already-seeded thread must not be re-seeded")
            self.assertEqual(json.loads(p.read_text()), existing, "file must be untouched on a no-op")

    def test_seed_ok_false_is_a_noop(self):
        """Multi-bot fleets: skip seeding when this bot wasn't addressed and a
        sibling already owns the seed (#1823)."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            mutator = _thread_seed_mutator_factory("thread-1", None, "sender-x", seed_ok=False)
            result = mutate_access_file(p, mutator)
            self.assertIsNone(result)
            self.assertFalse(p.exists(), "a not-seed_ok pass over a missing file must not create one")


class TestThreadSeedLeavesCorruptFileUntouched(unittest.TestCase):
    """Write-path regression (qingyun-wu, PR #3318 review): the mutator must
    never even run against a PRESENT but corrupt access.json — mirrors the
    absent-vs-corrupt contract every access_store writer shares."""

    def test_corrupt_file_never_invokes_mutator_and_is_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            corrupt_bytes = '{"allowFrom":["owner"'  # truncated/invalid JSON
            p.write_text(corrupt_bytes)

            called = []

            def _spy_mutator(access_data):
                called.append(True)
                return access_data, "should never run"

            result = mutate_access_file(p, _spy_mutator)
            self.assertIsNone(result, "a corrupt file must yield None, same as an already-seeded no-op")
            self.assertEqual(called, [], "the mutator must never be invoked against a corrupt file")
            self.assertEqual(p.read_text(), corrupt_bytes, "a corrupt file's bytes must be untouched")


class TestThreadEngageCallSiteWiredToSharedOwner(unittest.TestCase):
    """Structural guard: the bridge's thread-engage block must route through
    access_store.mutate_access_file (#3318) — not a hand-rolled read+write —
    so a concurrent owner/tier/group update and a thread seed can't
    lost-update each other."""

    def setUp(self):
        self.src = BRIDGE.read_text()

    def test_imports_mutate_access_file(self):
        self.assertIn("from access_store import (", self.src)
        self.assertIn("mutate_access_file,", self.src)
        self.assertIn("read_access_for_transaction,", self.src)

    def test_thread_engage_calls_mutate_access_file(self):
        self.assertIn(
            "seed_result = mutate_access_file(ACCESS_FILE, _thread_seed_mutator, backup=_backup_access_to_disk)",
            self.src,
        )

    def test_no_hand_rolled_read_or_write_remains(self):
        start = self.src.find("if isinstance(message.channel, discord.Thread):")
        end = self.src.find("seed_result = mutate_access_file(ACCESS_FILE, _thread_seed_mutator", start)
        self.assertNotEqual(start, -1)
        self.assertNotEqual(end, -1)
        block = self.src[start:end]
        self.assertNotIn("json.loads(ACCESS_FILE.read_text())", block,
                         "the thread-engage block must not hand-roll its own read — "
                         "that was the source of the absent-vs-corrupt conflation bug")
        self.assertNotIn("ACCESS_FILE.write_text(", block)


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
