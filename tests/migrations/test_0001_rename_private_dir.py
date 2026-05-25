"""Tests for migrations/0001-rename-private-dir-to-memory-dir.sh."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

MIGRATION = Path(__file__).resolve().parents[2] / "migrations" / "0001-rename-private-dir-to-memory-dir.sh"


def run_migration(env_file: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(MIGRATION)],
        capture_output=True,
        text=True,
        env={**os.environ, "SUTANDO_ENV_FILE": str(env_file)},
    )


def test_renames_old_var(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("SUTANDO_PRIVATE_DIR=/foo/bar\n")

    result = run_migration(env_file)

    assert result.returncode == 0
    content = env_file.read_text()
    assert "SUTANDO_MEMORY_DIR=/foo/bar" in content
    assert "SUTANDO_PRIVATE_DIR" not in content


def test_idempotent_when_already_renamed(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("SUTANDO_MEMORY_DIR=/foo/bar\n")

    result = run_migration(env_file)

    assert result.returncode == 0
    content = env_file.read_text()
    assert content == "SUTANDO_MEMORY_DIR=/foo/bar\n"
    assert "SUTANDO_PRIVATE_DIR" not in content


def test_idempotent_when_neither_set(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("OTHER_VAR=hello\n")

    result = run_migration(env_file)

    assert result.returncode == 0
    assert env_file.read_text() == "OTHER_VAR=hello\n"


def test_preserves_other_vars(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("OTHER_VAR=bar\nSUTANDO_PRIVATE_DIR=/baz\nANOTHER=qux\n")

    result = run_migration(env_file)

    assert result.returncode == 0
    content = env_file.read_text()
    assert "OTHER_VAR=bar" in content
    assert "ANOTHER=qux" in content
    assert "SUTANDO_MEMORY_DIR=/baz" in content
    assert "SUTANDO_PRIVATE_DIR" not in content


def test_no_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "nonexistent_env"

    result = run_migration(env_file)

    assert result.returncode == 0
    assert not env_file.exists()
