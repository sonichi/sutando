#!/usr/bin/env python3
"""Standalone tests for src/discord_config.py — #1147 workspace-local
Sutando discord config (`$WS/state/discord-config.json`).

Exercised paths:
- `resolve_owner_id` for each of the 5 config-driven priority steps
- `resolve_owner_id` returns None when all config paths exhausted
- `auto_seed_if_missing` for the 4 seed-source branches (explicit owner,
  tierMap tag, allowFrom[0] WARN, empty)
- `auto_seed_if_missing` is idempotent — no overwrite if file exists
- Both site callers (bridge + dm-result) share the helper — covered by
  the symmetric test fixtures from Lucy's #1147 watch-point #1.

Run: python3 tests/discord_config.test.py
Exit: 0 on pass, 1 on fail.

Stdlib-only (no pytest) — matches the repo's standalone-test convention
(`tests/*.test.py` invoked one-by-one by .github/workflows/ci.yml).
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import discord_config  # noqa: E402


# ----- test plumbing ------------------------------------------------------


_FAILURES: list[str] = []


def fail(msg: str) -> None:
    """Record a failure but continue running so the user sees ALL fails."""
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)


def expect_eq(actual, expected, label: str) -> None:
    if actual != expected:
        fail(f"{label}: expected {expected!r}, got {actual!r}")


def expect_in(needle: str, haystack: str, label: str) -> None:
    if needle not in haystack:
        fail(f"{label}: {needle!r} not found in {haystack!r}")


@contextmanager
def workspace_env():
    """Yield (workspace_path, captured_log_records) with a clean tmp
    workspace and a captured logger. State is torn down after.

    CRITICAL — we monkey-patch `discord_config.config_path` directly
    instead of setting `SUTANDO_WORKSPACE`. Setting that env var would
    trigger `workspace_default.resolve_workspace()`'s `_migrate_legacy_dirs`
    which MOVES (not copies) `notes/` etc. from cwd into the new workspace
    — and when `tempfile.TemporaryDirectory` cleans up, the moved files
    are destroyed. The 2026-05-25 lesson: 37 already-committed notes files
    and 1 uncommitted file were destroyed by a tmp-workspace env var in
    an earlier draft of this test. See
    `feedback_test_workspace_env_triggers_destructive_migration` in memory.
    """
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / "state").mkdir()
        old_owner = os.environ.get("SUTANDO_DM_OWNER_ID")
        os.environ.pop("SUTANDO_DM_OWNER_ID", None)

        # Override the path resolver — bypasses resolve_workspace entirely.
        original_config_path = discord_config.config_path
        discord_config.config_path = lambda: ws / "state" / discord_config.CONFIG_FILENAME

        # Capture log records emitted by discord_config.
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture(level=logging.DEBUG)
        discord_config.logger.addHandler(handler)
        original_level = discord_config.logger.level
        discord_config.logger.setLevel(logging.DEBUG)

        try:
            yield ws, records
        finally:
            discord_config.config_path = original_config_path
            discord_config.logger.removeHandler(handler)
            discord_config.logger.setLevel(original_level)
            if old_owner is None:
                os.environ.pop("SUTANDO_DM_OWNER_ID", None)
            else:
                os.environ["SUTANDO_DM_OWNER_ID"] = old_owner


def write_config(ws: Path, data: dict) -> None:
    (ws / "state" / discord_config.CONFIG_FILENAME).write_text(json.dumps(data))


# ----- resolve_owner_id chain --------------------------------------------


def test_env_override_wins():
    with workspace_env() as (ws, _):
        os.environ["SUTANDO_DM_OWNER_ID"] = "111"
        write_config(ws, {"owner": "222"})
        access = {"allowFrom": ["333"], "owner": "444"}
        expect_eq(
            discord_config.resolve_owner_id(access),
            "111",
            "env override should win over everything",
        )


def test_workspace_owner_wins():
    with workspace_env() as (ws, _):
        write_config(ws, {"owner": "222"})
        access = {
            "allowFrom": ["999"],
            "owner": "444",
            "tierMap": {"999": "owner"},
        }
        expect_eq(
            discord_config.resolve_owner_id(access),
            "222",
            "workspace owner should beat legacy access.json fields",
        )


def test_workspace_tier_map():
    with workspace_env() as (ws, _):
        write_config(ws, {"tierMap": {"333": "owner", "444": "team"}})
        access = {"allowFrom": ["444", "333", "555"]}
        expect_eq(
            discord_config.resolve_owner_id(access),
            "333",
            "workspace tierMap[uid]=owner should resolve",
        )


def test_legacy_owner_field():
    """Compat path #1118 manual edits (one release of grace)."""
    with workspace_env() as (_, _):
        access = {"allowFrom": ["999"], "owner": "555"}
        expect_eq(
            discord_config.resolve_owner_id(access),
            "555",
            "legacy access.owner should resolve when workspace empty",
        )


