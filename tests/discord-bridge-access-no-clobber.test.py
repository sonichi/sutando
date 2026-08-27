#!/usr/bin/env python3
"""Behavioral test: the pairing seed must NOT clobber a corrupt access.json.

Root cause (2026-07-21): the Discord bridge's pairing branch read access.json
with a bare `try/except` that fell back to an empty-allowFrom default, then
WROTE that default back to disk. So a single transient read glitch on
access.json permanently wiped the real config — the owner was dropped from
`allowFrom`, every sender then got a pairing prompt, and pairing codes leaked
into channels (incl. #dev). The owner was silently de-authorized mid-session.

Fix: `read_access_for_seed(path)` distinguishes the three cases —
  - present + valid  → the parsed dict,
  - genuinely ABSENT → a fresh default (first-run onboarding is fine to seed),
  - present but CORRUPT → None, signalling the caller to bail and NOT overwrite.

This test extracts the pure function's source and exercises it against REAL
temp files (no `import discord`, matching the other bridge tests' convention),
plus a structural guard that the pairing branch actually bails on None instead
of regressing to the destructive bare-except write.
"""
from pathlib import Path
import ast
import json
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"


def _load_read_access_for_seed():
    """Extract + exec ONLY read_access_for_seed (avoids the heavy discord.py
    import the full module does). Compile its original AST node with the
    production filename/line numbers so coverage measures the shipped helper,
    not a synthetic copy inside this test."""
    src = BRIDGE.read_text()
    tree = ast.parse(src, filename=str(BRIDGE))
    fn_node = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "read_access_for_seed"
    )
    ns = {"json": json}
    module = ast.Module(body=[fn_node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(BRIDGE), "exec"), ns)
    return ns["read_access_for_seed"]


class TestReadAccessForSeed(unittest.TestCase):
    def setUp(self):
        self.fn = _load_read_access_for_seed()

    def test_present_and_valid_returns_parsed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            good = {"dmPolicy": "allowlist", "allowFrom": ["123"], "pending": {}}
            p.write_text(json.dumps(good))
            self.assertEqual(self.fn(p), good)

    def test_absent_returns_default_seed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"  # never created
            out = self.fn(p)
            self.assertEqual(out, {"dmPolicy": "pairing", "allowFrom": [], "pending": {}})

    def test_corrupt_present_returns_none(self):
        """The load-bearing case: a present-but-unparseable file → None (do not clobber)."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            p.write_text('{"dmPolicy": "allowlist", "allowFrom": ["123"')  # truncated JSON
            self.assertIsNone(self.fn(p))

    def test_empty_file_returns_none(self):
        """A zero-byte access.json (partial-write crash) is corrupt, not absent → None."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            p.write_text("")
            self.assertIsNone(self.fn(p))

    def test_corrupt_file_left_untouched(self):
        """The helper never writes; the real config bytes survive the read attempt."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "access.json"
            original = '{"dmPolicy": "allowlist", "allowFrom": ["owner-id"'  # corrupt
            p.write_text(original)
            self.fn(p)
            self.assertEqual(p.read_text(), original)  # unchanged — no clobber


class TestPairingBranchBailsOnCorruption(unittest.TestCase):
    """Structural guard: the pairing branch must consult read_access_for_seed and
    bail on None — never regress to the bare-except-then-write that wiped config."""

    def setUp(self):
        self.src = BRIDGE.read_text()

    def test_pairing_uses_helper(self):
        self.assertIn("access = read_access_for_seed(ACCESS_FILE)", self.src)

    def test_pairing_bails_on_none(self):
        # The `if access is None:` guard + a return must appear before the write.
        guard = self.src.find("access = read_access_for_seed(ACCESS_FILE)")
        # The pairing write-back is now atomic (os.replace of a tmp file); the
        # first os.replace after the seed-read is the pairing branch's write.
        write = self.src.find("os.replace(tmp_path, ACCESS_FILE)", guard)
        none_guard = self.src.find("if access is None:", guard)
        self.assertNotEqual(none_guard, -1, "missing `if access is None:` bail guard")
        self.assertNotEqual(write, -1, "missing pairing-branch atomic write-back")
        self.assertLess(none_guard, write, "None-guard must precede the write-back")

    def test_pairing_write_is_atomic(self):
        """The pairing branch must write access.json atomically (tmp + os.replace),
        NOT a bare write_text truncate-in-place. That truncate window was the
        TRIGGER of the 2026-07-21 corrupt read: a concurrent reader (every message
        re-reads access.json via load_channel_config) saw a partial file → parse
        fail → (pre-guard) an empty-allowFrom clobber + pairing-code leak."""
        seed = self.src.find("access = read_access_for_seed(ACCESS_FILE)")
        end = self.src.find("route = await _deliver_pairing_prompt(", seed)
        self.assertNotEqual(end, -1, "could not locate the pairing branch end")
        branch = self.src[seed:end]
        self.assertIn("os.replace(tmp_path, ACCESS_FILE)", branch,
                      "pairing branch must write access.json atomically via os.replace")
        self.assertNotIn("ACCESS_FILE.write_text(", branch,
                         "pairing branch must NOT truncate-in-place with ACCESS_FILE.write_text")

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
