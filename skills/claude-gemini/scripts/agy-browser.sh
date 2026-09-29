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

cdp_up() { curl -fsS --max-time 2 "$CDP_URL/json/version" >/dev/null 2>&1; }
mcp_registered() { [[ -n "$AGY_BIN" ]] && "$AGY_BIN" mcp list 2>/dev/null | grep -q "^chrome-devtools .*--browserUrl $CDP_URL"; }

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
    if cdp_up; then
      echo "browser: already listening on $CDP_URL"
    else
      mkdir -p "$PROFILE"
      chrome="$(find_chrome)"
      nohup "$chrome" --user-data-dir="$PROFILE" --remote-debugging-port="$PORT" \
        --remote-debugging-address=127.0.0.1 --no-first-run --no-default-browser-check \
        --headless=new about:blank >/dev/null 2>&1 &
      for _ in $(seq 1 30); do cdp_up && break; sleep 0.5; done
      cdp_up || fail "Chrome did not start listening on $CDP_URL within 15s"
      echo "browser: started on $CDP_URL (profile $PROFILE)"
    fi
    if mcp_registered; then
      echo "mcp: chrome-devtools already registered"
    else
      command -v npx >/dev/null 2>&1 || fail "npx not found; install Node.js to run chrome-devtools-mcp"
      "$AGY_BIN" mcp add chrome-devtools npx -y chrome-devtools-mcp@latest --browserUrl "$CDP_URL" >/dev/null
      echo "mcp: chrome-devtools registered with agy"
    fi
    ;;
  status)
    rc=0
    if cdp_up; then echo "browser: listening on $CDP_URL"; else echo "browser: not listening on $CDP_URL"; rc=1; fi
    if mcp_registered; then echo "mcp: chrome-devtools registered"; else echo "mcp: chrome-devtools not registered"; rc=1; fi
    exit "$rc"
    ;;
  stop)
    if pkill -f -- "--user-data-dir=$PROFILE" 2>/dev/null; then
      echo "browser: stopped the Chrome on $PROFILE"
    else
      echo "browser: none running on $PROFILE"
    fi
    ;;
  *) usage; exit 2 ;;
esac
