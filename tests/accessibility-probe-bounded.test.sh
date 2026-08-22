#!/usr/bin/env bash
# The probe must be BOUNDED and must distinguish a timeout from a denial:
# only a denial should send someone to System Settings.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fails=0
ok()  { printf '  ok   %s\n' "$1"; }
bad() { printf '  FAIL %s %s\n' "$1" "${2:-}"; fails=$((fails+1)); }

echo "accessibility probe is bounded:"

[ -f "$REPO/src/accessibility_probe.sh" ] \
  && ok "the shared probe exists" \
  || { bad "the shared probe exists" "src/accessibility_probe.sh missing"; echo "  Total: 1 — pass: 0, fail: 1"; exit 1; }

# The defect existed in two places because the probe was duplicated, so a fix
# in only one of them is not a fix.
raw=0
for f in src/startup.sh src/init.sh; do
  if grep -q "osascript -e 'tell application \"System Events\" to get name of first process" "$REPO/$f"; then
    bad "$f goes through the shared probe" "still calls osascript directly"; raw=1
  fi
done
[ "$raw" -eq 0 ] && ok "both callers go through the shared probe"

for f in src/startup.sh src/init.sh; do
  grep -q 'accessibility_probe' "$REPO/$f" \
    && ok "$f calls accessibility_probe" \
    || bad "$f calls accessibility_probe" "no call found"
done

. "$REPO/src/accessibility_probe.sh"

# A hanging command must be killed at the bound, not waited on forever.
start=$(date +%s)
( perl -e 'my $s=shift; my $p=fork(); if(!$p){exec @ARGV; exit 127;}
           $SIG{ALRM}=sub{kill "KILL",$p; waitpid $p,0; exit 124};
           alarm $s; waitpid $p,0; alarm 0;
           exit($?>>8 ? $?>>8 : ($? ? 1 : 0));' 2 sleep 30 ) >/dev/null 2>&1
rc=$?; el=$(( $(date +%s) - start ))
[ "$rc" -eq 124 ] && [ "$el" -lt 5 ] \
  && ok "a hanging probe is killed at the bound (rc=124 after ${el}s)" \
  || bad "a hanging probe is killed at the bound" "rc=$rc elapsed=${el}s"

# Timeout and denial must not collapse: 142 means the ALRM handler was lost to
# exec, and a caller cannot tell that from a genuine denial.
probe_with() {
  perl -e 'my $s=shift; my $p=fork(); if(!$p){exec @ARGV; exit 127;}
           $SIG{ALRM}=sub{kill "KILL",$p; waitpid $p,0; exit 124};
           alarm $s; waitpid $p,0; alarm 0;
           exit($?>>8 ? $?>>8 : ($? ? 1 : 0));' "$@" >/dev/null 2>&1
}
probe_with 3 true;         [ $? -eq 0 ] && ok "success returns 0"        || bad "success returns 0"
probe_with 3 sh -c 'exit 7'; [ $? -eq 7 ] && ok "a denial code survives (7)" || bad "a denial code survives (7)"
probe_with 3 false;        [ $? -eq 1 ] && ok "a plain failure is 1, not 124" || bad "a plain failure is 1, not 124"

# 5s is a guess and a slow host is not a denial, so the bound is overridable.
grep -q 'ACCESSIBILITY_PROBE_TIMEOUT_S' "$REPO/src/accessibility_probe.sh" \
  && ok "the bound is overridable" || bad "the bound is overridable"

# Both callers run under `set -e`, where a bare non-zero call aborts the script
# before its summary — measured: init.sh --preflight emitted 0 summary lines.
for f in src/startup.sh src/init.sh; do
  if grep -qE '^\s*accessibility_probe\s*$' "$REPO/$f"; then
    bad "$f calls the probe set -e safely" "bare call aborts under set -e"
  else
    ok "$f calls the probe set -e safely"
  fi
done

# And prove the shape, not just its absence.
if bash -c 'set -e; f(){ return 124; }; rc=0; f || rc=$?; [ "$rc" -eq 124 ]'; then
  ok "the || rc=\$? form survives set -e and keeps the code"
else
  bad "the || rc=\$? form survives set -e and keeps the code"
fi

# $REPO is overridable — tests point it at a scratch dir with no src/. An
# unguarded `source "$REPO/..."` under `set -e` aborts before any output.
for f in src/startup.sh src/init.sh; do
  if grep -qE '^\s*(local )?source "\$REPO/src/accessibility_probe.sh"' "$REPO/$f"; then
    bad "$f guards the probe source" "sources \$REPO unguarded; a scratch repo aborts it"
  else
    ok "$f guards the probe source"
  fi
done

S="$(mktemp -d)"; mkdir -p "$S/.fake-home"
SUTANDO_REPO="$S" SUTANDO_WORKSPACE="$S/.workspace" SUTANDO_TEST_MODE=1 \
  HOME="$S/.fake-home" CLAUDE_CONFIG_DIR="$S/.fake-home/.claude" \
  bash "$REPO/src/init.sh" --preflight > "$S/out" 2>&1
prc=$?
if [ "$prc" -eq 0 ] && [ "$(grep -c 'Preflight' "$S/out")" -eq 1 ]; then
  ok "--preflight exits 0 and emits its summary with SUTANDO_REPO on a scratch dir"
else
  bad "--preflight exits 0 and emits its summary with SUTANDO_REPO on a scratch dir" \
      "rc=$prc summary_lines=$(grep -c 'Preflight' "$S/out")"
fi
rm -rf "$S"

total=15
echo "  Total: $total — pass: $((total-fails)), fail: $fails"
[ "$fails" -eq 0 ] || exit 1
