#!/usr/bin/env python3
"""Tests for pure logic in src/screen-capture-server.py.

Covers the guard/debounce logic that runs outside the HTTP server itself:
  - _notify_capture(): debounce timing + NOTIFY_ENABLED=0 short-circuit
  - Display-number validation: coercion/clamping rules (1-9 valid, else None)
  - Format normalization: unknown fmt → "png" fallback; jpg/jpeg → ext="jpg"

The server is NOT started — only the module-level constants and pure helper
functions are exercised. Subprocess calls (osascript) are stubbed.

Run: python3 tests/screen-capture-server.test.py
Exit 0 on pass, 1 on fail.
"""
from __future__ import annotations
import importlib.util
import os
import sys
import threading
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "screen_capture_server", REPO / "src" / "screen-capture-server.py"
)
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)


def check(label: str, cond: bool, fails: list) -> None:
    if not cond:
        fails.append(label)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

def test_debounce_constant() -> list[str]:
    """NOTIFY_DEBOUNCE_S is 5.0 seconds — changing this breaks burst-capture UX."""
    fails: list[str] = []
    check(f"NOTIFY_DEBOUNCE_S changed: {sc.NOTIFY_DEBOUNCE_S}",
          sc.NOTIFY_DEBOUNCE_S == 5.0, fails)
    return fails


def test_port_constant() -> list[str]:
    """PORT is 7845 — changing breaks all callers (voice-agent, inline-tools)."""
    fails: list[str] = []
    check(f"PORT changed: {sc.PORT}", sc.PORT == 7845, fails)
    return fails


# ---------------------------------------------------------------------------
# _notify_capture() debounce
# ---------------------------------------------------------------------------

def test_notify_debounce_fires_first_call() -> list[str]:
    """First call fires a thread (debounce window empty at t=0)."""
    fails: list[str] = []
    sc._last_notify_ts = 0.0
    spawned = []
    original_enabled = sc.NOTIFY_ENABLED
    sc.NOTIFY_ENABLED = True
    try:
        with unittest.mock.patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = lambda: spawned.append(1)
            mock_thread.return_value = unittest.mock.MagicMock()
            with unittest.mock.patch("time.time", return_value=100.0):
                sc._notify_capture()
        check("first call should update _last_notify_ts",
              sc._last_notify_ts == 100.0, fails)
    finally:
        sc.NOTIFY_ENABLED = original_enabled
    return fails


def test_notify_debounce_suppresses_within_window() -> list[str]:
    """Second call within NOTIFY_DEBOUNCE_S is suppressed — _last_notify_ts unchanged."""
    fails: list[str] = []
    sc._last_notify_ts = 100.0
    original_enabled = sc.NOTIFY_ENABLED
    sc.NOTIFY_ENABLED = True
    try:
        thread_calls = []
        with unittest.mock.patch("threading.Thread", side_effect=lambda **kw: thread_calls.append(1) or unittest.mock.MagicMock()):
            with unittest.mock.patch("time.time", return_value=103.0):  # 3s < 5s debounce
                sc._notify_capture()
        check("within-window call should NOT spawn a thread", len(thread_calls) == 0, fails)
        check("_last_notify_ts must stay unchanged", sc._last_notify_ts == 100.0, fails)
    finally:
        sc.NOTIFY_ENABLED = original_enabled
    return fails


def test_notify_debounce_fires_after_window() -> list[str]:
    """Call after NOTIFY_DEBOUNCE_S passes through and updates _last_notify_ts."""
    fails: list[str] = []
    sc._last_notify_ts = 100.0
    original_enabled = sc.NOTIFY_ENABLED
    sc.NOTIFY_ENABLED = True
    try:
        thread_calls = []
        with unittest.mock.patch("threading.Thread", side_effect=lambda **kw: thread_calls.append(1) or unittest.mock.MagicMock()):
            with unittest.mock.patch("time.time", return_value=106.0):  # 6s > 5s debounce
                sc._notify_capture()
        check("post-window call should spawn a thread", len(thread_calls) == 1, fails)
        check("_last_notify_ts should be updated to 106.0", sc._last_notify_ts == 106.0, fails)
    finally:
        sc.NOTIFY_ENABLED = original_enabled
    return fails


def test_notify_disabled_skips_without_update() -> list[str]:
    """NOTIFY_ENABLED=False returns early; _last_notify_ts stays at 0."""
    fails: list[str] = []
    sc._last_notify_ts = 0.0
    original_enabled = sc.NOTIFY_ENABLED
    sc.NOTIFY_ENABLED = False
    try:
        thread_calls = []
        with unittest.mock.patch("threading.Thread", side_effect=lambda **kw: thread_calls.append(1) or unittest.mock.MagicMock()):
            with unittest.mock.patch("time.time", return_value=999.0):
                sc._notify_capture()
        check("disabled notify should not spawn thread", len(thread_calls) == 0, fails)
        check("disabled notify should not update _last_notify_ts", sc._last_notify_ts == 0.0, fails)
    finally:
        sc.NOTIFY_ENABLED = original_enabled
    return fails


