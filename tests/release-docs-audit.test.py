#!/usr/bin/env python3
"""Behavioral coverage for the release skill documentation audit."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
AUDIT = REPO / "skills" / "release" / "scripts" / "docs_audit.py"


def write_fixture(root: Path) -> None:
    (root / "docs").mkdir(parents=True)
    (root / "README.md").write_text("# Root\n\n[Docs](docs/README.md)\n")
    (root / "docs" / "README.md").write_text("# Docs\n\n[Guide](guide.md)\n")
    (root / "docs" / "guide.md").write_text("# Guide\n\n[Root](../README.md)\n")
    catalog = {
        "schema_version": 1,
        "documents": {
            "docs/README.md": {
                "title": "Docs",
                "audience": ["users"],
                "status": "canonical",
                "canonical_for": "documentation navigation",
                "last_verified": "2026-07-22",
            },
            "docs/guide.md": {
                "title": "Guide",
                "audience": ["users"],
                "status": "active",
                "canonical_for": "fixture guidance",
                "last_verified": None,
            },
        },
    }
    (root / "docs" / "catalog.json").write_text(json.dumps(catalog))


def run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(AUDIT), "--repo-root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail}")
    print(f"ok   {label}")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_fixture(root)
        result = run(root)
        check("valid index, catalog, and links pass", result.returncode == 0, result.stderr)

        (root / "docs" / "orphan.md").write_text("# Orphan\n")
        result = run(root)
        check("unindexed document fails", result.returncode == 1 and "unindexed document" in result.stderr)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_fixture(root)
        (root / "docs" / "guide.md").write_text("# Guide\n\n[Missing](missing.md)\n")
        result = run(root)
        check("broken local link fails", result.returncode == 1 and "broken local link" in result.stderr)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_fixture(root)
        catalog_path = root / "docs" / "catalog.json"
        catalog = json.loads(catalog_path.read_text())
        del catalog["documents"]["docs/guide.md"]["canonical_for"]
        catalog_path.write_text(json.dumps(catalog))
        result = run(root)
        check("missing metadata fails", result.returncode == 1 and "missing fields" in result.stderr)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        write_fixture(root)
        catalog_path = root / "docs" / "catalog.json"
        catalog = json.loads(catalog_path.read_text())
        catalog["documents"]["docs/guide.md"]["last_verified"] = "not-a-date"
        catalog_path.write_text(json.dumps(catalog))
        result = run(root)
        check("invalid verification date fails", result.returncode == 1 and "last_verified" in result.stderr)

    print("ALL PASS (5 checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
