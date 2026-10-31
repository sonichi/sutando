#!/usr/bin/env bash
# The dispatcher refuses to launch when no tmux can reach the socket's server, or when the socket
# is a desktop install's and this checkout is not that install's engine. Stubs only.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
TD="$(mktemp -d)"; trap 'rm -rf "$TD"' EXIT
fails=0
say() { echo "$1  $2"; [ "$1" = FAIL ] && fails=$((fails+1)); return 0; }

make_fixture() {
  local fix="$1"
  mkdir -p "$fix/src/agent/claude/cli" "$fix/src/agent/codex/cli" "$fix/scripts" "$fix/bin"
  cp "$REPO/src/agent/start-cli.sh" "$REPO/src/agent/restart-guard.sh" \
    "$REPO/src/agent/tmux-socket-guard.sh" "$REPO/src/agent/core-runtime-record.sh" "$fix/src/agent/"
  for rt in claude codex; do  # each stub confirms its core at once
    printf '#!/bin/bash\necho "LAUNCHER %s $* tmux=$(command -v tmux) sock=${SUTANDO_TMUX_SOCKET:-unset}" >> "$TMUX_LOG"\nREPO=%q; . "$REPO/src/agent/core-runtime-record.sh"; persist_confirmed_desktop_selection %s\n' \
      "$rt" "$fix" "$rt" > "$fix/src/agent/$rt/cli/start-cli.sh"
  done
  cat > "$fix/scripts/sutando-config.sh" <<'EOF'
#!/bin/bash
case "$1" in
  workspace) echo "$TEST_WS" ;;
  desktop-runtime-select)
    echo "SELECT $2${3:+ $3}" >> "$TMUX_LOG"
    [ -n "${SELECT_FAILS:-}" ] && { echo "selection not written" >&2; exit 1; }
    [ "${3:-}" = --check ] && { echo would-update; exit 0; }
    echo "${SELECT_RESULT:-updated}" ;;
esac
EOF
  cat > "$fix/bin/tmux" <<'EOF'
#!/bin/bash
echo "$*" >> "$TMUX_LOG"
[ "${1:-}" = -V ] && { echo "tmux ${CLIENT_VERSION:-3.5a}"; exit 0; }
case "$*" in
  *list-sessions*)
    case "${SERVER:-same}" in
      same) echo "${CLIENT_VERSION:-3.5a}" ;;
      other) echo "3.8" ;;
      unreachable) echo "server exited unexpectedly" >&2; exit 1 ;;
      none) echo "no server running on ${3:-}" >&2; exit 1 ;;
    esac ;;
  *has-session*) [ "${SERVER:-same}" = none ] && exit 1; exit 0 ;;
  *show-environment*SUTANDO_CORE_RUNTIME*) echo "SUTANDO_CORE_RUNTIME=claude" ;;
esac
exit 0
EOF
  mkdir -p "$fix/appbin"
  cat > "$fix/appbin/tmux" <<'EOF'
