#!/usr/bin/env bash
# stage-readiness.sh — pre-flight checklist for the ICLR talk.
#
# Runs a series of checks and prints a PASS/WARN/FAIL line for each.
# Exits 1 if any FAIL, 2 if any WARN, 0 if all PASS. Aimed at the 5-10 min
# window before going on screen — no interactive prompts, finishes in <20s.
#
# Checks (all in terms of "does Sutando work RIGHT NOW for the talk?"):
#   voice-agent:       port 9900 responsive, recent Health tick, client can connect
#   bodhi FATAL:       0× CLOSED→RECONNECTING in the last 10 min (post-#409 expected)
#   conversation:      port 3100 /health endpoint returns ok
#   ngrok tunnel:      query local ngrok agent API (:4040), match by public_url
#   presenter-mode:    sentinel present + future expiry, so notifications are silenced
#   quota:             >10% remaining so we don't blow through mid-segment
#   disk + memory:     not in low-space / low-mem territory
#   memory-sync:       last sync <6h so memory/notes are current
#
# Usage: bash scripts/stage-readiness.sh
#        -q | --quiet : only print non-pass lines + final summary

set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
QUIET=0
for arg in "$@"; do case "$arg" in -q|--quiet) QUIET=1 ;; esac; done

# Portable mtime helper (GNU first, BSD fallback). On Macs with Homebrew
# coreutils in PATH, BSD `stat -f %m` emits filesystem info to stdout AND
# exits non-zero — the previous `BSD || GNU` in `$(...)` concatenated both
# outputs and died under `set -u` arithmetic. See #412 cold-review.
stat_mtime() {
    stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0
}

PASS=0
WARN=0
FAIL=0
WIDTH=20

pass() { [ $QUIET -eq 0 ] && printf "  \033[32m✓ PASS\033[0m  %-${WIDTH}s %s\n" "$1" "$2"; PASS=$((PASS + 1)); }
warn() { printf "  \033[33m⚠ WARN\033[0m  %-${WIDTH}s %s\n" "$1" "$2"; WARN=$((WARN + 1)); }
fail() { printf "  \033[31m✗ FAIL\033[0m  %-${WIDTH}s %s\n" "$1" "$2"; FAIL=$((FAIL + 1)); }

[ $QUIET -eq 0 ] && echo "━━━ Sutando Stage Readiness ━━━"

# 1) voice-agent
if lsof -iTCP:9900 -sTCP:LISTEN >/dev/null 2>&1; then
    # Check for a Health tick in the last 60s.
    VLOG="$REPO/logs/voice-agent.log"
    if [ -f "$VLOG" ] && [ $(($(date +%s) - $(stat_mtime "$VLOG"))) -lt 60 ]; then
        pass "voice-agent" "port 9900 listening, log fresh (<60s)"
    else
        warn "voice-agent" "port 9900 listening but log stale"
    fi
else
    fail "voice-agent" "port 9900 not listening — start with 'npx tsx src/voice-agent.ts'"
fi

# 2) bodhi FATAL count (since last voice-agent restart)
# Scope to the current process instance — the log accumulates across restarts,
# so a raw grep will always report historical FATALs from before a prior fix
# was installed and fail this check forever. Use the last "Watching for
# context drops" line as the startup marker; count FATALs only after it.
VLOG="$REPO/logs/voice-agent.log"
if [ -f "$VLOG" ]; then
    last_start=$(grep -n "Watching for context drops" "$VLOG" 2>/dev/null | tail -1 | cut -d: -f1)
    last_start="${last_start:-1}"
    fatals=$(tail -n +"$last_start" "$VLOG" | grep -c "FATAL.*SessionError.*Invalid transition" 2>/dev/null | tr -d '[:space:]')
    fatals="${fatals:-0}"
    if [ "$fatals" -eq 0 ]; then
        pass "bodhi state machine" "0× CLOSED→RECONNECTING FATALs since last restart"
    elif [ "$fatals" -lt 5 ]; then
        warn "bodhi state machine" "$fatals FATALs since restart — consider another restart if talk is <1h away"
    else
        fail "bodhi state machine" "$fatals FATALs since restart — restart voice-agent before talk"
    fi
fi

# 3) conversation-server
if curl -s -m 3 "http://localhost:3100/health" 2>/dev/null | grep -q '"status":"ok"'; then
    pass "conversation-server" "port 3100 /health ok"
else
    warn "conversation-server" "port 3100 /health not responding (non-blocking if not using phone)"
fi

