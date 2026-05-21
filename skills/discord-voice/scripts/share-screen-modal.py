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
import subprocess
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
# The Discord button uses the MAIN VIEW Share Your Screen — the voice-strip
# variant is unreliable (disappears when other voice participants leave).
# Main-view button is at (1056, 951) w=41 h=40 → center (1077, 971). Requires
# the voice channel detail to be the current view (Discord page = channel).
COORDS = {
    "discord_share_button": (1077, 971),  # main-view btn (stable)
    "entire_screen_tab":    (1142, 211),  # tab at 1041,195 w=203 h=32
    "thumbnail":            (825, 355),   # at 692,243 w=266 h=224
    "share_button":         (1206, 656),  # at 1168,638 w=76 h=36
}


def activate_mcp_chrome() -> None:
    """Bring the MCP-Chrome window (NOT user's regular Chrome) to front so
    CGEvent clicks register as button-presses. The user typically has two
    Chrome instances running: the regular one with their normal profile, and
    the chrome-devtools-mcp instance with the Discord webapp. `tell
    application "Google Chrome" to activate` picks the wrong one half the
    time. Activate by PID via System Events instead."""
    try:
        pid = subprocess.run(
            ["pgrep", "-f",
             "Google Chrome.app/Contents/MacOS/Google Chrome --.*chrome-devtools-mcp/chrome-profile"],
            check=False, timeout=1, capture_output=True, text=True,
        ).stdout.strip().splitlines()
        if not pid:
            return
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to set frontmost of (first process whose unix id is {pid[0]}) to true'],
            check=False, timeout=1, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


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

    # Bring MCP Chrome to front so CGEvent clicks on the Discord button
    # register as button-presses (focus-only swallow otherwise, especially
    # since user's regular Chrome shares the bundle id). Modal-only mode
    # skips this — the modal is a native macOS dialog, already frontmost
    # when it appears, so its 3 clicks don't need Chrome focus.
    if not args.modal:
        activate_mcp_chrome()
        time.sleep(0.08)

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
