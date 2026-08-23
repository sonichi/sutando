#!/usr/bin/env python3
"""Durable on-disk backup for the Discord access allowlist (#899 defense-in-depth).

Parity with slack-bridge.py's ACCESS_BACKUP_FILE mechanism. Before this fix the
Discord bridge had NO durable access-control backup: the only backups were
ad-hoc `channels/discord/access.json.bak-<ts>` files in the VOLATILE
`channels/discord/` dir, and on a wipe the bridge merely printed "restore from
access.json.bak-*" for the operator to restore BY HAND. A wipe + process restart
(access.json deleted/corrupted while the bridge was down) booted into
pairing/TOFU with the owner de-authorized (observed 2026-07-21).

A durable backup under state/auth/ (the cleanup-exempt per-host install-state
dir) closes that: every VALID existing access document is mirrored on startup,
every VALID access write mirrors to disk, and a missing/invalid live
access.json is auto-restored from the backup.

Guards:
  (a) a valid access doc is backed up to state/auth/discord-access-backup.json
  (b) a partial/invalid doc (no allowFrom list) is NOT backed up — can't clobber
      a good backup; an intentional empty lockdown (allowFrom: []) IS backed up
  (c) startup with a missing/invalid live access.json → _restore_access_from_disk
      restores it from the durable backup
  (d) self-gating: a VALID live access.json is left untouched (no stale-backup clobber)
  (e) before/after contrast: without the durable backup a wipe+restart is
      unrecoverable; with it the owner is restored
  + error branches (best-effort backup swallows OSError; restore returns False on
    absent/invalid backup or on a failed access.json write)

The three helpers are extracted from src/discord-bridge.py via AST (matching the
other discord bridge tests' convention — avoids the heavy `import discord`) and
compiled with the production filename/line numbers so coverage measures the
shipped helpers, not a synthetic copy.

Run: python3 tests/discord-access-backup.test.py  (exit 0/1)
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BRIDGE = REPO / "src" / "discord-bridge.py"
sys.path.insert(0, str(REPO / "src"))

import access_store  # noqa: E402

# Path resolution moved to access_store.py (#3318); only the
# backup/restore/validate helpers stay bridge-local.
_HELPERS = (
    "_is_valid_access_doc",
    "_write_owner_only",
    "_backup_access_to_disk",
    "_restore_access_from_disk",
)


def _load_helpers(access_file: Path, backup_file: Path):
    """Extract + exec the three backup helpers into a shared namespace so they
    can call one another (backup/restore both consult _is_valid_access_doc).
    ACCESS_FILE / ACCESS_BACKUP_FILE are injected as the given temp paths."""
    src = BRIDGE.read_text()
    tree = ast.parse(src, filename=str(BRIDGE))
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in _HELPERS
    ]
    if len(nodes) != len(_HELPERS):
        found = {n.name for n in nodes}
        raise AssertionError(f"missing helper(s): {set(_HELPERS) - found}")
    canonical = access_file
    legacy = access_file.parent / "legacy-access.json"
    ns = {
        "json": json,
        "os": os,
        "uuid": uuid,
        "Path": Path,
        "ACCESS_FILE": access_file,
        "ACCESS_BACKUP_FILE": backup_file,
        "claude_home_path": lambda *_parts: canonical,
        "channel_access_path": lambda _source: legacy,
    }
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(BRIDGE), "exec"), ns)
    return ns


class _Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="dc-dbak-"))
        self.access = self.d / "channels" / "discord" / "access.json"
        self.access.parent.mkdir(parents=True, exist_ok=True)
        self.backup = self.d / "state" / "auth" / "discord-access-backup.json"
        self.ns = _load_helpers(self.access, self.backup)

    def is_valid(self, doc):
        return self.ns["_is_valid_access_doc"](doc)

    def backup_to_disk(self, doc):
        return self.ns["_backup_access_to_disk"](doc)

    def restore_from_disk(self):
        return self.ns["_restore_access_from_disk"]()


class TestBackup(_Base):
    def test_valid_doc_backed_up(self):
        """(a) a valid access doc is written to state/auth/discord-access-backup.json."""
        good = {"dmPolicy": "allowlist", "allowFrom": ["OWNER"], "tierMap": {"OWNER": "owner"}}
        self.assertFalse(self.backup.exists(), "precondition: no durable backup yet")
        self.backup_to_disk(good)
        self.assertTrue(self.backup.exists(), "valid doc must be backed up to disk")
        self.assertEqual(json.loads(self.backup.read_text()), good)
        # 0600 — the backup holds owner IDs; must not be world-readable.
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o600)

    def test_empty_lockdown_backed_up(self):
        """An intentional locked-down allowFrom: [] IS valid and IS backed up."""
        self.backup_to_disk({"allowFrom": []})
        self.assertTrue(self.backup.exists())
        self.assertEqual(json.loads(self.backup.read_text()).get("allowFrom"), [])

    def test_partial_wipe_not_backed_up(self):
        """(b) a partial/invalid doc (no allowFrom list) must NOT overwrite a good backup."""
        good = {"allowFrom": ["OWNER"], "tierMap": {"OWNER": "owner"}}
        self.backup_to_disk(good)
        # transient/partial states that must be rejected by the gate:
        self.backup_to_disk({"pending": {}})            # no allowFrom key
        self.backup_to_disk({"allowFrom": "OWNER"})     # allowFrom not a list
        self.backup_to_disk("not-a-dict")               # non-dict
        self.backup_to_disk(None)                        # None
        self.assertEqual(json.loads(self.backup.read_text()), good,
                         "a partial wipe clobbered the good backup")

    def test_gate_predicate(self):
        self.assertTrue(self.is_valid({"allowFrom": []}))
        self.assertTrue(self.is_valid({"allowFrom": ["U"], "tierMap": {}}))
        self.assertFalse(self.is_valid({"pending": {}}))
        self.assertFalse(self.is_valid({"allowFrom": "x"}))
        self.assertFalse(self.is_valid("nope"))
        self.assertFalse(self.is_valid(None))

    def test_backup_swallows_oserror(self):
        """Best-effort: an OSError on the backup write must never raise."""
        import unittest.mock as mock
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            self.backup_to_disk({"allowFrom": ["U"]})  # must not raise
        self.assertTrue(True)

    def test_backup_write_failure_preserves_previous(self):
        """Clobber guard: a failed backup write leaves the previous good backup
        intact (atomic replace — no in-place truncation) and no stray temp."""
        import unittest.mock as mock
        good = {"allowFrom": ["OWNER"], "tierMap": {"OWNER": "owner"}}
        self.backup_to_disk(good)
        with mock.patch.object(os, "replace", side_effect=OSError("crashed mid-write")):
            self.backup_to_disk({"allowFrom": ["NEW"]})  # must not raise
        self.assertEqual(json.loads(self.backup.read_text()), good,
                         "failed write clobbered the previous good backup")
        stray = [p for p in self.backup.parent.iterdir() if p.name != self.backup.name]
        self.assertEqual(stray, [], "failed write left a temp file behind")

    def test_permissive_umask_backup_modes(self):
        """Permissive-umask regression (review 2026-07-28): the state/auth/ leaf
        must be created 0700 and the backup file 0600 even under umask 000."""
        old = os.umask(0o000)
        try:
            self.backup_to_disk({"allowFrom": ["OWNER"], "tierMap": {"OWNER": "owner"}})
        finally:
            os.umask(old)
        self.assertEqual(self.backup.parent.stat().st_mode & 0o777, 0o700,
                         "state/auth/ leaf must be owner-only")
        self.assertEqual(self.backup.stat().st_mode & 0o777, 0o600,
                         "backup file must be owner-only")

    def test_permissive_umask_normalizes_existing_broad_dir(self):
        """A pre-existing overly-broad state/auth/ (the reviewed 0777 repro) is
        narrowed to 0700 on the next backup write."""
        self.backup.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.backup.parent, 0o777)
        self.backup_to_disk({"allowFrom": ["OWNER"]})
        self.assertEqual(self.backup.parent.stat().st_mode & 0o777, 0o700)

    def test_backup_temp_born_0600(self):
        """The backup temp is CREATED with mode 0600 (O_EXCL) — not chmod'd
        after the contents are already on disk."""
        import unittest.mock as mock
        seen = []
        real_open = os.open

        def spy(path, flags, mode=0o777, *a, **kw):
            if str(path).endswith(".tmp"):
                seen.append((flags, mode))
            return real_open(path, flags, mode, *a, **kw)

        with mock.patch.object(os, "open", side_effect=spy):
            self.backup_to_disk({"allowFrom": ["OWNER"]})
        self.assertEqual(len(seen), 1, "expected exactly one temp-file open")
        flags, mode = seen[0]
        self.assertEqual(mode, 0o600, "temp must be born 0600")
        self.assertTrue(flags & os.O_EXCL, "temp must be O_EXCL (no reuse of a broader file)")


class TestAccessPathResolution(unittest.TestCase):
    """access_store.resolve_discord_access_file (#3318 blocker 1: extracted out
    of discord-bridge.py so the skill-callable CLI resolves the identical
    file). Drives the REAL production function — not an AST-extracted copy —
    with CLAUDE_CONFIG_DIR + resolve_workspace pointed at an isolated tmpdir."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="dc-dbak-resolve-"))
        self._old_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.d / "ccd")
        self._old_resolve_workspace = access_store.resolve_workspace
        access_store.resolve_workspace = lambda *a, **kw: self.d / "workspace"
        self.canonical = self.d / "ccd" / "channels" / "discord" / "access.json"
        self.backup = self.d / "workspace" / "state" / "auth" / "discord-access-backup.json"

    def tearDown(self):
        if self._old_config_dir is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._old_config_dir
        access_store.resolve_workspace = self._old_resolve_workspace

    def test_legacy_fallback_preserved_before_first_durable_backup(self):
        """Fresh migration-window installs still resolve via channel_access_path
        (which owns the canonical/legacy choice itself) until a durable backup
        has been seeded — resolve_discord_access_file must defer to it, not
        bypass it."""
        self.assertFalse(self.backup.exists())
        sentinel = self.d / "legacy-sentinel-access.json"
        old_cap = access_store.channel_access_path
        access_store.channel_access_path = lambda source: sentinel
        try:
            self.assertEqual(access_store.resolve_discord_access_file(), sentinel)
        finally:
            access_store.channel_access_path = old_cap

    def test_durable_backup_pins_missing_live_file_to_canonical_path(self):
        """Once state/auth has a valid backup, a missing canonical live file is
        a wipe to restore — not permission to resurrect stale legacy state.
        Proven by showing the canonical path wins even when channel_access_path
        (the legacy-fallback authority) would have returned something else."""
        self.backup.parent.mkdir(parents=True, exist_ok=True)
        self.backup.write_text(json.dumps({"allowFrom": ["OWNER"], "tierMap": {"OWNER": "owner"}}))
        sentinel = self.d / "legacy-sentinel-access.json"
        old_cap = access_store.channel_access_path
        access_store.channel_access_path = lambda source: sentinel
        try:
            result = access_store.resolve_discord_access_file()
        finally:
            access_store.channel_access_path = old_cap
        self.assertEqual(result, self.canonical)
        self.assertNotEqual(result, sentinel)


