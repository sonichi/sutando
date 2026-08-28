#!/usr/bin/env bash
# Commit must FAIL CLOSED: no sentinel and rc!=0 after any failed write, no
# mutation outside the physically-resolved dest root, and a correct union must
# pass mandatory verification. Controls mirror the #3418 review's injections.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
MIGRATE="$REPO/scripts/sutando-migrate.sh"

TMP="$(mktemp -d -t sutando-mig-failclosed.XXXXXX)"
trap 'chmod -R u+w "$TMP" 2>/dev/null; rm -rf "$TMP"' EXIT

pass=0
fail=0
check() {
    local description="$1"
    shift
    if "$@" >/dev/null 2>&1; then
        echo "OK: $description"
        pass=$((pass + 1))
    else
        echo "FAIL: $description"
        fail=$((fail + 1))
    fi
}

# One isolated fixture per control: a single source A and a dest.
new_case() {  # $1 = name; sets CASE_A, CASE_DEST
    CASE_A="$TMP/$1/source-a"
    CASE_DEST="$TMP/$1/dest"
    mkdir -p "$CASE_A" "$CASE_DEST/state"
}
RUN() {  # rc surfaced to caller; output to per-case log
    SUTANDO_MIGRATE_SRC_A="$CASE_A" \
    SUTANDO_MIGRATE_SRC_B="$TMP/absent-b" \
    SUTANDO_MIGRATE_SRC_C="$TMP/absent-c" \
    SUTANDO_MIGRATE_DEST="$CASE_DEST" \
        bash "$MIGRATE" "$@"
}
no_sentinel() { [ -z "$(ls "$CASE_DEST/state/.migrated-from-A-"* 2>/dev/null)" ]; }

echo "1. COPY_FAIL: an unwritable dest dir must fail the commit, not certify it"
new_case copyfail
mkdir -p "$CASE_A/hosts/Test-Host" "$CASE_DEST/hosts/Test-Host"
echo "payload" > "$CASE_A/hosts/Test-Host/crons.json"
chmod 555 "$CASE_DEST/hosts/Test-Host"
rc=0; RUN --commit > "$TMP/copyfail.log" 2>&1 || rc=$?
chmod 755 "$CASE_DEST/hosts/Test-Host"
check "commit exits non-zero" [ "$rc" -ne 0 ]
check "the file did NOT land" [ ! -f "$CASE_DEST/hosts/Test-Host/crons.json" ]
check "NO sentinel was written" no_sentinel
check "the failure is named in output" grep -q "WRITE-FAILED" "$TMP/copyfail.log"

echo "2. MODE_PROBE_FAIL: an unprobeable existing dest dir refuses the copy"
new_case probefail
mkdir -p "$CASE_A/hosts/probefail-host" "$CASE_DEST/hosts/probefail-host"
echo "payload" > "$CASE_A/hosts/probefail-host/crons.json"
chmod 700 "$CASE_A/hosts/probefail-host"   # differs from dest so the probe path runs
BIN="$TMP/bin-probefail"; mkdir -p "$BIN"
cat > "$BIN/stat" <<'SH'
#!/bin/bash
for a in "$@"; do case "$a" in *probefail-host*) exit 1;; esac; done
exec /usr/bin/stat "$@"
SH
chmod +x "$BIN/stat"
rc=0; PATH="$BIN:$PATH" RUN --commit > "$TMP/probefail.log" 2>&1 || rc=$?
check "commit exits non-zero" [ "$rc" -ne 0 ]
check "the file did NOT land under an unverified mode" [ ! -f "$CASE_DEST/hosts/probefail-host/crons.json" ]
check "NO sentinel was written" no_sentinel

echo "3. CHMOD_FAIL: a mode that cannot be applied refuses the copy"
new_case chmodfail
mkdir -p "$CASE_A/hosts/chmodfail-host" "$CASE_DEST/hosts/chmodfail-host"
echo "payload" > "$CASE_A/hosts/chmodfail-host/crons.json"
chmod 700 "$CASE_A/hosts/chmodfail-host"
chmod 755 "$CASE_DEST/hosts/chmodfail-host"
BIN="$TMP/bin-chmodfail"; mkdir -p "$BIN"
cat > "$BIN/chmod" <<'SH'
#!/bin/bash
for a in "$@"; do case "$a" in *chmodfail-host*) exit 1;; esac; done
exec /bin/chmod "$@"
SH
chmod +x "$BIN/chmod"
rc=0; PATH="$BIN:$PATH" RUN --commit > "$TMP/chmodfail.log" 2>&1 || rc=$?
check "commit exits non-zero" [ "$rc" -ne 0 ]
check "the file did NOT land under the widened mode" [ ! -f "$CASE_DEST/hosts/chmodfail-host/crons.json" ]
check "NO sentinel was written" no_sentinel

