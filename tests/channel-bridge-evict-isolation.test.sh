#!/bin/bash
# CR #2068: evict_own_bridge must kill only THIS checkout's bare bridge, never an
# identically-named bridge from another checkout on the same host.
#
# Two fake checkouts A and B each run `python3 src/<channel>-bridge.py` (relative,
# cwd = their own repo). Evicting for checkout A must kill A's bridge and leave
# B's alive. Also covers the absolute-path launch. Run: bash <this file> (exit 0/1)
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HELPER="$REPO_ROOT/src/launchd/evict-own-bridge.sh"
FAILED=0
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "SKIP: python3 not found"; exit 0; }

# shellcheck source=/dev/null
. "$HELPER"

TMP="$(mktemp -d 2>/dev/null || mktemp -d -t evict)"
cleanup() { pkill -P $$ 2>/dev/null || true; rm -rf "$TMP" 2>/dev/null || true; }
trap cleanup EXIT

assert_dead() {  # name  pid  — passes iff the process is gone (evicted)
  if kill -0 "$2" 2>/dev/null; then echo "  FAIL $1 (still alive)"; FAILED=1; else echo "  ok   $1"; fi
}
assert_alive() { # name  pid  — passes iff the process survived
  if kill -0 "$2" 2>/dev/null; then echo "  ok   $1"; else echo "  FAIL $1 (was killed)"; FAILED=1; fi
}

# Build two fake checkouts with an identical relative bridge path. Resolve to
# PHYSICAL paths (mktemp on macOS lives under a /tmp -> /private/tmp symlink).
mk_checkout() {
  mkdir -p "$1/src"
  printf 'import time,sys\ntime.sleep(120)\n' > "$1/src/slack-bridge.py"
}
mk_checkout "$TMP/checkoutA"; mk_checkout "$TMP/checkoutB"
A="$(cd "$TMP/checkoutA" && pwd -P)"; B="$(cd "$TMP/checkoutB" && pwd -P)"

# --- relative launch (as startup.sh does): cwd = the repo -------------------
( cd "$A" && exec "$PY" src/slack-bridge.py ) & PID_A=$!
( cd "$B" && exec "$PY" src/slack-bridge.py ) & PID_B=$!
sleep 0.6  # let them start + settle their cwd

assert_alive "checkout A's bridge started" "$PID_A"
assert_alive "checkout B's bridge started" "$PID_B"

evict_own_bridge slack "$A"
sleep 0.4
assert_dead  "checkout A's bridge (relative) was evicted" "$PID_A"
assert_alive "checkout B's bridge (other checkout) SURVIVED" "$PID_B"

kill "$PID_B" 2>/dev/null || true

# --- absolute launch: cmd path carries the repo ----------------------------
( exec "$PY" "$A/src/slack-bridge.py" ) & PID_A2=$!
( exec "$PY" "$B/src/slack-bridge.py" ) & PID_B2=$!
sleep 0.6
evict_own_bridge slack "$A"
sleep 0.4
assert_dead  "checkout A's bridge (absolute path) was evicted" "$PID_A2"
assert_alive "checkout B's bridge (absolute, other checkout) SURVIVED" "$PID_B2"
kill "$PID_A2" "$PID_B2" 2>/dev/null || true

# --- gateway bridge: same isolation must hold for the non-"channel" bridge ----
# The launchd gateway wrapper used a bare `pkill -f 'remote-gateway-bridge\.py$'`,
# which matched any checkout's gateway bridge on the host.
mk_gw() { mkdir -p "$1/src"; printf 'import time\ntime.sleep(120)\n' > "$1/src/remote-gateway-bridge.py"; }
mk_gw "$A"; mk_gw "$B"
( cd "$A" && exec "$PY" src/remote-gateway-bridge.py ) & PID_GA=$!
( cd "$B" && exec "$PY" src/remote-gateway-bridge.py ) & PID_GB=$!
sleep 0.6
assert_alive "gateway A started" "$PID_GA"
assert_alive "gateway B started" "$PID_GB"

evict_own_bridge remote-gateway "$A"
sleep 0.4
assert_dead  "gateway A (this checkout) was evicted" "$PID_GA"
assert_alive "gateway B (other checkout) SURVIVED" "$PID_GB"
kill "$PID_GA" "$PID_GB" 2>/dev/null || true

