#!/usr/bin/env bash
# onboard.sh — connect THIS Sutando instance to a hosted AG2 relay.
# Run once, interactively:  bash skills/ag2-relay/onboard.sh
# Writes AG2_REMOTE_TOKEN + AG2_AGENT_NAME to the repo .env (quoted), then
# startup.sh auto-starts the client on every boot. See SKILL.md.
set -uo pipefail
cd "$(dirname "$0")/../.."   # repo root — .env lives here

if [ -n "${AG2_REMOTE_TOKEN:-}" ] || grep -q "^AG2_REMOTE_TOKEN=" .env 2>/dev/null; then
  echo "already configured (.env has AG2_REMOTE_TOKEN) — remove it to re-onboard"
  exit 0
fi

# Non-interactive mode (agent-driven trigger): all inputs as arguments.
#   onboard.sh "<string>" "<instance-name>" ["<password>"]
# - new user (string contains "|"): password required (their NEW platform login)
# - existing user (bare address): password = their EXISTING platform password,
#   instance-name empty = reconnect to their existing agent
_ARG_IN="${1:-}"; _ARG_NAME="${2:-}"; _ARG_PASS="${3:-${AG2_ONBOARD_PASSWORD:-}}"
# AG2 onboarding: no AG2_REMOTE_TOKEN and we're interactive -> offer to
# connect right here. ONE prompt, the input shape picks the journey:
#   "https://<base>|<code>"  -> new user: redeem invite (creates account+agent)
#   "https://<base>"         -> existing user: log in, claim/reconnect agent
# The address travels in the pasted string — nothing service-specific lives
# in this repo. Non-interactive runs skip silently; failure never blocks.
  if [ -n "$_ARG_IN" ]; then _AG2_IN="$_ARG_IN"; else
    printf '  AG2 onboarding string or platform address (Enter to skip): '
    read -r _AG2_IN || _AG2_IN=""
  fi
  _AG2_RESP=""
  _AG2_BASE=""
  if [ -n "$_AG2_IN" ] && [[ "$_AG2_IN" == *"|"* ]]; then
    # New-user journey: invite redeem (account + agent + token in one shot).
    _AG2_BASE="${_AG2_IN%%|*}"; _AG2_CODE="${_AG2_IN#*|}"
    _FUN_A=(swift quiet lucky cosmic mellow brave nimble sunny)
    _FUN_B=(falcon otter lynx comet willow ember harbor sparrow)
    _FUN_NAME="${_FUN_A[$((RANDOM % 8))]}-${_FUN_B[$((RANDOM % 8))]}"
    if [ -n "$_ARG_NAME" ] || [ -n "$_ARG_PASS" ]; then
      _AG2_USER="${_ARG_NAME:-$_FUN_NAME}"; _AG2_PASS="$_ARG_PASS"
    else
      printf '  Name this Sutando instance [Enter = %s]: ' "$_FUN_NAME"
      read -r _AG2_USER || _AG2_USER=""
      _AG2_USER="${_AG2_USER:-$_FUN_NAME}"
      printf '  Choose a password for your NEW platform login (min 8 chars): '
      read -rs _AG2_PASS; echo
    fi
    _AG2_RESP=$(curl -sf -X POST "$_AG2_BASE/redeem" -H 'content-type: application/json' \
      -d "$(printf '%s\0%s\0%s' "$_AG2_CODE" "$_AG2_USER" "$_AG2_PASS" | python3 -c 'import json,sys; v=sys.stdin.read().split(chr(0)); print(json.dumps({"invite": v[0], "username": v[1], "password": v[2]}))')" 2>/dev/null) || _AG2_RESP=""
  elif [ -n "$_AG2_IN" ]; then
    _AG2_BASE="${_AG2_IN%/}"
    # North-star (device-authorization flow): open a browser to log in — no
    # credentials typed in the terminal. Interactive human path only; the
    # agent-driven CLI path (password supplied as arg) skips straight to login.
    if [ -z "$_ARG_PASS" ] && [ -t 0 ]; then
      _DS=$(curl -sf -X POST "$_AG2_BASE/connect/start" -H 'content-type: application/json' -d '{}' 2>/dev/null)
      _DC=$(printf '%s' "$_DS" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("device_code",""))' 2>/dev/null)
      _VU=$(printf '%s' "$_DS" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("verify_url",""))' 2>/dev/null)
      if [ -z "$_DC" ] && [ -n "$_DS" ]; then
        echo "  (device-login unavailable here — falling back to login)"
      fi
      if [ -n "$_DC" ] && [ -n "$_VU" ]; then
        echo "  Opening your browser to log in:"
        echo "    $_VU"
        { command -v open >/dev/null 2>&1 && open "$_VU"; } 2>/dev/null \
          || { command -v xdg-open >/dev/null 2>&1 && xdg-open "$_VU"; } 2>/dev/null \
          || echo "  (couldn't auto-open — paste that URL into a browser)"
        printf '  Waiting for you to finish logging in'
        _DEADLINE=$(( $(date +%s) + 900 ))
        while [ "$(date +%s)" -lt "$_DEADLINE" ]; do
          _PR=$(curl -sf -X POST "$_AG2_BASE/connect/poll" -H 'content-type: application/json' \
            -d "$(printf '%s' "$_DC" | python3 -c 'import json,sys;print(json.dumps({"device_code":sys.stdin.read()}))')" 2>/dev/null)
          if printf '%s' "$_PR" | python3 -c 'import json,sys;sys.exit(0 if json.load(sys.stdin).get("bearer") else 1)' 2>/dev/null; then
            _AG2_RESP="$_PR"; echo " - done."; break
          fi
          printf '.'; sleep 3
        done
        [ -n "$_AG2_RESP" ] || { echo; echo "  (browser login didn't complete — you can type credentials instead)"; }
      fi
    fi
    if [ -z "$_AG2_RESP" ]; then
    # Bare address: existing-account login, or request access (no invite yet).
    printf '  Do you already have a platform account? (y/N): '
    read -r _AG2_HAS || _AG2_HAS=""
    if [ "$_AG2_HAS" != "y" ] && [ "$_AG2_HAS" != "Y" ]; then
      # Request-access journey: records an application; the operator approves
      # and you receive an invite to finish onboarding.
      printf '  Your email (for the invite): '
      read -r _AG2_EMAIL || _AG2_EMAIL=""
      printf '  Your name: '
      read -r _AG2_NAME || _AG2_NAME=""
      printf '  One line on why / who invited you: '
      read -r _AG2_WHY || _AG2_WHY=""
      _AG2_APPLY=$(curl -sf -X POST "$_AG2_BASE/apply" -H 'content-type: application/json' \
        -d "$(printf '%s\0%s\0%s' "$_AG2_EMAIL" "$_AG2_NAME" "$_AG2_WHY" | python3 -c 'import json,sys; v=sys.stdin.read().split(chr(0)); print(json.dumps({"email": v[0], "name": v[1], "reason": v[2]}))')" 2>/dev/null) || _AG2_APPLY=""
      if [ -n "$_AG2_APPLY" ]; then
        echo "  ✓ request submitted — once approved you'll receive an invite; rerun this script with it"
      else
        echo "  ✗ request failed (network or rate limit) — try again later"
      fi
      exit 0
    fi
    # Existing-user journey: validate platform credentials, then claim (or
    # reconnect to) their agent — no new account, no new password.
    if [ -n "$_ARG_PASS" ]; then
      _AG2_USER="${_ARG_NAME:?existing-account mode needs <instance-or-username> arg}"
      # In arg mode arg2 is the PLATFORM USERNAME for login; instance naming
      # then uses AG2_ONBOARD_LABEL if set.
      _AG2_PASS="$_ARG_PASS"
    else
      printf '  Platform username: '
      read -r _AG2_USER || _AG2_USER=""
      printf '  Platform password: '
      read -rs _AG2_PASS; echo
    fi
    _AG2_SESS=$(curl -sf -X POST "$_AG2_BASE/user-login" -H 'content-type: application/json' \
      -d "$(printf '%s\0%s' "$_AG2_USER" "$_AG2_PASS" | python3 -c 'import json,sys; v=sys.stdin.read().split(chr(0)); print(json.dumps({"username": v[0], "password": v[1]}))')" 2>/dev/null \
      | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("user_session_token") or "")