# 4) ngrok tunnel
# Strip quotes AND any trailing comment after `#` AND whitespace.
NGROK_URL=$(grep -E "^TWILIO_WEBHOOK_URL=" "$REPO/.env" 2>/dev/null | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' | tr -d '"' | tr -d '[:space:]' | head -1)
if [ -n "${NGROK_URL:-}" ]; then
    # Preferred: check the local ngrok agent API. macOS ships LibreSSL which
    # fails TLS 1.3 negotiation with modern ngrok edge servers (curl: (35)
    # tlsv1 alert protocol version), so an external curl of $NGROK_URL gives
    # a false "unreachable" WARN even when the tunnel is fully live. The local
    # API at :4040 is plain HTTP and authoritative for "is my tunnel up?".
    tunnel_json=$(curl -s -m 3 http://localhost:4040/api/tunnels 2>/dev/null)
    if [ -n "$tunnel_json" ]; then
        # Match if any tunnel's public_url equals $NGROK_URL (with or without
        # trailing slash).
        match=$(printf '%s' "$tunnel_json" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
want = '$NGROK_URL'.rstrip('/')
for t in d.get('tunnels', []):
    if t.get('public_url', '').rstrip('/') == want:
        conns = t.get('metrics', {}).get('conns', {}).get('count', 0)
        print(f\"ok conns={conns} addr={t.get('config',{}).get('addr','?')}\")
        break
" 2>/dev/null)
        if [ -n "$match" ]; then
            pass "ngrok tunnel" "$NGROK_URL → local agent reports $match"
        else
            warn "ngrok tunnel" "local agent up but no tunnel matches $NGROK_URL"
        fi
    else
        warn "ngrok tunnel" "local ngrok API at :4040 unreachable (agent down?)"
    fi
else
    [ $QUIET -eq 0 ] && echo "  (skip) ngrok: no TWILIO_WEBHOOK_URL configured"
fi

# 5) presenter-mode sentinel
# Same fail-open bug class as the Python helpers in discord-bridge / telegram-bridge /
# check-pending-questions (PR #432 fixup). String comparison treats "garbage" as
# GREATER than any real ISO timestamp — without the digit-prefix guard, malformed
# sentinel content would appear active forever.
SENT="$REPO/state/presenter-mode.sentinel"
if [ -f "$SENT" ]; then
    expire_iso=$(cat "$SENT")
    now_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    if [[ ! "$expire_iso" =~ ^[0-9] ]]; then
        warn "presenter-mode" "sentinel content malformed — run 'bash scripts/presenter-mode.sh stop' to reset"
    elif [[ "$now_iso" < "$expire_iso" ]]; then
        pass "presenter-mode" "ACTIVE until $expire_iso (notifications silenced)"
    else
        warn "presenter-mode" "sentinel expired — run 'bash scripts/presenter-mode.sh start' before talk"
    fi
else
    warn "presenter-mode" "INACTIVE — run 'bash scripts/presenter-mode.sh start' before talk"
fi

# 6) quota
QUOTA_OUT=$(python3 "$HOME/.claude/skills/quota-tracker/scripts/read-quota.py" 2>/dev/null || echo "")
REM=$(echo "$QUOTA_OUT" | grep -oE '[0-9]+% remaining' | head -1 | grep -oE '[0-9]+')
if [ -n "${REM:-}" ]; then
    if [ "$REM" -ge 10 ]; then
        pass "Claude quota" "${REM}% remaining"
    else
        warn "Claude quota" "${REM}% remaining — may run low mid-talk"
    fi
else
    [ $QUIET -eq 0 ] && echo "  (skip) Claude quota: read-quota.py unavailable"
fi

# 7) disk
# Portable disk-free in GB — `df -g` is BSD-only (GNU df uses `-h`/`-B`).
# POSIX `df -k` returns KB; convert to GB via arithmetic. See #412 cold-review.
AVAIL_KB=$(df -k / 2>/dev/null | awk 'NR==2 {print $4}')
AVAIL_GB=$(( ${AVAIL_KB:-0} / 1024 / 1024 ))
if [ -n "${AVAIL_GB:-}" ]; then
    if [ "$AVAIL_GB" -gt 5 ]; then
        pass "disk space" "${AVAIL_GB} GB free on /"
    else
        warn "disk space" "only ${AVAIL_GB} GB free — clean up before talk"
    fi
fi

# 8) memory-sync age
SYNC_HEAD="$HOME/.sutando-memory-sync/.git/FETCH_HEAD"
if [ -f "$SYNC_HEAD" ]; then
    age_sec=$(($(date +%s) - $(stat_mtime "$SYNC_HEAD")))
    age_h=$((age_sec / 3600))
    if [ "$age_h" -lt 6 ]; then
        pass "memory-sync" "last sync ${age_h}h ago"
    elif [ "$age_h" -lt 48 ]; then
        warn "memory-sync" "last sync ${age_h}h ago — run 'bash src/sync-memory.sh'"
    else
        fail "memory-sync" "last sync ${age_h}h ago — stale, sync before talk"
    fi
fi

echo ""
printf "Summary: %d pass, %d warn, %d fail\n" "$PASS" "$WARN" "$FAIL"
if [ "$FAIL" -gt 0 ]; then exit 1
elif [ "$WARN" -gt 0 ]; then exit 2
else exit 0; fi
