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

# Portable octal file mode. `stat -f %Lp` is the BSD/macOS spelling; on GNU
# coreutils `-f` is --file-system, which EXITS 0 printing filesystem info — so
# a `stat -f ... || stat -c ...` chain never reaches its fallback on Linux and
# compares the wrong command's output. Ask python; it means the same on both.
_mode() {
    python3 -c 'import os,stat,sys; print(oct(stat.S_IMODE(os.stat(sys.argv[1]).st_mode))[2:])' "$1"
}

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
check "the outside dir's mode is untouched" [ "$(_mode "$OUTSIDE")" = "755" ]
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

echo "6. UNION_VERIFY dest-winner: the pre-union dest's scalars verify via the manifest"
# The reviewer's paired control: when the DEST was newer, ITS scalars won and
# no source's mtime can vouch for them — only the commit-time manifest can.
new_case destwin
mkdir -p "$CASE_A/state"
printf '{"users": ["alice"], "schemaVersion": 1}\n' > "$CASE_A/state/slack-allowed-recipients.json"
printf '{"users": ["bob"], "schemaVersion": 2}\n' > "$CASE_DEST/state/slack-allowed-recipients.json"
touch -t 202606011200 "$CASE_A/state/slack-allowed-recipients.json"
touch -t 202606011300 "$CASE_DEST/state/slack-allowed-recipients.json"
rc=0; RUN --commit > "$TMP/destwin-commit.log" 2>&1 || rc=$?
check "dest-newer union commit succeeds" [ "$rc" -eq 0 ]
DW_DST="$CASE_DEST/state/slack-allowed-recipients.json"
check "the dest's scalar won and both arrays merged" \
    python3 -c "
import json
d = json.load(open('$DW_DST'))
import sys; sys.exit(0 if sorted(d['users']) == ['alice','bob'] and d['schemaVersion'] == 2 else 1)
"
check "the commit recorded a union-scalars manifest" \
    bash -c "ls '$CASE_DEST/state/.migration-union-scalars-'*.json >/dev/null 2>&1"
rc=0; RUN --verify > "$TMP/destwin-verify.log" 2>&1 || rc=$?
check "pristine dest-winner union passes verify" [ "$rc" -eq 0 ]
python3 - "$DW_DST" <<'PY'
import json, os, sys
p = sys.argv[1]
mt = os.path.getmtime(p)
d = json.load(open(p))
d["schemaVersion"] = 999
json.dump(d, open(p, "w"), indent=2, sort_keys=True)
os.utime(p, (mt, mt))
PY
rc=0; RUN --verify > "$TMP/destwin-verify-corrupt.log" 2>&1 || rc=$?
check "a corrupted DEST-WINNER scalar fails verify (the reviewer's paired control)" [ "$rc" -ne 0 ]