echo "4. SYMLINK_BOUND: a dest child symlink must refuse BEFORE any outside mutation"
new_case symlink
OUTSIDE="$TMP/outside-the-boundary"
mkdir -p "$OUTSIDE"; chmod 755 "$OUTSIDE"
mkdir -p "$CASE_A/hosts/Test-Host"
echo "payload" > "$CASE_A/hosts/Test-Host/crons.json"
ln -s "$OUTSIDE" "$CASE_DEST/hosts"
rc=0; RUN --commit > "$TMP/symlink.log" 2>&1 || rc=$?
check "commit exits non-zero" [ "$rc" -ne 0 ]
check "nothing was created outside the dest root" [ -z "$(ls -A "$OUTSIDE")" ]
check "the outside dir's mode is untouched" [ "$(/usr/bin/stat -f %Lp "$OUTSIDE" 2>/dev/null || /usr/bin/stat -c %a "$OUTSIDE")" = "755" ]
check "NO sentinel was written" no_sentinel
check "the escape is named in output" grep -q "escapes the dest root" "$TMP/symlink.log"

echo "5. UNION_VERIFY: a correct union passes mandatory phase-three verification"
new_case union
mkdir -p "$CASE_A/state"
# The SOURCE is the newer side (the reviewer's shape): its scalars win, and
# the union stamps the dest with its mtime — which is what arms the
# scalar-winner check in union_contains.
printf '{"users": ["alice", "bob"], "schemaVersion": 2}\n' > "$CASE_A/state/slack-allowed-recipients.json"
printf '{"users": ["bob", "carol"], "schemaVersion": 1}\n' > "$CASE_DEST/state/slack-allowed-recipients.json"
touch -t 202606011300 "$CASE_A/state/slack-allowed-recipients.json"
touch -t 202606011200 "$CASE_DEST/state/slack-allowed-recipients.json"
rc=0; RUN --commit > "$TMP/union-commit.log" 2>&1 || rc=$?
check "divergent union commit succeeds" [ "$rc" -eq 0 ]
UNION_DST="$CASE_DEST/state/slack-allowed-recipients.json"
check "the union merged both arrays and kept the newer scalar" \
    python3 -c "
import json
d = json.load(open('$UNION_DST'))
import sys; sys.exit(0 if sorted(d['users']) == ['alice','bob','carol'] and d['schemaVersion'] == 2 else 1)
"
rc=0; RUN --verify > "$TMP/union-verify.log" 2>&1 || rc=$?
check "verify passes the union result (rc 0)" [ "$rc" -eq 0 ]
check "verify reports zero mismatches" grep -q "mismatch=0" "$TMP/union-verify.log"

# Content-only corruptions from here on: every edit preserves the dest mtime,
# and the pristine bytes are restored (with mtime) between controls.
GOOD_UNION="$(cat "$UNION_DST")"
corrupt() {  # $1 = python expression mutating dict d
    python3 - "$UNION_DST" "$1" <<'PY'
import json, os, sys
p, expr = sys.argv[1], sys.argv[2]
mt = os.path.getmtime(p)
d = json.load(open(p))
exec(expr)
json.dump(d, open(p, "w"), indent=2, sort_keys=True)
os.utime(p, (mt, mt))
PY
}
restore() {
    python3 - "$UNION_DST" <<PY
import os, sys
p = sys.argv[1]
mt = os.path.getmtime(p)
open(p, "w").write("""$GOOD_UNION""")
os.utime(p, (mt, mt))
PY
}

corrupt "d['users'] = ['carol']"
rc=0; RUN --verify > "$TMP/union-verify-neg.log" 2>&1 || rc=$?
check "a union that DROPPED an element still fails verify (control)" [ "$rc" -ne 0 ]
restore
rc=0; RUN --verify > "$TMP/union-verify-restore.log" 2>&1 || rc=$?
check "restored union verifies again (the controls are not wedged)" [ "$rc" -eq 0 ]

# The reviewer's scalar repro: arrays fully intact, only the winning scalar
# altered to a value no input carried.
corrupt "d['schemaVersion'] = 999"
rc=0; RUN --verify > "$TMP/union-verify-scalar.log" 2>&1 || rc=$?
check "a corrupted WINNING SCALAR fails verify (arrays all intact)" [ "$rc" -ne 0 ]

echo ""
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
