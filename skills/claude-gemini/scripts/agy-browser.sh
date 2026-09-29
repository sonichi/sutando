#!/bin/bash
# Give agy a browser of its own: a headless Chrome on a separate profile, reached through the
# chrome-devtools MCP server. agy's /browser otherwise wants the user's running Chrome restarted.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: agy-browser.sh start|status|stop [--port N] [--profile DIR] [--chrome PATH]

  start   Launch the agy-only headless Chrome (if not already listening) and register the
          chrome-devtools MCP server with agy (if not already registered).
  status  Report whether that Chrome is listening and whether agy has the MCP server.
  stop    Stop only the Chrome running on the agy profile; the user's Chrome is never touched.

A port answered by any other process is refused, never adopted: it may be the user's own Chrome.
Defaults: --port 9222, --profile ~/.gemini/antigravity-browser-profile, --chrome auto-detected.
USAGE
}

fail() { echo "agy-browser.sh: $*" >&2; exit 1; }

ACTION="${1:-}"; [[ $# -gt 0 ]] && shift
PORT=9222
PROFILE="$HOME/.gemini/antigravity-browser-profile"
CHROME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    --profile) PROFILE="${2:?--profile needs a directory}"; shift 2 ;;
    --chrome) CHROME="${2:?--chrome needs a path}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done
[[ "$PORT" =~ ^[0-9]+$ ]] || fail "--port must be a number"