class TestRestore(_Base):
    def test_restore_when_live_missing(self):
        """(c) startup with a MISSING live access.json → restored from durable backup."""
        good = {"dmPolicy": "allowlist", "allowFrom": ["OWNER"], "tierMap": {"OWNER": "owner"}}
        self.backup_to_disk(good)
        self.assertFalse(self.access.exists(), "precondition: live file wiped")
        self.assertTrue(self.restore_from_disk(), "restore should report success")
        self.assertTrue(self.access.exists(), "live access.json must be recreated")
        self.assertEqual(json.loads(self.access.read_text()), good)
        self.assertEqual(self.access.stat().st_mode & 0o777, 0o600)

    def test_restore_when_live_corrupt(self):
        """A present-but-corrupt live file is treated as invalid → restored."""
        self.backup_to_disk({"allowFrom": ["OWNER"]})
        self.access.write_text('{"allowFrom": ["OWNER"')  # truncated JSON
        self.assertTrue(self.restore_from_disk())
        self.assertEqual(json.loads(self.access.read_text()).get("allowFrom"), ["OWNER"])

    def test_restore_noop_when_live_valid(self):
        """(d) self-gating: a VALID live file is left untouched (no stale-backup clobber)."""
        live = {"allowFrom": ["LIVE_OWNER"], "tierMap": {"LIVE_OWNER": "owner"}}
        self.access.write_text(json.dumps(live))
        # a DIFFERENT (stale) backup on disk must NOT overwrite the good live file
        self.backup.parent.mkdir(parents=True, exist_ok=True)
        self.backup.write_text(json.dumps({"allowFrom": ["STALE"]}))
        self.assertFalse(self.restore_from_disk(), "must be a no-op on a valid live file")
        self.assertEqual(json.loads(self.access.read_text()), live, "live file was clobbered")

    def test_restore_false_when_backup_absent(self):
        self.assertFalse(self.access.exists())
        self.assertFalse(self.backup.exists())
        self.assertFalse(self.restore_from_disk())
        self.assertFalse(self.access.exists(), "must not create a file from nothing")

    def test_restore_false_when_backup_invalid(self):
        self.backup.parent.mkdir(parents=True, exist_ok=True)
        self.backup.write_text('{"pending": {}}')  # schema-invalid backup
        self.assertFalse(self.restore_from_disk())

    def test_restore_false_when_write_fails(self):
        """Valid backup but the access.json write fails → False (exception branch)."""
        import unittest.mock as mock
        self.backup_to_disk({"allowFrom": ["OWNER"]})
        with mock.patch.object(os, "replace", side_effect=OSError("readonly")):
            self.assertFalse(self.restore_from_disk())

    def test_permissive_umask_restore_modes(self):
        """Permissive-umask regression (review 2026-07-28): the restore temp is
        born 0600 (O_EXCL) and the restored access.json ends up 0600."""
        import unittest.mock as mock
        self.backup_to_disk({"allowFrom": ["OWNER"], "tierMap": {"OWNER": "owner"}})
        self.access.write_text("{corrupt")
        seen = []
        real_open = os.open

        def spy(path, flags, mode=0o777, *a, **kw):
            if str(path).endswith(".tmp"):
                seen.append((flags, mode))
            return real_open(path, flags, mode, *a, **kw)

        old = os.umask(0o000)
        try:
            with mock.patch.object(os, "open", side_effect=spy):
                self.assertTrue(self.restore_from_disk())
        finally:
            os.umask(old)
        self.assertEqual(self.access.stat().st_mode & 0o777, 0o600,
                         "restored access.json must be owner-only")
        self.assertEqual(len(seen), 1, "expected exactly one restore temp open")
        flags, mode = seen[0]
        self.assertEqual(mode, 0o600, "restore temp must be born 0600")
        self.assertTrue(flags & os.O_EXCL, "restore temp must be O_EXCL")


