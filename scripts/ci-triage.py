#!/usr/bin/env python3
"""Transitional shim: the tool moved to skills/pr-review/scripts/ (owner decision 2026-09-04)."""
import os
import sys
os.execv(sys.executable, [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "pr-review", "scripts", "ci-triage.py")] + sys.argv[1:])