#!/bin/bash
[ "${1:-}" = -V ] && { echo "tmux ${APP_CLIENT_VERSION:-3.8}"; exit 0; }
case "$*" in *list-sessions*) echo "3.8" ;; esac
exit 0
EOF
  chmod +x "$fix/bin/tmux" "$fix/appbin/tmux" "$fix/scripts/sutando-config.sh" "$fix"/src/agent/*/cli/start-cli.sh
}

FIX="$TD/repo"; make_fixture "$FIX"

# run <fixture> <PATH> <socket|-> [env...]   ("-": no SUTANDO_TMUX_SOCKET, as from a terminal)
run() {
  local fix="$1" path="$2" sock="$3"; shift 3
  : > "$TD/log"
  local sock_env=(SUTANDO_TMUX_SOCKET="$sock")
  [ "$sock" = - ] && sock_env=()
  env -i PATH="$path" HOME="$TD" TMUX_LOG="$TD/log" TEST_WS="$TD/ws" ${sock_env[@]+"${sock_env[@]}"} "$@" \
    /bin/bash "$fix/src/agent/start-cli.sh" --runtime claude > "$TD/out" 2> "$TD/err" < /dev/null
  rc=$?
}
refused() {  # <label> <stderr phrase>
  if [ "$rc" = 3 ] && ! grep -q LAUNCHER "$TD/log" && ! grep -q kill-session "$TD/log" \
     && grep -q "$2" "$TD/err"; then
    say PASS "$1: refused before any launcher or kill"
  else
    say FAIL "$1: rc=$rc err=$(tr '\n' '|' < "$TD/err") log=$(tr '\n' '|' < "$TD/log")"
  fi
}
launched() {  # <label>
  if [ "$rc" = 0 ] && grep -q "LAUNCHER claude" "$TD/log"; then
    say PASS "$1: launcher ran"
  else
    say FAIL "$1: rc=$rc err=$(tr '\n' '|' < "$TD/err") log=$(tr '\n' '|' < "$TD/log")"
  fi
}

run "$FIX" "$FIX/bin:/usr/bin:/bin" "$TD/sock" SERVER=unreachable
refused "client cannot talk to the server" "cannot query the tmux server"
run "$FIX" "$FIX/bin:/usr/bin:/bin" "$TD/sock" SERVER=other
if [ "$rc" = 0 ] && grep -q "LAUNCHER claude .*tmux=$FIX/bin/tmux" "$TD/log" \
   && grep -q "warning: .*is tmux 3.5a, but tmux 3.8 owns" "$TD/err"; then
  say PASS "versions differ but the client reaches the server: warns naming both, launches with it"
else
  say FAIL "reachable mismatch: rc=$rc err=$(tr '\n' '|' < "$TD/err") log=$(tr '\n' '|' < "$TD/log")"
fi
run "$FIX" "$FIX/bin:/usr/bin:/bin" "$TD/sock" SERVER=unreachable SUTANDO_TMUX_BIN="$FIX/appbin/tmux" APP_CLIENT_VERSION=3.7
if [ "$rc" = 0 ] && grep -q "LAUNCHER claude .*tmux=$FIX/appbin/tmux" "$TD/log" \
   && grep -q "warning: .*appbin/tmux is tmux 3.7, but tmux 3.8 owns" "$TD/err"; then
  say PASS "PATH tmux cannot reach the server: the declared client that does goes first on PATH"
else
  say FAIL "declared reachable mismatch: rc=$rc err=$(tr '\n' '|' < "$TD/err") log=$(tr '\n' '|' < "$TD/log")"
fi
run "$FIX" "$FIX/bin:/usr/bin:/bin" "$TD/sock" SERVER=other SUTANDO_TMUX_BIN="$FIX/appbin/tmux"
if [ "$rc" = 0 ] && grep -q "LAUNCHER claude .*tmux=$FIX/appbin/tmux" "$TD/log"; then
  say PASS "the host's declared client matches the server: launcher runs with it first on PATH"
else
  say FAIL "declared client: rc=$rc err=$(tr '\n' '|' < "$TD/err") log=$(tr '\n' '|' < "$TD/log")"
fi
run "$FIX" "$FIX/bin:/usr/bin:/bin" "$TD/sock" SERVER=same
launched "same tmux build"
run "$FIX" "$FIX/bin:/usr/bin:/bin" "$TD/sock" SERVER=none
launched "no server yet"

if [ -x /usr/bin/tmux ] || [ -x /bin/tmux ]; then
  say SKIP "live socket without tmux on PATH: this host has a system tmux"
else
  python3 -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1])' "$TD/live.sock"
  run "$FIX" "/usr/bin:/bin" "$TD/live.sock"
  refused "a server socket exists but no tmux is on PATH" "no tmux is on PATH"
  run "$FIX" "/usr/bin:/bin" "$TD/absent.sock"
  launched "no tmux and no server socket"
  run "$FIX" "/usr/bin:/bin" "$TD/live.sock" SUTANDO_TMUX_BIN="$FIX/appbin/tmux"
  launched "no tmux on PATH, but the host's declared client"
fi

mkdir -p "$TD/app/run" "$TD/app/engine/sutando"
run "$FIX" "$FIX/bin:/usr/bin:/bin" "$TD/app/run/tmux.sock"
refused "another checkout on a desktop socket" "belongs to the desktop engine"
run "$FIX" "$FIX/bin:/usr/bin:/bin" "$TD/app/run/tmux.sock" SUTANDO_ALLOW_FOREIGN_CORE_CHECKOUT=1
launched "explicit foreign-checkout opt-in"
OWN="$TD/app2/engine/sutando"; make_fixture "$OWN"; mkdir -p "$TD/app2/run"
run "$OWN" "$OWN/bin:/usr/bin:/bin" "$TD/app2/run/tmux.sock"
launched "the desktop's own engine checkout"
if grep -q "SELECT claude --check" "$TD/log" && grep -qx "SELECT claude" "$TD/log" \
   && [ "$(grep -n -m1 -x "SELECT claude" "$TD/log" | cut -d: -f1)" -gt "$(grep -n -m1 LAUNCHER "$TD/log" | cut -d: -f1)" ] \
   && grep -q "desktop runtime selection is now claude" "$TD/err"; then
  say PASS "an explicit --runtime on a desktop install becomes the desktop's selection once confirmed"
else
  say FAIL "desktop selection: err=$(tr '\n' '|' < "$TD/err") log=$(tr '\n' '|' < "$TD/log")"
fi
run "$OWN" "$OWN/bin:/usr/bin:/bin" "$TD/app2/run/tmux.sock" SELECT_RESULT=unchanged
if [ "$rc" = 0 ] && grep -q "LAUNCHER claude" "$TD/log" && ! grep -q "selection is now" "$TD/err"; then
  say PASS "an unchanged desktop selection launches quietly"
else
  say FAIL "unchanged selection: rc=$rc err=$(tr '\n' '|' < "$TD/err") log=$(tr '\n' '|' < "$TD/log")"
fi
run "$OWN" "$OWN/bin:/usr/bin:/bin" "$TD/app2/run/tmux.sock" SELECT_FAILS=1
if [ "$rc" = 1 ] && ! grep -q LAUNCHER "$TD/log" && ! grep -q kill-session "$TD/log" \
   && grep -q "the desktop would still select another runtime" "$TD/err"; then
  say PASS "an unwritable desktop selection refuses before any launcher or kill"
else
  say FAIL "unwritable selection: rc=$rc err=$(tr '\n' '|' < "$TD/err") log=$(tr '\n' '|' < "$TD/log")"
fi
run "$FIX" "$FIX/bin:/usr/bin:/bin" "$TD/sock" SERVER=same
if [ "$rc" = 0 ] && ! grep -q SELECT "$TD/log"; then
  say PASS "no desktop: the selection is left alone"
else
  say FAIL "no desktop: rc=$rc log=$(tr '\n' '|' < "$TD/log")"
fi
# 2026-10-23: a core suggested `SUTANDO_APP_SUPPORT=<app> bash src/agent/start-cli.sh --runtime
# codex --restart` from a PR checkout in a terminal; it took the /tmp socket and a second core.
run "$FIX" "$FIX/bin:/usr/bin:/bin" - SUTANDO_APP_SUPPORT="$TD/app2"
refused "a terminal switch from another checkout targets the desktop's socket and refuses" \
  "belongs to the desktop engine"
if ! grep -q SELECT "$TD/log"; then
  say PASS "...and leaves the desktop's selection alone"
else
  say FAIL "refused launch wrote the selection: log=$(tr '\n' '|' < "$TD/log")"
fi
run "$FIX" "$FIX/bin:/usr/bin:/bin" - SUTANDO_APP_SUPPORT="$TD/app2" SUTANDO_ALLOW_FOREIGN_CORE_CHECKOUT=1
if [ "$rc" = 0 ] && grep -q "LAUNCHER claude .*sock=$TD/app2/run/tmux.sock" "$TD/log" \
   && grep -qx "SELECT claude" "$TD/log"; then
  say PASS "with the opt-in it launches on the desktop's socket and selects the runtime there"
else
  say FAIL "opt-in on desktop socket: rc=$rc log=$(tr '\n' '|' < "$TD/log")"
fi
mkdir -p "$TD/app2/engine/bin"; cp "$FIX/appbin/tmux" "$TD/app2/engine/bin/tmux"
run "$OWN" "$OWN/bin:/usr/bin:/bin" - SERVER=other
if [ "$rc" = 0 ] && grep -q "LAUNCHER claude .*tmux=$TD/app2/engine/bin/tmux sock=$TD/app2/run/tmux.sock" "$TD/log"; then
  say PASS "the bundled engine from a terminal uses the desktop's socket and bundled tmux"
else
  say FAIL "bundled engine from a terminal: rc=$rc err=$(tr '\n' '|' < "$TD/err") log=$(tr '\n' '|' < "$TD/log")"
fi
run "$FIX" "$FIX/bin:/usr/bin:/bin" "$TD/sock" SERVER=same SUTANDO_APP_SUPPORT="$TD/app2"
if [ "$rc" = 0 ] && ! grep -q SELECT "$TD/log" && grep -q "not the desktop's core socket" "$TD/err"; then
  say PASS "a core on another socket switches without touching the desktop's selection"
else
  say FAIL "other socket: rc=$rc err=$(tr '\n' '|' < "$TD/err") log=$(tr '\n' '|' < "$TD/log")"
fi
run "$FIX" "$FIX/bin:/usr/bin:/bin" - SERVER=same
if [ "$rc" = 0 ] && grep -q "LAUNCHER claude .*sock=unset" "$TD/log"; then
  say PASS "no desktop: the socket is left to the launcher's own default"
else
  say FAIL "no desktop default: rc=$rc log=$(tr '\n' '|' < "$TD/log")"
fi

[ "$fails" = 0 ] && echo "all tmux socket guard checks pass" || { echo "$fails failed"; exit 1; }
