"""Baseline harness validation — runner exits 0 when nothing to apply."""
from __future__ import annotations

from pathlib import Path

import pytest

from run_migrations import list_pending, read_state, run_migrations, write_state
from tests.migrations.conftest import make_migration  # noqa: F401 (used via fixture param)


def test_no_migrations_fresh_state(workspace: Path, migrations_dir: Path) -> None:
    """Empty migrations dir + no state file → 0 pending, exit 0."""
    assert list_pending(workspace, migrations_dir) == []
    assert run_migrations(workspace, migrations_dir) == 0


def test_state_defaults_when_missing(workspace: Path) -> None:
    """Missing schema-version.json → treated as empty state."""
    state = read_state(workspace)
    assert state["applied"] == []
    assert state["current"] == 0


def test_write_then_read_state(workspace: Path) -> None:
    state = {"applied": [1, 2], "current": 2, "engine_version_at_apply": "v0.1.0"}
    write_state(workspace, state)
    loaded = read_state(workspace)
    assert loaded["applied"] == [1, 2]
    assert loaded["current"] == 2
    assert loaded["engine_version_at_apply"] == "v0.1.0"


def test_pending_excludes_already_applied(workspace: Path, migrations_dir: Path) -> None:
    make_migration(migrations_dir, 1)
    make_migration(migrations_dir, 2)
    write_state(workspace, {"applied": [1], "current": 1})
    pending = list_pending(workspace, migrations_dir)
    assert len(pending) == 1
    assert pending[0][0] == 2


def test_run_applies_script_and_records(workspace: Path, migrations_dir: Path) -> None:
    make_migration(migrations_dir, 1, body="exit 0")
    rc = run_migrations(workspace, migrations_dir)
    assert rc == 0
    state = read_state(workspace)
    assert 1 in state["applied"]
    assert state["current"] == 1


def test_run_stops_on_failure(workspace: Path, migrations_dir: Path) -> None:
    make_migration(migrations_dir, 1, body="exit 0")
    make_migration(migrations_dir, 2, body="exit 1")
    make_migration(migrations_dir, 3, body="exit 0")
    rc = run_migrations(workspace, migrations_dir)
    assert rc == 1
    state = read_state(workspace)
    assert 1 in state["applied"]
    assert 2 not in state["applied"]
    assert 3 not in state["applied"]


def test_dry_run_does_not_mutate_state(workspace: Path, migrations_dir: Path) -> None:
    make_migration(migrations_dir, 1)
    rc = run_migrations(workspace, migrations_dir, dry_run=True)
    assert rc == 0
    state = read_state(workspace)
    assert state["applied"] == []


def test_python_migration_runs(workspace: Path, migrations_dir: Path) -> None:
    make_migration(migrations_dir, 1, body="import sys; sys.exit(0)", suffix=".py")
    rc = run_migrations(workspace, migrations_dir)
    assert rc == 0
    state = read_state(workspace)
    assert 1 in state["applied"]


def test_idempotent_rerun(workspace: Path, migrations_dir: Path) -> None:
    """Running the runner twice on the same workspace applies each migration once."""
    make_migration(migrations_dir, 1)
    run_migrations(workspace, migrations_dir)
    rc = run_migrations(workspace, migrations_dir)
    assert rc == 0
    state = read_state(workspace)
    assert state["applied"].count(1) == 1
