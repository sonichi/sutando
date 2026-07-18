#!/usr/bin/env python3
"""Regression guard: Dashboard quick links must not trap the desktop iframe."""

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "src" / "dashboard.py").read_text()


def _quick_links_block() -> str:
    match = re.search(r'<div class="quick-links">(?P<body>.*?)</div></div>"""', SRC, re.DOTALL)
    assert match is not None, "Dashboard quick-links block is missing"
    return match.group("body")


def test_quick_links_open_outside_dashboard_frame():
    block = _quick_links_block()
    links = re.findall(r"<a\s+[^>]*href=", block)
    targets = re.findall(r'target="_blank"', block)
    assert links, "Dashboard quick-links block has no links"
    assert len(targets) == len(links), (
        "Every Dashboard quick link must use target=\"_blank\" so clicking it "
        "does not navigate the embedded desktop iframe away from Dashboard."
    )


def test_quick_links_use_noopener():
    block = _quick_links_block()
    links = re.findall(r"<a\s+[^>]*href=", block)
    rels = re.findall(r'rel="noopener noreferrer"', block)
    assert len(rels) == len(links), (
        "Every target=_blank Dashboard quick link must include "
        "rel=\"noopener noreferrer\"."
    )


def test_quick_links_prevent_same_frame_navigation():
    block = _quick_links_block()
    links = re.findall(r"<a\s+[^>]*href=", block)
    handlers = re.findall(r'onclick="openQuickLink\(event,this\)"', block)
    assert "function openQuickLink(event, link)" in SRC, "openQuickLink handler is missing"
    assert len(handlers) == len(links), (
        "Every Dashboard quick link must intercept clicks so embedded desktop "
        "iframes cannot navigate away from the Dashboard page."
    )


def main():
    failures = []
    for fn in (
        test_quick_links_open_outside_dashboard_frame,
        test_quick_links_use_noopener,
        test_quick_links_prevent_same_frame_navigation,
    ):
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failures.append(f"{fn.__name__}: {e}")
            print(f"  ✗ {fn.__name__}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    print("All dashboard quick-link tests passed.")


if __name__ == "__main__":
    main()
