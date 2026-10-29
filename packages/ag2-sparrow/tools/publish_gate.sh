#!/bin/bash
# The test gate a release must pass: every package suite plus the src-drift guard.
# The tag publish and the PR job both run this file, on the same interpreter.
set -euo pipefail
cd "$(dirname "$0")/../../.."
for t in packages/ag2-sparrow/tests/test_*.py; do python3 "$t"; done
python3 packages/ag2-sparrow/tools/test_no_drift.py