# ---------------------------------------------------------------------------
# Display-number validation (inline expression extracted for testing)
# The expression in do_GET():
#   int(d) if d and d.isdigit() and 1 <= int(d) <= 9 else None
# ---------------------------------------------------------------------------

def _validate_display(display_raw):
    """Mirror of the inline expression in Handler.do_GET()."""
    return int(display_raw) if display_raw and display_raw.isdigit() and 1 <= int(display_raw) <= 9 else None


def test_display_valid_range() -> list[str]:
    """Display 1..9 are accepted as integers."""
    fails: list[str] = []
    for d in range(1, 10):
        result = _validate_display(str(d))
        check(f"display {d} should be accepted as int {d}", result == d, fails)
    return fails


def test_display_zero_rejected() -> list[str]:
    """Display 0 is outside the valid range → None."""
    fails: list[str] = []
    check("display 0 should be rejected", _validate_display("0") is None, fails)
    return fails


def test_display_ten_rejected() -> list[str]:
    """Display 10 exceeds max (9) → None."""
    fails: list[str] = []
    check("display 10 should be rejected", _validate_display("10") is None, fails)
    return fails


def test_display_none_input() -> list[str]:
    """None (query param absent) → None without crash."""
    fails: list[str] = []
    check("None display should return None", _validate_display(None) is None, fails)
    return fails


def test_display_alpha_rejected() -> list[str]:
    """Non-numeric string → None (isdigit() guard)."""
    fails: list[str] = []
    check("'abc' should be rejected", _validate_display("abc") is None, fails)
    check("'1a' should be rejected", _validate_display("1a") is None, fails)
    return fails


# ---------------------------------------------------------------------------
# Format normalization (inline logic in do_GET())
# ---------------------------------------------------------------------------

def _normalize_fmt(fmt: str) -> tuple[str, str]:
    """Mirror of the inline format logic in Handler.do_GET()."""
    if fmt not in ("png", "jpg", "jpeg"):
        fmt = "png"
    ext = "jpg" if fmt in ("jpg", "jpeg") else "png"
    type_flag = "jpg" if ext == "jpg" else "png"
    return ext, type_flag


def test_format_png() -> list[str]:
    """'png' → ext='png', type_flag='png'."""
    fails: list[str] = []
    ext, flag = _normalize_fmt("png")
    check(f"png ext wrong: {ext}", ext == "png", fails)
    check(f"png flag wrong: {flag}", flag == "png", fails)
    return fails


def test_format_jpg() -> list[str]:
    """'jpg' → ext='jpg', type_flag='jpg'."""
    fails: list[str] = []
    ext, flag = _normalize_fmt("jpg")
    check(f"jpg ext wrong: {ext}", ext == "jpg", fails)
    check(f"jpg flag wrong: {flag}", flag == "jpg", fails)
    return fails


def test_format_jpeg() -> list[str]:
    """'jpeg' → ext='jpg' (normalized), type_flag='jpg'."""
    fails: list[str] = []
    ext, flag = _normalize_fmt("jpeg")
    check(f"jpeg ext wrong: {ext}", ext == "jpg", fails)
    check(f"jpeg flag wrong: {flag}", flag == "jpg", fails)
    return fails


def test_format_unknown_falls_back_to_png() -> list[str]:
    """Unknown format string → 'png' fallback."""
    fails: list[str] = []
    for bad in ("bmp", "webp", "tiff", "", "PNG"):
        ext, flag = _normalize_fmt(bad)
        check(f"'{bad}' should fall back to png ext, got {ext!r}", ext == "png", fails)
        check(f"'{bad}' should fall back to png flag, got {flag!r}", flag == "png", fails)
    return fails


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    cases = [
        ("constants: NOTIFY_DEBOUNCE_S == 5.0", test_debounce_constant),
        ("constants: PORT == 7845", test_port_constant),
        ("debounce: first call updates _last_notify_ts", test_notify_debounce_fires_first_call),
        ("debounce: within-window suppressed, ts unchanged", test_notify_debounce_suppresses_within_window),
        ("debounce: post-window fires and updates ts", test_notify_debounce_fires_after_window),
        ("debounce: NOTIFY_ENABLED=0 exits early, ts unchanged", test_notify_disabled_skips_without_update),
        ("display: 1..9 accepted as ints", test_display_valid_range),
        ("display: 0 rejected → None", test_display_zero_rejected),
        ("display: 10 rejected → None", test_display_ten_rejected),
        ("display: None (absent) → None", test_display_none_input),
        ("display: alpha string rejected → None", test_display_alpha_rejected),
        ("format: 'png' → ext=png, flag=png", test_format_png),
        ("format: 'jpg' → ext=jpg, flag=jpg", test_format_jpg),
        ("format: 'jpeg' → ext=jpg, flag=jpg", test_format_jpeg),
        ("format: unknown → png fallback", test_format_unknown_falls_back_to_png),
    ]
    all_failures: list[str] = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as exc:
            fails = [f"raised {type(exc).__name__}: {exc}"]
        if fails:
            print(f"  ✗ {label}")
            for f in fails:
                print(f"      {f}")
            all_failures.extend(fails)
        else:
            print(f"  ✓ {label}")

    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    total = len(cases)
    print(f"\nscreen-capture-server: {total}/{total} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
