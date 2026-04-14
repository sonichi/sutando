#!/bin/bash
# POC: CodeQL #16-23, #28-31, #35-36 — path injection in agent-api.py and dashboard.py
# Tests that path traversal attempts are blocked by existing guards.

API_PORT=7843
DASH_PORT=7844

echo "=== agent-api.py path injection tests ==="

echo "Test 1: normal task lookup"
curl -s "http://localhost:$API_PORT/result/task-123" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()))" 2>/dev/null || echo "(server not running)"

echo "Test 2: path traversal in task ID"
curl -s "http://localhost:$API_PORT/result/../../../etc/passwd" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()))" 2>/dev/null || echo "(server not running)"

echo "Test 3: null byte injection"
curl -s "http://localhost:$API_PORT/result/task%00.txt" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()))" 2>/dev/null || echo "(server not running)"

echo "Test 4: media path traversal"
curl -s "http://localhost:$API_PORT/media/../../etc/passwd" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()))" 2>/dev/null || echo "(server not running)"

echo ""
echo "=== dashboard.py path injection tests ==="

echo "Test 5: normal note GET"
curl -s "http://localhost:$DASH_PORT/notes/test-note" -w " (HTTP %{http_code})" 2>/dev/null || echo "(server not running)"

echo ""
echo "Test 6: path traversal in slug"
curl -s "http://localhost:$DASH_PORT/notes/../../etc/passwd" -w " (HTTP %{http_code})" 2>/dev/null || echo "(server not running)"

echo ""
echo "Test 7: null byte in slug"
curl -s "http://localhost:$DASH_PORT/notes/test%00evil" -w " (HTTP %{http_code})" 2>/dev/null || echo "(server not running)"

echo ""
echo "Expected: All traversal attempts (tests 2-4, 6-7) return 400 or 404"
echo "If any return file contents — BUG CONFIRMED"
