#!/usr/bin/env python3
"""A missing quota reading must not render as "0% used" behind a green check.

## Why this test exists

`get_quota_status()` already degrades honestly: no `quota-state.json` returns
`{"available": True}` with **no numbers**, precisely so the panel cannot imply
freshness it can't vouch for. The tiles in `render_dashboard()` then undid
that — they formatted utilization with `.get("utilization_5h", 0)` and picked
the glyph off `available`, so an ABSENT file rendered as:

    ✓ QUOTA (no data)      0% 5H USED      0% 7D USED

which reads as "quota healthy, nothing consumed" — the opposite of the truth.
Observed on a live host 2026-08-11, where nothing had written the file for the
whole session because the core runs without ANTHROPIC_BASE_URL (sonichi#2417):
the owner reasonably read the tile as "quota is fine" and separately as "the
quota is not showing the right info".

Absence must look like absence. This pins the em-dash rendering so the zero
default cannot come back.

Plain-python self-runner (no pytest — CI runs these files directly).
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location("dashboard", REPO / "src" / "dashboard.py")
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)

NO_DATA = {"available": True}                      # exactly what a missing file yields
HAS_DATA = {                                        # a real reading, for the control
    "headers": {
        "anthropic-ratelimit-unified-5h-utilization": 0.42,
        "anthropic-ratelimit-unified-7d-utilization": 0.13,
    },
    "age_h": 0.2,
    "stale": False,
    "available": True,
}


def _render_with(quota: dict) -> str:
    """Render with a fully synthetic stats dict — never the real collector.

    `get_system_stats()` shells out to `/usr/bin/pmset`, which does not exist on
    the Linux CI runner, so calling it and patching only `quota` raised
    FileNotFoundError before any assertion ran. `render_dashboard()` reads
    exactly four keys off stats, so a complete fake is cheap and keeps this
    hermetic on every platform.
    """
    real = dashboard.get_system_stats
    dashboard.get_system_stats = lambda: {
        "disk_free": "53GB",
        "battery": "42%",
        "charging": False,
        "quota": quota,
    }
    try:
        return dashboard.render_dashboard()
    finally:
        dashboard.get_system_stats = real


def test_missing_reading_renders_as_absent_not_zero() -> None:
    html = _render_with(NO_DATA)
    assert "no data" in html, "the age label should still say so"
    # The bug: both utilization tiles fell back to 0 and printed "0%".
    assert ">0%<" not in html, 'absent quota rendered as "0%" — the zero default is back'
    assert html.count(">—<") >= 3, (
        "expected em-dashes for the glyph and both utilization tiles, got "
        f"{html.count('>—<')}"
    )


def test_a_real_reading_still_shows_numbers() -> None:
    """Control: the fix must not blank out a genuine reading."""
    html = _render_with(HAS_DATA)
    assert ">42%<" in html, "5h utilization should render from a real reading"
    assert ">13%<" in html, "7d utilization should render from a real reading"
    assert ">✓<" in html, "a fresh real reading should still show the check"


if __name__ == "__main__":
    test_missing_reading_renders_as_absent_not_zero()
    test_a_real_reading_still_shows_numbers()
    print("dashboard-quota-no-data: PASS")