# --- same checkout, DIFFERENT gateway instance: prod must not evict dev --------
# One script path serves every instance, so checkout scope alone cannot separate
# them; identity has to come from GATEWAY_INSTANCE.
( cd "$A" && exec "$PY" src/remote-gateway-bridge.py ) & PID_PROD=$!
( cd "$A" && GATEWAY_INSTANCE=dev exec "$PY" src/remote-gateway-bridge.py ) & PID_DEV=$!
sleep 0.8
assert_alive "prod gateway started" "$PID_PROD"
assert_alive "dev gateway started" "$PID_DEV"

evict_own_bridge remote-gateway "$A" GATEWAY_INSTANCE ""
sleep 0.4
assert_dead  "prod gateway (matching instance) was evicted" "$PID_PROD"
assert_alive "dev gateway (same checkout, other instance) SURVIVED" "$PID_DEV"
kill "$PID_PROD" "$PID_DEV" 2>/dev/null || true

# --- _pid_env contract: "unset" and "unreadable" must NOT be the same answer ----
# The primary gateway evicts with instance-value "", so if an unreadable env also
# returned "" it would compare equal and kill an instance it could not identify.
( sleep 60 ) & PID_LIVE=$!
sleep 0.3
if v="$(_pid_env "$PID_LIVE" GATEWAY_INSTANCE)"; then
  [ -z "$v" ] && echo "  ok   _pid_env: live pid, var unset -> rc=0 and empty (readable)" \
              || { echo "  FAIL _pid_env returned '$v' for an unset var"; FAILED=1; }
else
  echo "  FAIL _pid_env said indeterminate for a readable live pid"; FAILED=1
fi
if _pid_env 999999 GATEWAY_INSTANCE >/dev/null 2>&1; then
  echo "  FAIL _pid_env claimed to read a nonexistent pid (would kill on '' match)"; FAILED=1
else
  echo "  ok   _pid_env: unreadable pid -> rc=1 (indeterminate, never kills)"
fi
kill "$PID_LIVE" 2>/dev/null || true

# --- LIVE process whose env cannot be read: the real restricted-introspection case
# The nonexistent-pid case above is a proxy. This is the production shape: the
# process exists and is ours by checkout, but introspection fails, so identity is
# unknown and it must survive. (Repro technique from bassilkhilo-ag2 on #2068.)
mk_gw "$A"
( cd "$A" && GATEWAY_INSTANCE=dev exec "$PY" src/remote-gateway-bridge.py ) & PID_BLIND=$!
sleep 0.6
assert_alive "blind-case: dev instance started" "$PID_BLIND"
SHADOW="$TMP/shadow"; mkdir -p "$SHADOW"
printf '#!/bin/sh\nexit 1\n' > "$SHADOW/ps"; chmod +x "$SHADOW/ps"
( PATH="$SHADOW:$PATH"; . "$HELPER"; evict_own_bridge remote-gateway "$A" GATEWAY_INSTANCE "" ) 2>/dev/null
sleep 0.4
assert_alive "blind-case: env unreadable -> live instance SURVIVED (never kills on unknown)" "$PID_BLIND"
kill "$PID_BLIND" 2>/dev/null || true

# --- list mode (read-only verifier over the SAME identity decision) ---------
# The one-owner rule (#3553 round 4): health-check's post-eviction survivor scan
# delegates here instead of mirroring the policy in Python. OWN only for this
# checkout; foreign silent; unreadable identity prints INDETERMINATE.
( cd "$A" && exec "$PY" src/slack-bridge.py ) & PID_LA=$!
( cd "$B" && exec "$PY" src/slack-bridge.py ) & PID_LB=$!
sleep 0.6
LIST_OUT="$(bash "$HELPER" --list slack "$A")"
case "$LIST_OUT" in
  "OWN $PID_LA") echo "  ok   list mode: exactly this checkout's pid, foreign silent" ;;
  *) echo "  FAIL list mode output: '$LIST_OUT' (expected 'OWN $PID_LA')"; FAILED=1 ;;
esac
# Indeterminate identity must SURFACE, not hide. The probe seam is overridden
# directly (platform-portable: /proc makes real env/cwd readable on Linux).
LIST_BLIND="$( . "$HELPER"; _pid_env() { return 1; }; list_own_bridge slack "$A" GATEWAY_INSTANCE "" 2>/dev/null )"
case "$LIST_BLIND" in
  *INDETERMINATE*) echo "  ok   list mode: indeterminate identity prints INDETERMINATE (fail-closed signal)" ;;
  *) echo "  FAIL list mode hid an indeterminate pid: '$LIST_BLIND'"; FAILED=1 ;;
