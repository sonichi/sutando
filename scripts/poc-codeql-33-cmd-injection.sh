#!/bin/bash
# POC: CodeQL #33 — command-line injection in screen-capture-server.py
# The display param flows unsanitized into subprocess.run args.
# While subprocess.run with list args prevents shell injection,
# an attacker can inject arbitrary screencapture flags.

PORT=7845

echo "=== Test 1: normal display param ==="
curl -s "http://localhost:$PORT/capture?display=1" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()))"

echo ""
echo "=== Test 2: malicious display param with path traversal in filename ==="
# This injects into the -D flag and the filename suffix
curl -s "http://localhost:$PORT/capture?display=1%20-t%20pdf" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()))"

echo ""
echo "=== Test 3: display param with non-numeric value ==="
curl -s "http://localhost:$PORT/capture?display=../../etc/passwd" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()))"

echo ""
echo "Before fix: tests 2/3 pass malicious input to screencapture command"
echo "After fix: tests 2/3 silently ignore invalid display param (isdigit check)"
