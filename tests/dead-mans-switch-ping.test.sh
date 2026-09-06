#!/usr/bin/env bash
# Hermetic tests for ping.sh: a local HTTP capture stands in for the external
# monitor, so no outbound request leaves the host.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd -P)"
PING="$REPO/skills/dead-mans-switch/scripts/ping.sh"

TMPDIR="$(mktemp -d)"
SERVER_PID=""
cleanup() {
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
    rm -rf "$TMPDIR"
}
trap cleanup EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }
ok()   { echo "  ok  $1"; }

# --- capture server: logs each request path to $TMPDIR/requests.log ----------
CAP="$TMPDIR/requests.log"
: > "$CAP"
python3 - "$CAP" > "$TMPDIR/port" 2>/dev/null <<'PYEOF' &
import sys, http.server, socketserver

cap_path = sys.argv[1]

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with open(cap_path, "a") as f:
            f.write(self.path + "\n")
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *a):
        pass

with socketserver.TCPServer(("127.0.0.1", 0), H) as srv:
    print(srv.server_address[1], flush=True)
    srv.serve_forever()
PYEOF
SERVER_PID=$!
for _ in $(seq 1 20); do [ -s "$TMPDIR/port" ] && break; sleep 0.25; done
[ -s "$TMPDIR/port" ] || fail "capture server did not start"
PORT="$(cat "$TMPDIR/port")"
URL="http://127.0.0.1:$PORT/ping"

ALIVE="$TMPDIR/host.alive"

run_ping() {  # run_ping <url-or-empty> [alive-file]
    env -u HEALTHCHECKS_PING_URL -u SUTANDO_DEADMAN_ALIVE_FILE \
        ${1:+HEALTHCHECKS_PING_URL="$1"} ${2:+SUTANDO_DEADMAN_ALIVE_FILE="$2"} \
        bash "$PING" 2>/dev/null
}

# --- fresh alive file → healthy ping (no /fail suffix) ------------------------
touch "$ALIVE"
run_ping "$URL" "$ALIVE" || fail "healthy ping: expected exit 0, got $?"
last="$(tail -1 "$CAP")"
[ "$last" = "/ping" ] || fail "healthy ping: expected request '/ping', got '$last'"
ok "fresh alive file pings the healthy endpoint"

# --- stale alive file (mtime 5 min ago) → /fail --------------------------------
python3 -c 'import os,sys,time; t=time.time()-300; os.utime(sys.argv[1],(t,t))' "$ALIVE"
run_ping "$URL" "$ALIVE" || fail "stale ping: expected exit 0, got $?"
last="$(tail -1 "$CAP")"
[ "$last" = "/ping/fail" ] || fail "stale ping: expected '/ping/fail', got '$last'"
ok "stale alive file pings /fail"

# --- missing alive file → /fail -------------------------------------------------
rm -f "$ALIVE"
run_ping "$URL" "$ALIVE" || fail "missing-alive ping: expected exit 0, got $?"
last="$(tail -1 "$CAP")"
[ "$last" = "/ping/fail" ] || fail "missing-alive ping: expected '/ping/fail', got '$last'"
ok "missing alive file pings /fail"

# --- unreachable monitor → fail-open exit 0, no crash --------------------------
touch "$ALIVE"
run_ping "http://127.0.0.1:1/ping" "$ALIVE" || fail "unreachable monitor: expected exit 0 (fail-open), got $?"
ok "unreachable monitor endpoint fails open (exit 0)"

# --- no URL configured → silent no-op, no request ------------------------------
lines_before="$(wc -l < "$CAP")"
# HOME redirect keeps any vault lookup away from real keys; vault miss → no-op.
HOME="$TMPDIR" run_ping "" "$ALIVE" || fail "unconfigured: expected exit 0, got $?"
lines_after="$(wc -l < "$CAP")"
[ "$lines_before" = "$lines_after" ] || fail "unconfigured: a request was sent despite no URL"
ok "no configured URL is a silent no-op"

echo
echo "OK — 5/5 dead-mans-switch ping tests passed"