def test_legacy_tier_map():
    """Compat path #846 — Sutando tag in access.json."""
    with workspace_env() as (_, _):
        access = {
            "allowFrom": ["111", "222", "333"],
            "tierMap": {"222": "owner", "333": "team"},
        }
        expect_eq(
            discord_config.resolve_owner_id(access),
            "222",
            "legacy access.tierMap[uid]=owner should resolve",
        )


def test_returns_none_when_exhausted():
    with workspace_env() as (_, _):
        access = {"allowFrom": ["111", "222"]}
        expect_eq(
            discord_config.resolve_owner_id(access),
            None,
            "no config path matches → None (caller does bot-filter)",
        )


def test_empty_access_returns_none():
    with workspace_env() as (_, _):
        expect_eq(
            discord_config.resolve_owner_id({}),
            None,
            "empty access dict + empty workspace = None",
        )


def test_stale_tier_tag_for_de_listed_user():
    """A `tierMap` tag for someone NOT in allowFrom must NOT resolve them."""
    with workspace_env() as (ws, _):
        write_config(ws, {"tierMap": {"999": "owner"}})
        access = {"allowFrom": ["111", "222"]}
        expect_eq(
            discord_config.resolve_owner_id(access),
            None,
            "stale tier tag must not resolve a de-listed user",
        )


def test_injected_config_overrides_disk():
    with workspace_env() as (ws, _):
        write_config(ws, {"owner": "from-disk"})
        out = discord_config.resolve_owner_id({}, config={"owner": "from-arg"})
        expect_eq(out, "from-arg", "config= kwarg must bypass disk read")


# ----- auto_seed_if_missing ----------------------------------------------


def test_seed_from_explicit_owner():
    with workspace_env() as (ws, records):
        access = {"allowFrom": ["111"], "owner": "999"}
        seed = discord_config.auto_seed_if_missing(access)
        expect_eq(seed.get("owner"), "999", "seed should propagate access.owner")
        if not (ws / "state" / discord_config.CONFIG_FILENAME).exists():
            fail("seed file should exist on disk after auto_seed_if_missing")
        msgs = [r.getMessage() for r in records]
        if not any("auto-seeded owner=999" in m for m in msgs):
            fail(f"expected INFO log of seed source; got: {msgs!r}")


def test_seed_from_tier_map_tag():
    with workspace_env() as (ws, records):
        access = {
            "allowFrom": ["111", "222"],
            "tierMap": {"222": "owner"},
        }
        seed = discord_config.auto_seed_if_missing(access)
        expect_eq(seed.get("owner"), "222", "tierMap tag should drive seed")
        expect_eq(
            seed.get("tierMap"),
            {"222": "owner"},
            "tierMap should be mirrored into seed",
        )