esac
kill "$PID_LA" "$PID_LB" 2>/dev/null || true

# --- a checkout path CONTAINING SPACES (#3553 round 4, blocker 1's shape) ----
# The bundled install lives under "Application Support"; identity must survive
# whitespace for both the evicting and listing entry points.
mkdir -p "$TMP/spaced dir"
mk_checkout "$TMP/spaced dir/checkoutS"
SREPO="$(cd "$TMP/spaced dir/checkoutS" && pwd -P)"
( cd "$SREPO" && exec "$PY" src/slack-bridge.py ) & PID_S=$!
( cd "$B" && exec "$PY" src/slack-bridge.py ) & PID_SB=$!
sleep 0.6
assert_alive "spaced-path checkout's bridge started" "$PID_S"
SLIST="$(bash "$HELPER" --list slack "$SREPO")"
case "$SLIST" in
  "OWN $PID_S") echo "  ok   list mode: spaced-path checkout classified OWN" ;;
  *) echo "  FAIL spaced-path list output: '$SLIST' (expected 'OWN $PID_S')"; FAILED=1 ;;
esac
evict_own_bridge slack "$SREPO"
sleep 0.4
assert_dead  "spaced-path checkout's bridge was evicted" "$PID_S"
assert_alive "other checkout SURVIVED the spaced-path eviction" "$PID_SB"
kill "$PID_SB" 2>/dev/null || true

# --- hard candidate-discovery failure (#3553 round 5) ------------------------
# A pgrep rc >1 is an UNREADABLE process table, not emptiness. Both REAL entry
# points must exit nonzero and take no side effects — rc 0 + '' here is what let
# health-check report "evicted + confirmed exited" over a live stale bridge.
BADPG="$TMP/badpgrep"; mkdir -p "$BADPG"
printf '#!/bin/sh\nexit 3\n' > "$BADPG/pgrep"; chmod +x "$BADPG/pgrep"
( cd "$A" && exec "$PY" src/slack-bridge.py ) & PID_H=$!
sleep 0.6
assert_alive "hard-rc: own bridge started" "$PID_H"
LIST_HARD_OUT="$(PATH="$BADPG:$PATH" bash "$HELPER" --list slack "$A")"; LIST_HARD_RC=$?
if [ "$LIST_HARD_RC" -gt 1 ] && [ -z "$LIST_HARD_OUT" ]; then
  echo "  ok   --list: pgrep rc=3 propagates (rc=$LIST_HARD_RC), no clean-empty lie"
else
  echo "  FAIL --list swallowed a hard discovery failure: rc=$LIST_HARD_RC out='$LIST_HARD_OUT'"; FAILED=1
fi
EV_HARD_OUT="$(PATH="$BADPG:$PATH" bash "$HELPER" slack "$A" 2>/dev/null)"; EV_HARD_RC=$?
if [ "$EV_HARD_RC" -gt 1 ]; then
  echo "  ok   evict: pgrep rc=3 propagates (rc=$EV_HARD_RC)"
else
  echo "  FAIL evict swallowed a hard discovery failure: rc=$EV_HARD_RC out='$EV_HARD_OUT'"; FAILED=1
fi
sleep 0.3
assert_alive "hard-rc: bridge UNTOUCHED by the failed discovery (side-effect-free)" "$PID_H"
# rc 1 (clean no-match) stays a clean empty success — the legitimate case.
NOMATCH_OUT="$(bash "$HELPER" --list nosuchchannel "$A")"; NOMATCH_RC=$?
if [ "$NOMATCH_RC" -eq 0 ] && [ -z "$NOMATCH_OUT" ]; then
  echo "  ok   --list: rc 1 no-match is still a clean empty success"
else
  echo "  FAIL no-match case broke: rc=$NOMATCH_RC out='$NOMATCH_OUT'"; FAILED=1
fi
kill "$PID_H" 2>/dev/null || true

echo
if [ "$FAILED" -eq 0 ]; then echo "PASS — channel-bridge evict isolation"; exit 0; fi
echo "FAIL — channel-bridge evict isolation"; exit 1