# Absolute, symlink-free path, so the argv match below cannot hit another cwd's relative profile.
canon() {
  if [[ -d "$1" ]]; then (cd "$1" && pwd -P)
  elif parent="$(cd "$(dirname "$1")" 2>/dev/null && pwd -P)"; then echo "$parent/$(basename "$1")"
  elif [[ "$1" == /* ]]; then echo "$1"
  else echo "$PWD/$1"; fi
}
[[ "$ACTION" == start ]] && mkdir -p "$PROFILE"
PROFILE="$(canon "$PROFILE")"

AGY_BIN="$(command -v agy 2>/dev/null || true)"
[[ -z "$AGY_BIN" && -x "$HOME/.local/bin/agy" ]] && AGY_BIN="$HOME/.local/bin/agy"
LSOF="$(command -v lsof 2>/dev/null || true)"
for c in /usr/sbin/lsof /usr/bin/lsof; do [[ -z "$LSOF" && -x "$c" ]] && LSOF="$c"; done
CDP_URL="http://127.0.0.1:$PORT"
MCP_PKG="chrome-devtools-mcp@1.10.1"

cdp_up() { curl -fsS --max-time 2 "$CDP_URL/json/version" >/dev/null 2>&1; }
# PIDs whose argv carries exactly one --user-data-dir, and it is this profile; fixed strings, not regex.
# The needle goes in via the environment so awk's own argv never matches it.
profile_pids() {
  ps -Ao pid=,command= | D="--user-data-dir=$PROFILE " \
    awk '{ n = gsub(/--user-data-dir=/, "&") } n == 1 && index($0 " ", ENVIRON["D"]) { print $1 }'
}
# True only when every process listening on the port is itself one of the profile's processes.
ours() {
  [[ -n "$LSOF" ]] || return 1
  local listeners owned p
  listeners="$("$LSOF" -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
  [[ -n "$listeners" ]] || return 1
  owned="$(profile_pids)"
  for p in $listeners; do grep -qx "$p" <<<"$owned" || return 1; done
}
# TERM every process on the profile, then wait up to 5s for all of them to be gone.
stop_profile() {
  local pids; pids="$(profile_pids)"
  [[ -n "$pids" ]] || return 0
  kill $pids 2>/dev/null || true
  for _ in $(seq 1 25); do [[ -z "$(profile_pids)" ]] && return 0; sleep 0.2; done
  return 1
}
# Fail, first tearing down the Chrome this run launched, if any.
abort_start() {
  [[ -n "$launched" ]] || fail "$1"
  stop_profile || fail "$1; the Chrome this run started on $PROFILE did not exit"
  fail "$1; stopped the Chrome this run started"
}
need_npx() { command -v npx >/dev/null 2>&1; }
NPX_MSG="npx not found; install Node.js to run chrome-devtools-mcp"
NOT_OURS="$CDP_URL is answered by a process not running on $PROFILE (possibly your own Chrome); not using it. Pick another --port."
ELSEWHERE="agy's chrome-devtools server points at another browser URL; not overwriting it. Remove it with 'agy mcp remove chrome-devtools' or pass that --port."
# Prints pinned, other (this URL, another package), elsewhere (another URL) or none.
mcp_state() {
  [[ -n "$AGY_BIN" ]] || { echo none; return; }
  "$AGY_BIN" mcp list 2>/dev/null | U=" --browserUrl $CDP_URL " K=" $MCP_PKG " awk '
    $1 != "chrome-devtools" { next }
    index($0 " ", ENVIRON["U"]) { s = index($0 " ", ENVIRON["K"]) ? "pinned" : "other"; next }
    s == "" { s = "elsewhere" }
    END { print s ? s : "none" }'
}

find_chrome() {
  [[ -n "$CHROME" ]] && { [[ -x "$CHROME" ]] || fail "--chrome is not executable: $CHROME"; echo "$CHROME"; return; }
  local c
  for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
           "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
    [[ -x "$c" ]] && { echo "$c"; return; }
  done
  for c in google-chrome google-chrome-stable chromium chromium-browser; do
    command -v "$c" >/dev/null 2>&1 && { command -v "$c"; return; }
  done
  fail "no Chrome or Chromium found; pass --chrome PATH"
}

case "$ACTION" in
  start)
    [[ -n "$AGY_BIN" ]] || fail "agy not found on PATH or in ~/.local/bin"
    need_npx || fail "$NPX_MSG"
    [[ -n "$LSOF" ]] || fail "lsof not found; it is needed to check who owns $CDP_URL"
    [[ "$(mcp_state)" == elsewhere ]] && fail "$ELSEWHERE"
    launched=""
    if cdp_up; then
      ours || fail "$NOT_OURS"
      echo "browser: already listening on $CDP_URL"
    else
      chrome="$(find_chrome)"
      nohup "$chrome" --user-data-dir="$PROFILE" --remote-debugging-port="$PORT" \
        --remote-debugging-address=127.0.0.1 --no-first-run --no-default-browser-check \
        --headless=new about:blank >/dev/null 2>&1 &
      launched=$!
      for _ in $(seq 1 30); do cdp_up && break; sleep 0.5; done
      cdp_up || abort_start "Chrome did not start listening on $CDP_URL within 15s"
      ours || abort_start "$NOT_OURS"
      echo "browser: started on $CDP_URL (profile $PROFILE)"
    fi
    # The registry and the listener may have changed during the launch wait; recheck both.
    state="$(mcp_state)"
    [[ "$state" == elsewhere ]] && abort_start "$ELSEWHERE"
    ours || abort_start "$NOT_OURS"
    if [[ "$state" == pinned ]]; then
      echo "mcp: chrome-devtools already registered"
    else
      "$AGY_BIN" mcp add chrome-devtools npx -y "$MCP_PKG" --browserUrl "$CDP_URL" >/dev/null \
        || abort_start "agy mcp add failed"
      if [[ "$state" == other ]]; then echo "mcp: chrome-devtools re-registered with $MCP_PKG"
      else echo "mcp: chrome-devtools registered with agy"; fi
    fi
    ;;
  status)
    rc=0
    if ! cdp_up; then echo "browser: not listening on $CDP_URL"; rc=1
    elif ours; then echo "browser: listening on $CDP_URL"
    else echo "browser: $CDP_URL is held by a process not running on $PROFILE"; rc=1; fi
    case "$(mcp_state)" in
      pinned) echo "mcp: chrome-devtools registered" ;;
      other) echo "mcp: chrome-devtools registered with a package other than $MCP_PKG; run start to re-register"; rc=1 ;;
      elsewhere) echo "mcp: chrome-devtools points at another browser URL, not $CDP_URL"; rc=1 ;;
      *) echo "mcp: chrome-devtools not registered"; rc=1 ;;
    esac
    need_npx || { echo "mcp: $NPX_MSG"; rc=1; }
    exit "$rc"
    ;;
  stop)
    if [[ -z "$(profile_pids)" ]]; then
      echo "browser: none running on $PROFILE"
    elif stop_profile; then
      echo "browser: stopped the Chrome on $PROFILE"
    else
      fail "the Chrome on $PROFILE did not exit within 5s; still running: $(profile_pids | tr '\n' ' ')"
    fi
    ;;
  *) usage; exit 2 ;;
esac
