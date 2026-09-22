#!/usr/bin/env python3
"""Forwarder beside the new script: the installed `room-doc` alias points at
this directory, so the old script name must resolve here too. Remove one
release after the rename."""
import os
import sys

NEW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "room_collab.py")
print("note: skills/room-doc is now skills/room-collab (same commands; run room_collab.py)", file=sys.stderr)
os.execv(sys.executable, [sys.executable, os.path.normpath(NEW), *sys.argv[1:]])
