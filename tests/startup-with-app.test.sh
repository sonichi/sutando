#!/bin/bash
# `--with-app` is one entry point over two opt-in steps. The three properties that
# can actually break it are ORDER, GUARD and BLAST RADIUS — not the flag string.
#
# startup.sh ends in `exec`, orchestrates ~15 services and resolves a workspace,
# so a full execution test would need a fixture larger than the change. Instead
# this EXECUTES the shipped parse loop (extracted from the real file at run time,
# never retyped) and asserts the two structural invariants that make the block
# reachable and non-fatal. Stated plainly so the evidence is not oversold.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
S="$REPO/src/startup.sh"
fail=0
ok()   { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=1; }

# 1. The shipped parse loop, executed — not a copy of it.
parse="$(awk '/^WITH_APP=0$/,/^done$/' "$S")"
[ -n "$parse" ] || { bad "could not extract the parse loop from startup.sh"; exit 1; }

probe() { # args... -> prints resulting WITH_APP
  ( set -- "$@"; eval "$parse"; echo "$WITH_APP" )
}
[ "$(probe)" = "0" ]                        && ok "no args -> WITH_APP=0"            || bad "no args should leave WITH_APP=0"
[ "$(probe --with-app)" = "1" ]             && ok "--with-app -> WITH_APP=1"         || bad "--with-app should set WITH_APP=1"
[ "$(probe --other --with-app)" = "1" ]     && ok "flag found in any position"       || bad "flag must be found in any position"
[ "$(probe --withapp)" = "0" ]              && ok "--withapp (typo) -> 0"            || bad "a near-miss flag must not enable it"
[ "$(probe --with-app=1)" = "0" ]           && ok "--with-app=1 -> 0 (exact match)"  || bad "only the exact token enables it"

# 2. ORDER: the block must precede the final exec. After `exec` the process is
#    replaced, so a block moved below it is dead code that still reads as present.
app_line=$(grep -n 'install-menu-bar-app.sh" --supervise' "$S" | head -1 | cut -d: -f1)
exec_line=$(grep -n '^exec bash "\$REPO/src/agent/start-cli.sh"' "$S" | head -1 | cut -d: -f1)
if [ -n "$app_line" ] && [ -n "$exec_line" ] && [ "$app_line" -lt "$exec_line" ]; then
  ok "installer invoked at line $app_line, before the exec at $exec_line"
else
  bad "installer must be invoked BEFORE the final exec (app=$app_line exec=$exec_line)"
fi

# 3. BLAST RADIUS: `set -e` is on, so an unguarded call would abort the core.
#    The invocation must sit in an if-condition, which suppresses errexit.
if awk -v n="$app_line" 'NR==n' "$S" | grep -q '^\s*if bash '; then
  ok "invocation is in an if-condition — installer failure cannot abort the core"
else
  bad "installer call must be guarded (if/||) or set -e takes the core down with it"
fi

[ "$fail" -eq 0 ] && echo "startup-with-app: PASS" || echo "startup-with-app: FAIL"
exit "$fail"