echo "7. MANIFEST AUTHORITY: a damaged manifest fails verify, never degrades to mtime"
MANIFEST="$(ls "$CASE_DEST/state/.migration-union-scalars-"*.json | head -1)"
GOOD_MANIFEST="$(cat "$MANIFEST")"
# 7a. malformed manifest + corrupted dest-winner scalar -> must FAIL
python3 - "$DW_DST" <<'PY'
import json, os, sys
p = sys.argv[1]
mt = os.path.getmtime(p)
d = json.load(open(p))
d["schemaVersion"] = 999
json.dump(d, open(p, "w"), indent=2, sort_keys=True)
os.utime(p, (mt, mt))
PY
printf 'not json{' > "$MANIFEST"
rc=0; RUN --verify > "$TMP/manifest-malformed.log" 2>&1 || rc=$?
check "malformed manifest + corrupt scalar FAILS verify (no mtime degrade)" [ "$rc" -ne 0 ]
# 7b. entry deleted from an otherwise-valid manifest -> must FAIL
printf '{}' > "$MANIFEST"
rc=0; RUN --verify > "$TMP/manifest-missing-entry.log" 2>&1 || rc=$?
check "missing rel entry in a present manifest FAILS verify" [ "$rc" -ne 0 ]
# 7c. restore manifest, dest still corrupt -> still fails (via the entry)
printf '%s' "$GOOD_MANIFEST" > "$MANIFEST"
rc=0; RUN --verify > "$TMP/manifest-restored-corrupt.log" 2>&1 || rc=$?
check "restored manifest still catches the corrupt scalar (control)" [ "$rc" -ne 0 ]
# 7d. recorder refuses an invalid existing manifest instead of replacing it
new_case recorder-guard
mkdir -p "$CASE_A/state"
printf '{"users": ["x"], "schemaVersion": 1}\n' > "$CASE_A/state/slack-allowed-recipients.json"
printf '{"users": ["y"], "schemaVersion": 2}\n' > "$CASE_DEST/state/slack-allowed-recipients.json"
touch -t 202606011300 "$CASE_A/state/slack-allowed-recipients.json"
touch -t 202606011200 "$CASE_DEST/state/slack-allowed-recipients.json"
# pre-plant an invalid manifest for EVERY possible backup id? The id is minted
# at run time — plant via the same glob shape the recorder writes and verify
# reads... the recorder writes a NEW id, so instead assert on the direct
# helper: drive record_union_scalars against an invalid manifest file.
INVALID="$TMP/invalid-manifest.json"
printf 'not json{' > "$INVALID"
REC_FN="$TMP/record_fn.sh"
python3 - "$MIGRATE" "$REC_FN" "$REPO" <<'PYX'
import sys
src, out, repo = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(src).read()
i = s.index("record_union_scalars() {")
j = s.index("\n}\n", i) + 3
open(out, "w").write(
    f'SCRIPT_DIR="{repo}/scripts"\nREPO_DIR="{repo}"\n'
    f'. "{repo}/scripts/python-binary.sh"\n\n' + s[i:j])
PYX
rc=0
( . "$REC_FN"; record_union_scalars "$CASE_A/state/slack-allowed-recipients.json" "state/x.json" "$INVALID" ) || rc=$?
check "recorder REFUSES an invalid existing manifest (rc != 0)" [ "$rc" -ne 0 ]
check "...and did not replace it with {}" bash -c "grep -q 'not json{' '$INVALID'"

echo "8. MANIFEST MODE: the manifest is never wider than the union it describes"
# keweichen on #3418: the writer used a plain open(), so under umask 0022 a
# private (0600) union file's non-array state was duplicated into a 0644
# manifest — the same disclosure, one file over.
new_case modes
mkdir -p "$CASE_A/state"
printf '{"users": ["alice"], "schemaVersion": 1}\n' > "$CASE_A/state/slack-allowed-recipients.json"
printf '{"users": ["bob"], "schemaVersion": 2}\n' > "$CASE_DEST/state/slack-allowed-recipients.json"
chmod 600 "$CASE_A/state/slack-allowed-recipients.json"
chmod 600 "$CASE_DEST/state/slack-allowed-recipients.json"
rc=0; RUN --commit > "$TMP/modes-commit.log" 2>&1 || rc=$?
check "private-input union commit succeeds" [ "$rc" -eq 0 ]
MODE_DST="$CASE_DEST/state/slack-allowed-recipients.json"
MODE_MANIFEST="$(ls "$CASE_DEST/state/.migration-union-scalars-"*.json | head -1)"
check "the union file is still private" [ "$(_mode "$MODE_DST")" = "600" ]
check "the manifest is no wider than the union it describes" [ "$(_mode "$MODE_MANIFEST")" = "600" ]
check "the manifest carries the union's private scalar (so its mode matters)" \
    bash -c "grep -q schemaVersion '$MODE_MANIFEST'"
check "the commit recorded the expected union mode" \
    bash -c "grep -q '__union_modes__' '$MODE_MANIFEST'"
rc=0; RUN --verify > "$TMP/modes-verify.log" 2>&1 || rc=$?
check "a pristine private union passes verify" [ "$rc" -eq 0 ]
# The recorded mode is load-bearing: widening the dest must FAIL verification
# even though every scalar and array still matches.
chmod 644 "$MODE_DST"
rc=0; RUN --verify > "$TMP/modes-verify-widened.log" 2>&1 || rc=$?
check "a WIDENED union fails verify though its content is untouched" [ "$rc" -ne 0 ]
chmod 600 "$MODE_DST"

echo ""
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
