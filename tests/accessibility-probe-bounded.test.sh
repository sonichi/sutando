#!/usr/bin/env bash
# Drives the REAL accessibility_probe, not a copy: the previous version tested a
# hand-copied perl invocation, so the bound could be defeated while all passed.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s %s\n' "$1" "${2:-}"; fails=$((fails+1)); }

echo "accessibility probe is bounded:"

PROBE="$REPO/src/accessibility_probe.sh"
[ -f "$PROBE" ] && ok "the shared probe exists" \
  || { bad "the shared probe exists" "missing"; echo "  Total: 1 — pass: 0, fail: 1"; exit 1; }

# shellcheck source=../src/accessibility_probe.sh
. "$PROBE"
type accessibility_probe >/dev/null 2>&1 \
  && ok "the real function is what this file exercises" \
  || bad "the real function is what this file exercises" "not defined after sourcing"

run_probe() {
  local s0 rc
  s0=$(date +%s)
  ACCESSIBILITY_PROBE_CMD=(sleep 8)
  ACCESSIBILITY_PROBE_TIMEOUT_S="$1" accessibility_probe; rc=$?
  ELAPSED=$(( $(date +%s) - s0 ))
  return $rc
}

# Valid override HONOURED. Without this control the rows below are equally
# consistent with a helper that ignores the variable and always waits 5s.
run_probe 2; rc=$?
[ "$rc" -eq 124 ] && [ "$ELAPSED" -ge 2 ] && [ "$ELAPSED" -lt 4 ] \
  && ok "a valid bound is honoured (2s -> 124 in ${ELAPSED}s)" \
  || bad "a valid bound is honoured" "rc=$rc elapsed=${ELAPSED}s"

# A bad bound must FAIL CLOSED: perl reads `alarm 0`, and anything coercing to
# 0, as cancel — silently restoring the unbounded call this exists to prevent.
for badv in 0 abc -1 ''; do
  run_probe "$badv"; rc=$?
  if [ "$rc" -eq 124 ] && [ "$ELAPSED" -lt 7 ]; then
    ok "bound '$badv' fails closed to the default (124 in ${ELAPSED}s)"
  else
    bad "bound '$badv' fails closed to the default" "rc=$rc elapsed=${ELAPSED}s — UNBOUNDED"
  fi
done

ACCESSIBILITY_PROBE_CMD=(true);           accessibility_probe; [ $? -eq 0 ] && ok "granted -> 0" || bad "granted -> 0"
ACCESSIBILITY_PROBE_CMD=(sh -c 'exit 7'); accessibility_probe; [ $? -eq 7 ] && ok "a denial code survives (7)" || bad "a denial code survives (7)"
ACCESSIBILITY_PROBE_CMD=(false);          accessibility_probe; [ $? -eq 1 ] && ok "a plain denial is 1, not 124" || bad "a plain denial is 1, not 124"

# The defect existed in two files, so a fix in one is not a fix.
for f in src/startup.sh src/init.sh; do
  grep -q "osascript -e 'tell application \"System Events\" to get name of first process" "$REPO/$f" \
    && bad "$f goes through the shared probe" "raw osascript remains" || ok "$f goes through the shared probe"
  grep -qE '^\s*(local )?source "\$REPO/src/accessibility_probe.sh"' "$REPO/$f" \
    && bad "$f guards the probe source" "unguarded \$REPO source" || ok "$f guards the probe source"
  grep -qE '^\s*accessibility_probe\s*$' "$REPO/$f" \
    && bad "$f keeps the call set -e exempt" "bare call aborts" || ok "$f keeps the call set -e exempt"
done

# Could-not-check must not read as granted: acc_rc=0 with no probe file would
# print "✓ Accessibility" for a run where nothing was probed.
for f in src/startup.sh src/init.sh; do
  grep -qE '^\s*(local )?acc_rc=125' "$REPO/$f" \
    && ok "$f defaults to could-not-check, not granted" \
    || bad "$f defaults to could-not-check, not granted" "acc_rc starts at 0"
done

S="$(mktemp -d)"; mkdir -p "$S/.fake-home"
SUTANDO_REPO="$S" SUTANDO_WORKSPACE="$S/.workspace" SUTANDO_TEST_MODE=1 \
  HOME="$S/.fake-home" CLAUDE_CONFIG_DIR="$S/.fake-home/.claude" \
  bash "$REPO/src/init.sh" --preflight > "$S/out" 2>&1
prc=$?
[ "$prc" -eq 0 ] && [ "$(grep -c 'Preflight' "$S/out")" -eq 1 ] \
  && ok "--preflight exits 0 and emits its summary under a scratch SUTANDO_REPO" \
  || bad "--preflight exits 0 and emits its summary under a scratch SUTANDO_REPO" "rc=$prc"
rm -rf "$S"

total=18
echo "  Total: $total — pass: $((total-fails)), fail: $fails"
[ "$fails" -eq 0 ] || exit 1
