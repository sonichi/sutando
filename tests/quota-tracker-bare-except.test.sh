#!/bin/bash
# Tests for the except ImportError narrowing in read-quota.py (issue #1062).
#
# Verifies three scenarios:
# 1. except ImportError: is present; no bare except Exception: remains.
# 2. status_read_path raises RuntimeError (non-import failure): script propagates
#    the error as a traceback — NEW behavior the fix enables.
# 3. workspace_default raises ImportError on import: graceful "not found" + exit 1.
# 4. Happy path: valid quota-state.json -> prints quota (skipped if proxy not running).
#
# Tests 2 and 3 work by copying read-quota.py into a fake repo tree so that
# _REPO_ROOT resolves to a temp dir whose src/ contains the fake workspace_default.
# PYTHONPATH alone is not enough because read-quota.py does sys.path.insert(0, ...)
# which always wins over PYTHONPATH.

set -u

PASS=0
FAIL=0
SCRIPT="$(cd "$(dirname "$0")/.." && pwd)/skills/quota-tracker/scripts/read-quota.py"

ok()   { echo "  PASS: $1"; ((PASS++)); }
fail() { echo "  FAIL: $1"; ((FAIL++)); }

echo "=== quota-tracker bare-except narrowing tests (issue #1062) ==="

# 1. Static checks: clause is ImportError, not Exception.
if grep -q "except ImportError:" "$SCRIPT"; then
    ok "except clause is ImportError (not bare Exception)"
else
    fail "except clause is NOT ImportError -- fix not applied"
fi

if grep -q "except Exception:" "$SCRIPT"; then
    fail "bare 'except Exception:' still present -- fix incomplete"
else
    ok "no bare 'except Exception:' remains"
fi

# Helper: create a fake repo tree with a copy of the script + a custom workspace_default.
# $1 = temp dir, $2 = content to write into src/workspace_default.py
setup_fake_repo() {
    local tmpdir="$1" module_content="$2"
    mkdir -p "$tmpdir/skills/quota-tracker/scripts" "$tmpdir/src"
    cp "$SCRIPT" "$tmpdir/skills/quota-tracker/scripts/read-quota.py"
    printf '%s\n' "$module_content" > "$tmpdir/src/workspace_default.py"
}

# 2. RuntimeError from status_read_path propagates (not silenced by ImportError guard).
TMP2="$(mktemp -d)"
setup_fake_repo "$TMP2" "$(printf '%s\ndef status_read_path(name):\n    raise RuntimeError("synthetic: workspace config corrupted")')"

runtime_out="$(python3 "$TMP2/skills/quota-tracker/scripts/read-quota.py" 2>&1)"
runtime_code=$?

if echo "$runtime_out" | grep -q "RuntimeError"; then
    ok "RuntimeError from status_read_path propagates (not silenced)"
else
    fail "RuntimeError from status_read_path was silenced -- fix may not have landed"
    echo "    output: $runtime_out"
fi

if [ "$runtime_code" -ne 0 ]; then
    ok "script exits non-zero on RuntimeError"
else
    fail "script exited 0 on RuntimeError -- should have been non-zero"
fi
rm -rf "$TMP2"

# 3. ImportError from workspace_default -> graceful "not found" + exits 1.
TMP3="$(mktemp -d)"
setup_fake_repo "$TMP3" 'raise ImportError("synthetic: missing transitive dep")'

import_out="$(python3 "$TMP3/skills/quota-tracker/scripts/read-quota.py" 2>&1)"
import_code=$?

if echo "$import_out" | grep -q "No quota-state.json found"; then
    ok "ImportError from workspace_default -> graceful not-found message"
else
    fail "ImportError from workspace_default did not produce expected message"
    echo "    output: $import_out"
fi

if [ "$import_code" -ne 0 ]; then
    ok "script exits non-zero on ImportError"
else
    fail "script exited 0 on ImportError"
fi
rm -rf "$TMP3"

# 4. Happy path -- only run if quota-state.json symlink resolves.
QUOTA_CANONICAL=~/.sutando/workspace/state/quota-state.json
if [ -f "$QUOTA_CANONICAL" ]; then
    happy_out="$(python3 "$SCRIPT" 2>&1)"
    happy_code=$?
    if [ "$happy_code" -eq 0 ] && echo "$happy_out" | grep -q "5h window"; then
        ok "happy path: quota reads correctly with live proxy"
    else
        fail "happy path: unexpected output or exit code $happy_code"
        echo "    output: $happy_out"
    fi
else
    echo "  SKIP: happy path (quota-state.json not at canonical path -- proxy not running)"
fi

echo
TOTAL=$((PASS + FAIL))
echo "Results: $PASS/$TOTAL passed"
if [ "$FAIL" -gt 0 ]; then
    echo "FAILED"
    exit 1
else
    echo "ALL TESTS PASSED"
    exit 0
fi