def test_seed_warns_on_allow_from_fallback():
    """Lucy #1147 watch-point #2 — operator must see a WARN if seed
    falls through to allowFrom[0]. Silence here recreates the bug."""
    with workspace_env() as (ws, records):
        access = {"allowFrom": ["123"]}  # no owner, no tierMap
        seed = discord_config.auto_seed_if_missing(access)
        expect_eq(seed.get("owner"), "123", "fallback seeds from allowFrom[0]")
        warns = [
            r for r in records if r.levelno >= logging.WARNING
        ]
        joined = " | ".join(r.getMessage() for r in warns)
        expect_in("allowFrom[0]", joined, "WARN must mention allowFrom[0] path")
        expect_in("VERIFY", joined, "WARN must instruct operator to VERIFY")


def test_seed_warns_on_empty():
    with workspace_env() as (ws, records):
        seed = discord_config.auto_seed_if_missing({})
        expect_eq(seed, {}, "no candidates → empty seed")
        warns = [r for r in records if r.levelno >= logging.WARNING]
        joined = " | ".join(r.getMessage() for r in warns)
        expect_in(
            "no owner candidates",
            joined,
            "WARN must surface when there's nothing to seed",
        )


def test_seed_is_idempotent():
    with workspace_env() as (ws, _):
        write_config(ws, {"owner": "existing-owner-id"})
        access = {"allowFrom": ["should-not-overwrite"]}
        seed = discord_config.auto_seed_if_missing(access)
        expect_eq(
            seed.get("owner"),
            "existing-owner-id",
            "idempotent: existing file must not be overwritten",
        )
        on_disk = json.loads(
            (ws / "state" / discord_config.CONFIG_FILENAME).read_text()
        )
        expect_eq(
            on_disk.get("owner"),
            "existing-owner-id",
            "disk state must match (no rewrite)",
        )


def test_save_load_roundtrip():
    with workspace_env() as (ws, _):
        discord_config.save_config({"owner": "rt", "tierMap": {"rt": "owner"}})
        loaded = discord_config.load_config()
        expect_eq(loaded.get("owner"), "rt", "roundtrip owner")
        expect_eq(
            loaded.get("tierMap"),
            {"rt": "owner"},
            "roundtrip tierMap",
        )


def test_load_returns_empty_on_corrupt_file():
    """Corrupt JSON must not crash — return {} so the legacy fallback
    chain keeps the bridge operational."""
    with workspace_env() as (ws, records):
        path = ws / "state" / discord_config.CONFIG_FILENAME
        path.write_text("{not-valid-json")
        expect_eq(
            discord_config.load_config(),
            {},
            "corrupt JSON must degrade to empty dict",
        )
        warns = [r for r in records if r.levelno >= logging.WARNING]
        joined = " | ".join(r.getMessage() for r in warns)
        expect_in("failed to read", joined, "WARN must surface read failure")


# ----- runner -------------------------------------------------------------


def main() -> int:
    tests = [
        test_env_override_wins,
        test_workspace_owner_wins,
        test_workspace_tier_map,
        test_legacy_owner_field,
        test_legacy_tier_map,
        test_returns_none_when_exhausted,
        test_empty_access_returns_none,
        test_stale_tier_tag_for_de_listed_user,
        test_injected_config_overrides_disk,
        test_seed_from_explicit_owner,
        test_seed_from_tier_map_tag,
        test_seed_warns_on_allow_from_fallback,
        test_seed_warns_on_empty,
        test_seed_is_idempotent,
        test_save_load_roundtrip,
        test_load_returns_empty_on_corrupt_file,
    ]
    for t in tests:
        try:
            t()
        except Exception as exc:  # noqa: BLE001
            fail(f"{t.__name__} raised: {exc!r}")

    if _FAILURES:
        print(f"\n{len(_FAILURES)} failure(s) in {len(tests)} tests", file=sys.stderr)
        return 1
    print(f"PASS: {len(tests)} discord_config tests")
    return 0


if __name__ == "__main__":
    sys.exit(main())
