#!/usr/bin/env python3
"""
Tests for the sutando-app hotkey detail derivation in health-check.py.

Motivated by the 2026-07-03 incident: the check hardcoded "running (⌃C/⌃V/⌃M)"
on a bare pgrep name match. On a host running an app lineage that registers no
global hotkeys (Electron shell), the claim was a false positive that misled
live debugging — and the hardcoded string had also drifted from the real
defaults (⌃⇧C/⌃S/⌃⇧R/⌃V/⌃M). Since #1920 the app publishes the registered
hotkeys to <workspace>/state/hotkeys.json; the check now reads that instead.

Covers `sutando_app_hotkey_detail`:
  a) missing hotkeys.json → "running (no hotkeys published)"
  b) valid file → labels joined in publish order
  c) malformed JSON → fallback (no false claim on half-written files)
  d) empty list → fallback
  e) entries without a label are skipped; label-less file → fallback
  f) non-dict entries (wrong shape) → fallback, no crash

Run: python3 tests/health-check-sutando-app-hotkeys.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Load src/health-check.py as `health_check` (filename has a hyphen, can't
# import directly).
spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

FAILURES = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'} {name}: {got!r}")
    if not ok:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")


def with_state(content):
    """Make a temp workspace; content=None → no hotkeys.json, str → raw write,
    other → json.dump."""
    ws = Path(tempfile.mkdtemp())
    (ws / "state").mkdir()
    if content is not None:
        p = ws / "state" / "hotkeys.json"
        p.write_text(content if isinstance(content, str) else json.dumps(content))
    return ws


# a) missing file
check("a_missing_file",
      hc.sutando_app_hotkey_detail(with_state(None)),
      "running (no hotkeys published)")

# b) valid published file (shape from main.swift publishHotkeys)
entries = [
    {"action": "drop_context", "label": "⌃⇧C", "key": "C", "modifiers": ["control", "shift"]},
    {"action": "drop_screenshot", "label": "⌃S", "key": "S", "modifiers": ["control"]},
    {"action": "toggle_voice", "label": "⌃V", "key": "V", "modifiers": ["control"]},
]
check("b_valid_file",
      hc.sutando_app_hotkey_detail(with_state(entries)),
      "running (hotkeys: ⌃⇧C/⌃S/⌃V)")

# c) malformed JSON
check("c_malformed_json",
      hc.sutando_app_hotkey_detail(with_state("{broken")),
      "running (no hotkeys published)")

# d) empty list
check("d_empty_list",
      hc.sutando_app_hotkey_detail(with_state([])),
      "running (no hotkeys published)")

# e) entries missing labels are skipped
check("e_partial_labels",
      hc.sutando_app_hotkey_detail(with_state([{"action": "x"}, {"action": "y", "label": "⌃M"}])),
      "running (hotkeys: ⌃M)")
check("e_all_label_less",
      hc.sutando_app_hotkey_detail(with_state([{"action": "x"}])),
      "running (no hotkeys published)")

# f) wrong shape (list of strings) → fallback, no crash
check("f_wrong_shape",
      hc.sutando_app_hotkey_detail(with_state(["not-a-dict"])),
      "running (no hotkeys published)")

if FAILURES:
    print(f"\n{len(FAILURES)} failure(s):")
    for f in FAILURES:
        print(f"  {f}")
    sys.exit(1)
print("\nAll tests passed.")