except Exception: print("")' 2>/dev/null)
    if [ -n "$_AG2_SESS" ]; then
      # Name THIS instance (becomes the agent label) — Enter reconnects to
      # the user's existing agent instead (action=list; first-timers
      # auto-create server-side).
      if [ -n "$_ARG_IN" ]; then
        _AG2_LABEL="${AG2_ONBOARD_LABEL:-}"
      else
        printf '  Name this Sutando instance [Enter = reconnect existing]: '
        read -r _AG2_LABEL || _AG2_LABEL=""
      fi
      if [ -n "$_AG2_LABEL" ]; then
        _AG2_RESP=$(curl -sf -X POST "$_AG2_BASE/claim-agent" -H 'content-type: application/json' \
          -d "$(printf '%s\0%s' "$_AG2_SESS" "$_AG2_LABEL" | python3 -c 'import json,sys; v=sys.stdin.read().split(chr(0)); print(json.dumps({"user_session_token": v[0], "action": "create", "label": v[1], "auto_spawn": False}))')" 2>/dev/null) || _AG2_RESP=""
      else
        _AG2_RESP=$(curl -sf -X POST "$_AG2_BASE/claim-agent" -H 'content-type: application/json' \
          -d "$(printf '%s' "$_AG2_SESS" | python3 -c 'import json,sys; print(json.dumps({"user_session_token": sys.stdin.read(), "action": "list", "auto_spawn": False}))')" 2>/dev/null) || _AG2_RESP=""
      fi
    else
      echo "  ✗ login failed (bad credentials or rate limit) — continuing without"
    fi
    fi
  fi
  if [ -n "$_AG2_RESP" ]; then
    _AG2_ENVLINE=$(printf '%s' "$_AG2_RESP" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    line = d.get("env_line")
    if not line and d.get("relay_url") and (d.get("relay_token") or d.get("bearer")):
        line = "AG2_REMOTE_TOKEN=%s|%s" % (d["relay_url"], d.get("relay_token") or d.get("bearer"))
    print(line or "")
except Exception: print("")' 2>/dev/null)
    if [ -n "$_AG2_ENVLINE" ]; then
      # Single-quote the value in .env — the combined token contains a pipe
      # character, which a shell source-of-.env would otherwise run as a
      # command ("command not found", empty token, dead client — 2026-06-13).
      _AG2_KEY="${_AG2_ENVLINE%%=*}"; _AG2_VAL="${_AG2_ENVLINE#*=}"
      printf "\n%s='%s'\n" "$_AG2_KEY" "$_AG2_VAL" >> .env
      export "$_AG2_KEY=$_AG2_VAL"
      # Persist the agent identity too (owner ask 2026-06-13) — quoted, since
      # matrix ids contain ':' and tools sourcing .env should get clean values.
      # Bare localpart only (owner 2026-06-13): "@" and the homeserver are
      # composed in code — keeps service specifics out of .env entirely.
      _AG2_AGENT=$(printf '%s' "$_AG2_RESP" | python3 -c 'import json,sys
try: print((json.load(sys.stdin).get("agent_id") or "").lstrip("@").split(":", 1)[0])
except Exception: print("")' 2>/dev/null)
      if [ -n "$_AG2_AGENT" ]; then
        printf "AG2_AGENT_NAME='%s'\n" "$_AG2_AGENT" >> .env
        export "AG2_AGENT_NAME=$_AG2_AGENT"
      fi
      # Persist the summary — it scrolled off-screen on the first live test.
      _AG2_SUMMARY="ag2-onboarding.txt"
      printf '%s' "$_AG2_RESP" | python3 -c 'import json,sys
d = json.load(sys.stdin)
out = ["AG2 onboarding — keep this file private (it lists your invite codes)", ""]
if d.get("agent_id"): out.append("your agent:   " + d["agent_id"])
if d.get("matrix_id"): out.append("your account: " + d["matrix_id"])
codes = d.get("invite_codes") or []
if codes:
    out.append("")
    out.append("single-use invite codes for friends:")
    out += ["  " + sys.argv[1] + "|" + c for c in codes]
print("\n".join(out))' "$_AG2_BASE" > "$_AG2_SUMMARY" 2>/dev/null || true
      echo "  ✓ onboarded — token saved to .env; details in $_AG2_SUMMARY"
      sed 's/^/    /' "$_AG2_SUMMARY" 2>/dev/null || true
    else
      echo "  ✗ onboarding failed (invalid/used invite, taken username, or network) — continuing without"
    fi
  fi

echo "now run:  bash src/startup.sh"
