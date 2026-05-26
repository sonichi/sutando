#!/usr/bin/env python3
"""Tests: from __future__ import annotations present in dashboard.py, telegram-bridge.py,
and slack-bridge.py. Also verifies no duplicates (double-import was introduced by
merge overlap — regression guard added here).

Stock macOS ships python3.9; X | None union syntax fails at runtime on 3.9 without
this import. Port of upstream #1068.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

FILES = [
    "src/dashboard.py",
    "src/telegram-bridge.py",
    "src/slack-bridge.py",
]


def _check_file(rel_path: str):
    src = REPO / rel_path
    assert src.exists(), f"{rel_path} not found"
    text = src.read_text()
    assert "from __future__ import annotations" in text, (
        f"missing 'from __future__ import annotations' in {rel_path}"
    )
    lines = text.splitlines()
    positions = [i for i, l in enumerate(lines) if "from __future__ import annotations" in l]
    assert len(positions) == 1, (
        f"expected exactly 1 occurrence of annotation import in {rel_path}, "
        f"found {len(positions)} at lines {[p + 1 for p in positions]}"
    )
    assert positions[0] < 40, (
        f"annotation import must appear early in {rel_path}, "
        f"found at line {positions[0] + 1}"
    )


def test_dashboard_py39():
    _check_file("src/dashboard.py")


def test_telegram_bridge_py39():
    _check_file("src/telegram-bridge.py")


def test_slack_bridge_py39():
    _check_file("src/slack-bridge.py")


def test_no_duplicate_imports():
    for rel in FILES:
        text = (REPO / rel).read_text()
        count = text.count("from __future__ import annotations")
        assert count == 1, (
            f"{rel}: expected 1 occurrence of annotation import, got {count}"
        )


def test_files_parse_cleanly():
    import ast
    for rel in FILES:
        src = (REPO / rel).read_text()
        try:
            ast.parse(src)
        except SyntaxError as e:
            raise AssertionError(f"syntax error in {rel}: {e}") from e


if __name__ == "__main__":
    test_dashboard_py39()
    test_telegram_bridge_py39()
    test_slack_bridge_py39()
    test_no_duplicate_imports()
    test_files_parse_cleanly()
    print("5/5 passed")
