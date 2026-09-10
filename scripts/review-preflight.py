#!/usr/bin/env python3
"""Transitional shim for the tool under skills/review-preflight/scripts/."""
import os
import sys
os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "review-preflight", "scripts", "review-preflight.py")] + sys.argv[1:])