class TestBeforeAfterContrast(_Base):
    """Explicit before/after: the exposure the durable backup closes."""

    def test_wipe_restart_recoverability(self):
        owner = {"dmPolicy": "allowlist", "allowFrom": ["REAL_OWNER"], "tierMap": {"REAL_OWNER": "owner"}}
        self.access.write_text(json.dumps(owner))

        # BEFORE (no durable backup written): a wipe+restart has nothing to
        # restore from — _restore_access_from_disk is a no-op, live stays gone.
        self.access.unlink()
        self.assertFalse(self.backup.exists(), "BEFORE world: no durable backup")
        self.assertFalse(self.restore_from_disk(),
                         "BEFORE: with no backup, restart cannot recover the allowlist")
        self.assertFalse(self.access.exists(),
                         "BEFORE: access.json stays wiped → bridge boots into open pairing/TOFU")

        # AFTER (this fix): the valid write mirrored a durable backup, so the
        # same wipe+restart restores the real owner — no open-pairing exposure.
        self.backup_to_disk(owner)          # what every valid write now does
        self.assertTrue(self.restore_from_disk(),
                        "AFTER: durable backup lets restart recover the allowlist")
        restored = json.loads(self.access.read_text())
        self.assertEqual(restored.get("allowFrom"), ["REAL_OWNER"],
                         "AFTER: the real owner is restored, not a fresh TOFU enrollee")


