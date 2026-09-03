#!/usr/bin/env bash
# sync-workspace.sh lock: "stale" means the holder process is GONE, never "older
# than 10 minutes". Measured 2026-09-03 (#3824): sync-conflicts-report.py ran
# 25+ min inside the lock, the next starter deleted the "stale" lock at the
# 10-min mark, and three full syncs of one vault ran concurrently. Drives the
# real acquire_lock() extracted from the script, in subshells, so `exit 0` is
# observable as the subshell ending without the acquired marker.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC="$SCRIPT_DIR/../scripts/sync-workspace.sh"
FN="$(awk '/^acquire_lock\(\) \{/,/^}$/' "$SYNC")"
[ -n "$FN" ] || { echo "FAIL: acquire_lock() not found in $SYNC"; exit 1; }
T="$(mktemp -d)"; trap 'rm -rf "$T"; kill "$HOLDER" 2>/dev/null' EXIT
fail=0
attempt() {  # $1 = lock dir; prints "acquired <pid>" or "skipped"
    ( log() { :; }; LOCK_DIR="$1"; eval "$FN"; trap - EXIT INT TERM
      acquire_lock >/dev/null; echo "acquired $(cat "$LOCK_DIR/pid" 2>/dev/null)"; trap - EXIT ) 2>/dev/null
}
check() { local name="$1" want="$2" got="$3"
    case "$got" in *"$want"*) echo "ok   $name";; *) echo "FAIL $name  (wanted '$want', got '${got:-<skipped>}')"; fail=1;; esac; }

# A. live holder, lock 20 min old -> must SKIP (this is the #3824 overtaking case)
sleep 300 & HOLDER=$!
L="$T/live"; mkdir "$L"; echo "$HOLDER" > "$L/pid"; touch -t "$(date -v-20M +%Y%m%d%H%M)" "$L" 2>/dev/null || touch -d '20 minutes ago' "$L"
out="$(attempt "$L")"; [ -z "$out" ] && echo "ok   A. live holder 20 min old -> skipped, lock kept" || { echo "FAIL A. live holder 20 min old was overtaken: $out"; fail=1; }
[ -d "$L" ] && [ "$(cat "$L/pid")" = "$HOLDER" ] && echo "ok   A2. holder's lock untouched" || { echo "FAIL A2. holder's lock was removed"; fail=1; }

# B. dead holder, lock fresh -> must ACQUIRE (a crash must not wedge the sync)
( sleep 0.1 ) & DEAD=$!; wait "$DEAD" 2>/dev/null
L="$T/dead"; mkdir "$L"; echo "$DEAD" > "$L/pid"
out="$(attempt "$L")"; check "B. dead holder (fresh lock) -> acquired" "acquired" "$out"

# C. legacy lock, no pid file, 20 min old -> age rule still applies -> ACQUIRE
L="$T/legacy-old"; mkdir "$L"; touch -t "$(date -v-20M +%Y%m%d%H%M)" "$L" 2>/dev/null || touch -d '20 minutes ago' "$L"
out="$(attempt "$L")"; check "C. legacy pid-less lock, 20 min old -> acquired" "acquired" "$out"

# D. legacy lock, no pid file, fresh -> SKIP (cannot tell, so do not overtake)
L="$T/legacy-fresh"; mkdir "$L"
out="$(attempt "$L")"; [ -z "$out" ] && echo "ok   D. legacy pid-less fresh lock -> skipped" || { echo "FAIL D. fresh legacy lock overtaken: $out"; fail=1; }

# E. a fresh acquire records its own pid
L="$T/new"; out="$(attempt "$L")"; check "E. acquire writes the holder pid" "acquired" "$out"
[ -s "$L/pid" ] && echo "ok   E2. pid file present" || { echo "FAIL E2. no pid file written"; fail=1; }

[ "$fail" = 0 ] && echo "sync-workspace-lock-liveness: all passed" || { echo "sync-workspace-lock-liveness: FAILED"; exit 1; }
