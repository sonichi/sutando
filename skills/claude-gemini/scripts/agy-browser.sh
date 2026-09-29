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

AGY_BIN="$(command -v agy 2>/dev/null || true)"
[[ -z "$AGY_BIN" && -x "$HOME/.local/bin/agy" ]] && AGY_BIN="$HOME/.local/bin/agy"
CDP_URL="http://127.0.0.1:$PORT"
MCP_PKG="chrome-devtools-mcp@1.10.1"

cdp_up() { curl -fsS --max-time 2 "$CDP_URL/json/version" >/dev/null 2>&1; }
# PIDs whose argv carries exactly this profile (and, given a port, that port); fixed strings, not regex.
# The needles go in via the environment so awk's own argv never matches them.
profile_pids() {
  ps -Ao pid=,command= | D="--user-data-dir=$PROFILE " P="${1:+--remote-debugging-port=$1 }" \
    awk 'index($0 " ", ENVIRON["D"]) && (ENVIRON["P"] == "" || index($0 " ", ENVIRON["P"])) { print $1 }'
}
ours() { [[ -n "$(profile_pids "$PORT")" ]]; }
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
    state="$(mcp_state)"
    [[ "$state" == elsewhere ]] && fail "agy's chrome-devtools server points at another browser URL; not overwriting it. Remove it with 'agy mcp remove chrome-devtools' or pass that --port."
    [[ "$state" == pinned ]] || command -v npx >/dev/null 2>&1 || fail "npx not found; install Node.js to run chrome-devtools-mcp"
    launched=""
    if cdp_up; then
      ours || fail "$CDP_URL is answered by a process not running on $PROFILE (possibly your own Chrome); not using it. Pick another --port."
      echo "browser: already listening on $CDP_URL"
    else
      mkdir -p "$PROFILE"
      chrome="$(find_chrome)"
      nohup "$chrome" --user-data-dir="$PROFILE" --remote-debugging-port="$PORT" \
        --remote-debugging-address=127.0.0.1 --no-first-run --no-default-browser-check \
        --headless=new about:blank >/dev/null 2>&1 &
      launched=$!
      for _ in $(seq 1 30); do cdp_up && break; sleep 0.5; done
      cdp_up || { kill "$launched" 2>/dev/null || true; fail "Chrome did not start listening on $CDP_URL within 15s"; }
      echo "browser: started on $CDP_URL (profile $PROFILE)"
    fi
    if [[ "$state" == pinned ]]; then
      echo "mcp: chrome-devtools already registered"
    else
      if ! "$AGY_BIN" mcp add chrome-devtools npx -y "$MCP_PKG" --browserUrl "$CDP_URL" >/dev/null; then
        [[ -n "$launched" ]] && kill "$launched" 2>/dev/null
        fail "agy mcp add failed${launched:+; stopped the Chrome this run started}"
      fi
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
    exit "$rc"
    ;;
  stop)
    pids="$(profile_pids)"
    if [[ -n "$pids" ]]; then
      kill $pids 2>/dev/null || true
      echo "browser: stopped the Chrome on $PROFILE"
    else
      echo "browser: none running on $PROFILE"
    fi
    ;;
  *) usage; exit 2 ;;
esac
