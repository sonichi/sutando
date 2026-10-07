#!/usr/bin/env python3
"""Battery stat must not render as "? ⚡" on battery-less Macs.

## Why this test exists

`get_system_stats()` in `src/dashboard.py` parses `pmset -g batt`. Two
pre-fix bugs in the same two lines:

1. On desktop Macs (mini / Studio / Pro) the output contains "Now
   drawing from 'AC Power'" and **no percentage line**, so the code set
   `battery = "?"` while `"ac power"` still set `charging = True` — and
   the dashboard's stat tile rendered the pair as `? ⚡` (glyph soup
   that reads like a sensor error). Fixed: em-dash, no charge bolt.
2. The charging check used a plain substring — and "discharging"
   contains "charging", so laptops running **on battery** also showed
   the ⚡ bolt. Fixed with a word-boundary match.

Plain-python self-runner (no pytest — CI runs these files directly):
imports the real module and stubs `subprocess.run` with captured pmset
shapes for both machine classes.
"""

import importlib.util
import shutil
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

spec = importlib.util.spec_from_file_location("dashboard", REPO / "src" / "dashboard.py")
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)

DESKTOP_PMSET = "Now drawing from 'AC Power'\n"
LAPTOP_AC_PMSET = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=1234)\t82%; charging; 1:07 remaining present: true\n"
)
LAPTOP_BATT_PMSET = (
    "Now drawing from 'Battery Power'\n"
    " -InternalBattery-0 (id=1234)\t64%; discharging; 3:12 remaining present: true\n"
)
LAPTOP_CHARGED_PMSET = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=1234)\t100%; charged; 0:00 remaining present: true\n"
)


def _stats_with(pmset_stdout):
    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(stdout=pmset_stdout, stderr="", returncode=0)

    orig_run = dashboard.subprocess.run
    orig_disk_usage = shutil.disk_usage
    dashboard.subprocess.run = fake_run
    shutil.disk_usage = lambda _path: types.SimpleNamespace(free=100 * 1024 ** 3)
    try:
        return dashboard.get_system_stats()
    finally:
        dashboard.subprocess.run = orig_run
        shutil.disk_usage = orig_disk_usage


def test_desktop_mac_shows_dash_and_no_bolt():
    """No percentage in pmset output → em-dash, charging False so the UI adds no ⚡."""
    stats = _stats_with(DESKTOP_PMSET)
    assert stats["battery"] == "—", stats["battery"]
    assert stats["charging"] is False, stats["charging"]


def test_laptop_on_ac_keeps_percent_and_bolt():
    stats = _stats_with(LAPTOP_AC_PMSET)
    assert stats["battery"] == "82%", stats["battery"]
    assert stats["charging"] is True, stats["charging"]


def test_laptop_on_battery_keeps_percent_no_bolt():
    """"discharging" must not substring-match as charging."""
    stats = _stats_with(LAPTOP_BATT_PMSET)
    assert stats["battery"] == "64%", stats["battery"]
    assert stats["charging"] is False, stats["charging"]


def test_laptop_charged_on_ac_keeps_bolt():
    """Fully charged on AC: "ac power" branch still shows the bolt."""
    stats = _stats_with(LAPTOP_CHARGED_PMSET)
    assert stats["battery"] == "100%", stats["battery"]
    assert stats["charging"] is True, stats["charging"]


def test_pmset_absent_is_a_supported_platform_fallback():
    orig_run = dashboard.subprocess.run
    orig_disk_usage = shutil.disk_usage
    dashboard.subprocess.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError())
    shutil.disk_usage = lambda _path: types.SimpleNamespace(free=100 * 1024 ** 3)
    try:
        stats = dashboard.get_system_stats()
    finally:
        dashboard.subprocess.run = orig_run
        shutil.disk_usage = orig_disk_usage
    assert stats["battery"] == "—", stats["battery"]
    assert stats["charging"] is False, stats["charging"]


def main():
    failures = []
    for fn in (
        test_desktop_mac_shows_dash_and_no_bolt,
        test_laptop_on_ac_keeps_percent_and_bolt,
        test_laptop_on_battery_keeps_percent_no_bolt,
        test_laptop_charged_on_ac_keeps_bolt,
        test_pmset_absent_is_a_supported_platform_fallback,
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
    print("All dashboard-battery-fallback tests passed.")


if __name__ == "__main__":
    main()
