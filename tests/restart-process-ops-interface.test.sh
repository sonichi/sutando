#!/usr/bin/env bash
# restart.sh must touch processes ONLY through src/process-ops.sh.
#
# The isolation guarantee is the seam, not the PATH stubs a test happens to
# install: a bare `pkill` in restart.sh runs the host's pkill whatever the fake
# layer says, and the fake's call log stays silent about it. So the shipped
# source is scanned for bare process verbs, with a positive control proving the
# scan can fail.
#
# Run: bash tests/restart-process-ops-interface.test.sh
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OPS="$REPO/src/process-ops.sh"
RS="$REPO/src/restart.sh"
fails=0
ck() { if [ "$2" = "0" ]; then echo "  ok   $1"; else echo "  FAIL $1"; fails=$((fails+1)); fi; }

SB="$(mktemp -d)"; trap 'rm -rf "$SB"' EXIT

# Comment lines are prose about the verbs and quoted strings name them to the
# operator; neither invokes anything. What is left is command position, where a
# verb IS a call — `pkill -f "x"` survives the strip as `pkill -f`.
VERBS='(^|[^_[:alnum:]])(pkill|killall|launchctl|tmux)([[:space:]]|$)|(^|[^_[:alnum:]])kill[[:space:]]+-|xargs[[:space:]]+kill'
scan() {
  grep -vE '^[[:space:]]*#' "$1" | sed -e "s/\"[^\"]*\"//g" -e "s/'[^']*'//g" | grep -E "$VERBS"
}

[ -r "$OPS" ]; ck "src/process-ops.sh exists and is readable" $?

# The interface the owner declared, plus the two inspection calls ownership needs.
for fn in pops_signal pops_alive pops_argv pops_pattern_kill pops_pattern_running \
          pops_name_running pops_launchctl pops_tmux pops_port_listening; do
  grep -q "^$fn()" "$OPS"; ck "process-ops.sh exposes $fn" $?
done

hits="$(scan "$RS")"
[ -z "$hits" ]; ck "the shipped restart.sh contains no bare process verb" $?
[ -n "$hits" ] && printf '%s\n' "$hits" | sed 's/^/       /'

grep -q '^\. "\$REPO/src/process-ops.sh"' "$RS"; ck "restart.sh sources the interface" $?
grep -q 'pops_' "$RS"; ck "restart.sh actually calls through it (scan is not vacuous)" $?

# --- the control: a bare verb must be CAUGHT --------------------------------
cp "$RS" "$SB/perturbed.sh"
printf 'pkill -f "watch-tasks"\n' >> "$SB/perturbed.sh"
[ -n "$(scan "$SB/perturbed.sh")" ]; ck "CONTROL: a re-added bare pkill is caught by the scan" $?
cp "$RS" "$SB/perturbed2.sh"
printf 'kill -9 "$pid"\n' >> "$SB/perturbed2.sh"
[ -n "$(scan "$SB/perturbed2.sh")" ]; ck "CONTROL: a re-added bare kill -9 is caught by the scan" $?

# --- injection: the fake replaces the whole layer ----------------------------
LOG="$SB/ops.log"; : > "$LOG"
out="$(POPS_LOG="$LOG" SUTANDO_PROCESS_OPS="$REPO/tests/fixtures/process-ops-fake.sh" \
       bash -c '. "$1"; pops_pattern_kill "nothing-real"; pops_signal 424242 TERM' _ "$OPS" 2>&1)"
grep -q '^pattern_kill nothing-real$' "$LOG"; ck "SUTANDO_PROCESS_OPS routes calls to the fake" $?
grep -q '^signal 424242 TERM$' "$LOG"; ck "the fake records signals instead of sending them" $?

# An unreadable injection must fail loudly: silently falling through to the real
# implementations would run a test's kills against the host.
out="$(SUTANDO_PROCESS_OPS="$SB/does-not-exist" bash -c '. "$1"' _ "$OPS" 2>&1)"; rc=$?
[ "$rc" -ne 0 ]; ck "an unreadable SUTANDO_PROCESS_OPS refuses rather than falling back" $?
grep -q "unreadable" <<<"$out"; ck "and says which file" $?

bash -n "$OPS"; ck "process-ops.sh parses" $?

echo
[ "$fails" -eq 0 ] && { echo "all ok"; exit 0; } || { echo "$fails FAILED"; exit 1; }
