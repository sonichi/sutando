#!/usr/bin/env python3
"""Forwarder: this skill is now `room-collab`. Runs the new script in place, so
a caller with the old path in its notes keeps working; the note below says
where to go. Remove one release after the rename."""
import os
import sys

NEW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "room-collab", "scripts", "room_collab.py")
print("note: skills/room-doc is now skills/room-collab (same commands; run room_collab.py)", file=sys.stderr)
os.execv(sys.executable, [sys.executable, os.path.normpath(NEW), *sys.argv[1:]])