class TestWiredIntoBridge(unittest.TestCase):
    """Structural guards: the helpers are defined and invoked at the live sites,
    so a future refactor can't silently drop the backup/restore wiring."""

    def setUp(self):
        self.src = BRIDGE.read_text()

    def test_backup_file_under_state_auth(self):
        # Path resolution moved to access_store.py (#3318 blocker 1) so the
        # skill-callable CLI shares it; the bridge now binds by reference.
        self.assertIn("ACCESS_BACKUP_FILE = discord_access_backup_file()", self.src)
        self.assertEqual(
            access_store.discord_access_backup_file().parts[-3:],
            ("state", "auth", "discord-access-backup.json"),
        )

    def test_helpers_defined(self):
        for name in _HELPERS:
            self.assertIn(f"def {name}(", self.src, f"missing helper def {name}")

    def test_restore_called_in_on_ready(self):
        ready = self.src.find("async def on_ready():")
        self.assertNotEqual(ready, -1)
        call = self.src.find("_restore_access_from_disk()", ready)
        seed = self.src.find("json.loads(ACCESS_FILE.read_text())", ready)
        backup = self.src.find("_backup_access_to_disk(_initial_access)", seed)
        self.assertNotEqual(call, -1, "on_ready must call _restore_access_from_disk()")
        self.assertLess(call, seed, "restore must run BEFORE the first access read in on_ready")
        self.assertNotEqual(
            backup, -1,
            "on_ready must seed the durable backup from an existing valid access.json",
        )

    def test_backup_called_at_write_sites(self):
        # Every atomic write-back mirrors a durable backup, passed BY REFERENCE
        # (backup=_backup_access_to_disk) to the shared mutator, not called direct.
        self.assertIn("def _backup_access_to_disk(", self.src, "missing helper def")
        self.assertGreaterEqual(
            self.src.count("backup=_backup_access_to_disk"), 3,
            "expected _backup_access_to_disk wired by reference at the "
            "tier-map seed, thread-engage, and pairing write sites")


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
    os._exit(0 if _r.result.wasSuccessful() else 1)
