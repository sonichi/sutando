#!/usr/bin/env python3
"""Behavioral coverage for the release skill documentation audit."""

import json
import shutil
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


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    )


def write_feature_list(root: Path, revision: str, reviewed_at: str = "") -> None:
    path = root / "skills" / "tutorial" / "feature-list.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"schema_version": 1, "revision": revision, "steps": [
        {"id": "calls", "watches": ["skills/voice/"]},
        {"id": "apps", "watches": ["src/apps.py"]},
        {"id": "rooms"},
    ]}
    if reviewed_at:
        data["reviewed_at"] = reviewed_at
    path.write_text(json.dumps(data))


def released_fixture(root: Path) -> None:
    write_fixture(root)
    write_feature_list(root, "tutorial.1")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print(1)\n")
    (root / "src" / "apps.py").write_text("print(1)\n")
    (root / "skills" / "voice").mkdir(parents=True)
    (root / "skills" / "voice" / "call.py").write_text("print(1)\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "release")
    git(root, "tag", "-a", "v0.1.0", "-m", "v0.1.0")


def commit_all(root: Path, message: str) -> None:
    git(root, "add", "-A")
    git(root, "commit", "-qm", message)


FAILED: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        FAILED.append(label)
        print(f"FAIL {label}: {detail.strip()[:300]}")
        return
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

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        released_fixture(root)
        (root / "skills" / "voice" / "call.py").write_text("print(2)\n")
        commit_all(root, "watched change")
        result = run(root)
        check(
            "watched path change with unchanged revision warns naming the step, exit 0",
            result.returncode == 0 and "step(s) calls changed since v0.1.0" in result.stderr,
            result.stderr,
        )

        write_feature_list(root, "tutorial.1", reviewed_at="v0.0.9")
        commit_all(root, "stale acknowledgement")
        result = run(root)
        check("reviewed_at naming an older tag does not silence", "step(s) calls" in result.stderr, result.stderr)

        write_feature_list(root, "tutorial.1", reviewed_at="v0.1.0")
        commit_all(root, "acknowledge")
        result = run(root)
        check("reviewed_at equal to the baseline tag silences", "WARN" not in result.stderr, result.stderr)

        write_feature_list(root, "tutorial.2")
        commit_all(root, "bump tour")
        result = run(root)
        check("bumped revision clears the warning", "WARN" not in result.stderr, result.stderr)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        released_fixture(root)
        (root / "src" / "app.py").write_text("print(2)\n")
        (root / "CONTRIBUTING.md").write_text("# Contributing\n")
        commit_all(root, "unwatched change")
        result = run(root)
        check("unwatched src/ change does not warn", result.returncode == 0 and "WARN" not in result.stderr,
              result.stderr)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        released_fixture(root)
        (root / "src" / "apps.py").write_text("print(2)\n")
        commit_all(root, "watched file change")
        git(root, "tag", "-a", "v0.2.0-rc.1", "-m", "rc")
        git(root, "tag", "-a", "v0.2.0-pre-x", "-m", "pre")
        result = run(root)
        check("-rc/-pre tags are not the baseline", "step(s) apps changed since v0.1.0" in result.stderr,
              result.stderr)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        released_fixture(root)
        shutil.rmtree(root / "skills" / "voice")
        commit_all(root, "remove watched dir")
        result = run(root)
        check("a watched path that does not exist warns", "watches skills/voice/, which does not exist"
              in result.stderr, result.stderr)

    if FAILED:
        print(f"FAILED {len(FAILED)} of 12 checks")
        return 1
    print("ALL PASS (12 checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
