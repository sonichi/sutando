#!/usr/bin/env python3
"""Fast Chrome share-screen modal driver via CGEvent (skips macos-use traversal).

Use after clicking Discord's "Share Your Screen" button — this script then
drives the Chrome native picker (Entire Screen → thumbnail → Share button)
via CGEvent mouse clicks. ~0.5s total vs macos-use's ~3s (which always does
a11y refresh_traversal after every click).

Coords are hard-coded for the standard Chrome share-picker layout when the
Chrome window is in its usual position (top-left of display, 1920x1080-ish).
If Chrome window is moved/resized, coords may drift — re-derive via
macos-use refresh_traversal on the MCP-Chrome PID.

Usage:
    python3 share-screen-modal.py                # default: standard 3-click sequence
    python3 share-screen-modal.py --dry-run      # print coords, don't click
"""
from __future__ import annotations
import argparse
import sys
import time

try:
    from Quartz import (
        CGEventCreateMouseEvent,
        CGEventPost,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseUp,
        kCGHIDEventTap,
        kCGMouseButtonLeft,
    )
except ImportError:
    print("share-screen-modal: requires pyobjc-framework-Quartz "
          "(pip3 install --break-system-packages --user pyobjc-framework-Quartz)",
          file=sys.stderr)
    sys.exit(2)


# Coords measured 2026-05-17 via macos-use refresh_traversal on the MCP Chrome
# (PID via `pgrep -f chrome-devtools-mcp/chrome-profile`). Centers of the
# tracked elements.
COORDS = {
    "entire_screen_tab": (1142, 211),  # tab at 1041,195 w=203 h=32
    "thumbnail":         (825, 355),   # thumbnail at 692,243 w=266 h=224
    "share_button":      (1206, 656),  # share btn at 1168,638 w=76 h=36
}


def click(x: int, y: int) -> None:
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, (x, y), kCGMouseButtonLeft)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (x, y), kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, down)
    time.sleep(0.04)
    CGEventPost(kCGHIDEventTap, up)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="print coords, don't click")
    p.add_argument("--wait-before", type=float, default=0.3,
                   help="seconds to wait before first click (let Chrome render modal)")
    p.add_argument("--inter-click", type=float, default=0.15,
                   help="seconds between clicks (let DOM react)")
    args = p.parse_args()

    if args.dry_run:
        for name, (x, y) in COORDS.items():
            print(f"  {name}: ({x}, {y})")
        return 0

    time.sleep(args.wait_before)
    start = time.time()
    click(*COORDS["entire_screen_tab"])
    time.sleep(args.inter_click)
    click(*COORDS["thumbnail"])
    time.sleep(args.inter_click)
    click(*COORDS["share_button"])
    elapsed = time.time() - start
    print(f"share-screen-modal: 3 clicks done in {elapsed:.3f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
