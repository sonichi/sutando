"""Shared pytest fixtures for migration tests."""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

# Make src/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


@pytest.fixture()
def fake_home(tmp_path: Path) -> Path:
    """Return a tmp dir wired as $HOME + fake ~/.sutando/env."""
    workspace = tmp_path / ".sutando" / "workspace"
    (workspace / "state").mkdir(parents=True)
    return tmp_path


@pytest.fixture()
def workspace(fake_home: Path) -> Path:
    return fake_home / ".sutando" / "workspace"


@pytest.fixture()
def migrations_dir(tmp_path: Path) -> Path:
    d = tmp_path / "migrations"
    d.mkdir()
    return d


def make_migration(migrations_dir: Path, number: int, body: str = "exit 0", suffix: str = ".sh") -> Path:
    """Write a migration script NNNN-test<suffix> and return its path."""
    p = migrations_dir / f"{number:04d}-test{suffix}"
    if suffix == ".sh":
        p.write_text(f"#!/bin/bash\n{body}\n")
        p.chmod(0o755)
    else:
        p.write_text(textwrap.dedent(body))
    return p
