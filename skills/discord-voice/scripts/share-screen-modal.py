#!/usr/bin/env python3
"""Fast Discord share-screen driver via CGEvent.

Single-process, no MCP, no task-bridge — meant to be spawned directly by
discord-voice-server.ts so end-to-end latency is sub-2s instead of ~20s
(which was the cost of routing through the proactive-loop task-bridge).

Click sequence (default mode --full):
    1. Discord "Share Your Screen" button in the voice strip       (338, 809)
    2. wait ~400ms for Chrome's native picker modal to render
    3. "Entire Screen" tab                                        (1142, 211)
    4. screen thumbnail                                            (825, 355)
    5. "Share" button                                             (1206, 656)

Modes:
    --full      (default) clicks all 5 (1 Discord + 3 modal + 1 share)
    --modal     skip Discord button; just drive the modal (legacy path)
    --stop      single click on Discord button at (338,809) — stops a
                live share (button morphs to "Stop Streaming" when active)
    --dry-run   print coords only, no clicks

Coords are calibrated for the MCP-Chrome instance (PID main: $(pgrep -f
'Google Chrome.*chrome-devtools-mcp/chrome-profile' | head -1)) when:
  - the Chrome window is maximized at the default macOS top-left position
    (screenX=0, screenY=32, outerHeight≈972 with topChromeOffset≈139)
  - the user is connected to a Discord voice channel and the voice
    strip is visible at the bottom-left of the page
If Chrome window is moved/resized, coords drift. Re-derive via
`macos-use refresh_traversal` on the Chrome main PID, then grep for
"Share Your Screen" / "Entire Screen" / "Share" in the .txt output.
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


# Screen coords (points, top-left origin) measured 2026-05-17 via
# macos-use refresh_traversal on the MCP Chrome main process.
COORDS = {
    "discord_share_button": (338, 809),  # voice-strip btn at 322,793 w=32 h=32
    "entire_screen_tab":    (1142, 211),  # tab at 1041,195 w=203 h=32
    "thumbnail":            (825, 355),   # at 692,243 w=266 h=224
    "share_button":         (1206, 656),  # at 1168,638 w=76 h=36
}


def click(x: int, y: int) -> None:
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, (x, y), kCGMouseButtonLeft)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (x, y), kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, down)
    time.sleep(0.04)
    CGEventPost(kCGHIDEventTap, up)


def main() -> int:
    p = argparse.ArgumentParser()
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true",
                      help="(default) Discord button + 3 modal clicks (5 clicks total)")
    mode.add_argument("--modal", action="store_true",
                      help="modal-only — 3 clicks, no Discord button (legacy path)")
    mode.add_argument("--stop", action="store_true",
                      help="single click on Discord button to stop a live share")
    p.add_argument("--dry-run", action="store_true", help="print coords, don't click")
    p.add_argument("--modal-wait", type=float, default=0.4,
                   help="seconds to wait after Discord click for picker to render")
    p.add_argument("--inter-click", type=float, default=0.15,
                   help="seconds between modal clicks (let DOM react)")
    args = p.parse_args()

    if args.dry_run:
        for name, (x, y) in COORDS.items():
            print(f"  {name}: ({x}, {y})")
        return 0

    start = time.time()

    if args.stop:
        click(*COORDS["discord_share_button"])
        elapsed = time.time() - start
        print(f"share-screen-modal: stop-click done in {elapsed:.3f}s")
        return 0

    if not args.modal:  # default = --full
        click(*COORDS["discord_share_button"])
        time.sleep(args.modal_wait)

    click(*COORDS["entire_screen_tab"])
    time.sleep(args.inter_click)
    click(*COORDS["thumbnail"])
    time.sleep(args.inter_click)
    click(*COORDS["share_button"])

    elapsed = time.time() - start
    nclicks = 5 if not args.modal else 3
    print(f"share-screen-modal: {nclicks} clicks done in {elapsed:.3f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
