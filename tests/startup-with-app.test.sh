#!/bin/bash
# `--with-app` must GUARD the installer, run BEFORE the final exec, and never
# take the core down. All three are executed here against the shipped file.
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
app_line=$(grep -n 'install-menu-bar-app.sh" --launch' "$S" | head -1 | cut -d: -f1)

# --with-app means "run the app". Installing a login-persistent launchd job is a
# separate decision, and it is what bakes an install-time path into a LaunchAgent.
grep -q 'install-menu-bar-app.sh" --supervise' "$S" \
  && bad "--with-app must not install the launchd supervisor" \
  || ok "--with-app launches without installing the launchd supervisor"

# The usage header is the only description most readers see, and it drifted from
# the call it documents in exactly this way once already.
grep -qiE '^#.*--with-app.*supervis' "$S" \
  && bad "the --with-app usage comment still promises supervision" \
  || ok "usage comment matches what --with-app actually does"
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

# 4. DISPATCH, EXECUTED — the structural checks above all survived an `if true`
# mutation. Anchored on the installer CALL, so mutating the guard breaks behavior.
dispatch="$(python3 - "$S" <<'EXTRACT'
import re, sys
lines = open(sys.argv[1]).read().splitlines()
call = next(i for i, l in enumerate(lines) if "install-menu-bar-app.sh" in l and l.strip().startswith("if bash"))
start = max(i for i in range(call) if re.match(r"^if .*; then$", lines[i]))
depth, end = 0, None
for i in range(start, len(lines)):
    s = lines[i].strip()
    if re.match(r"^if .*; then$", s): depth += 1
    elif s == "fi":
        depth -= 1
        if depth == 0: end = i; break
print("\n".join(lines[start:end + 1]))
EXTRACT
)"
[ -n "$dispatch" ] || bad "could not extract the app-dispatch block from startup.sh"

run_dispatch() { # $1 = WITH_APP value; echoes whatever the stub installer recorded
  tmp="$(mktemp -d)"
  mkdir -p "$tmp/scripts"
  printf '#!/bin/bash\necho "CALLED $*" >> "%s/calls.log"\n' "$tmp" > "$tmp/scripts/install-menu-bar-app.sh"
  chmod +x "$tmp/scripts/install-menu-bar-app.sh"
  : > "$tmp/calls.log"
  ( set -e; REPO="$tmp"; WITH_APP="$1"; eval "$dispatch" ) >/dev/null 2>&1
  cat "$tmp/calls.log"
  rm -rf "$tmp"
}

off="$(run_dispatch 0)"
if [ -z "$off" ]; then ok "WITH_APP=0 -> installer called ZERO times"
else bad "WITH_APP=0 must not call the installer, got: $off"; fi

on="$(run_dispatch 1)"
if [ "$(printf '%s' "$on" | grep -c CALLED)" = "1" ]; then ok "WITH_APP=1 -> installer called exactly once"
else bad "WITH_APP=1 must call the installer exactly once, got: $on"; fi

if printf '%s' "$on" | grep -q -- '--launch'; then ok "the single call passes --launch"
else bad "the call must pass --launch, got: $on"; fi

if printf '%s' "$on" | grep -q -- '--supervise'; then bad "the call must NOT pass --supervise, got: $on"
else ok "the call does not pass --supervise"; fi

[ "$fail" -eq 0 ] && echo "startup-with-app: PASS" || echo "startup-with-app: FAIL"
exit "$fail"
