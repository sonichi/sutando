#!/bin/bash
# The wrapper must find the token wherever the host's onboarding put it.
# Hosts differ: some write REMOTE_TASK_* into channels/ag2space/.env, others
# into a sibling while `.env` holds Matrix creds. Reading `.env` by name picks
# the blank file, the wrapper exits "nothing to run", and launchd stops
# supervising the bridge — observed live 2026-09-12 as EX_CONFIG with the real
# bridge running unsupervised beside a stranded duplicate.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
. "$REPO/scripts/python-binary.sh"
PY="$(require_python "$REPO" "run the gateway wrapper env-resolution test")" || exit 1

# An isolated repo: the real wrapper and the real resolver, a stub bridge that
# reports the token it was handed, and NO evict helper — the wrapper skips
# eviction when that file is absent, so this can never touch a live bridge.
run_case() {  # $1=.env body  $2=relay-client.env body  $3=optional NAME=value passed in the process env
  d=$(mktemp -d); mkdir -p "$d/src/launchd" "$d/scripts" "$d/home/channels/ag2space" \
      "$d/ws/state/channel-bridge-supervisor" "$d/ws/results" "$d/shims"
  cp "$REPO/src/launchd/gateway-bridge-wrapper.sh" "$d/src/launchd/"
  cp "$REPO/scripts/channel-env.sh" "$REPO/scripts/python-binary.sh" "$d/scripts/"
  # the resolver plus the modules it imports — a real resolution, not a stub
  cp "$REPO/src/channel_env_resolve.py" "$REPO/src/channel_env_containment.py" \
     "$REPO/src/channel_token.py" "$REPO/src/gateway_credential_sources.py" "$d/src/"
  mkdir -p "$d/shared"; cp "$REPO/shared/ag2_gateway_credentials.py" "$d/shared/"
  # Faithful to the real helper: `claude-home-path [suffix]` appends the suffix,
  # `workspace` names the supervisor's state dir.
  printf '#!/bin/bash\ncase "${1:-}" in\n  workspace) echo "%s/ws" ;;\n  claude-home-path) if [ -n "${2:-}" ]; then echo "%s/home/$2"; else echo "%s/home"; fi ;;\nesac\nexit 0\n' \
      "$d" "$d" "$d" > "$d/scripts/sutando-config.sh"; chmod +x "$d/scripts/sutando-config.sh"
  # The stub bridge reports the token it was handed, then stands down (rc 75), ending the wrapper.
  printf 'import os, sys\nprint("BRIDGE_STARTED token=%%s tier=%%s" %% (os.environ.get("REMOTE_TASK_TOKEN") or "<none>", os.environ.get("REMOTE_TASK_TIER") or "<none>"))\nsys.exit(75)\n' \
      > "$d/src/remote-gateway-bridge.py"
  printf 'import time\nwhile True: time.sleep(0.2)\n' > "$d/src/sparrowd.py"
  printf '#!/bin/bash\nexec "%s" "$@"\n' "$PY" > "$d/shims/python3"
  printf '#!/bin/bash\nexit 0\n' > "$d/shims/osascript"; chmod +x "$d/shims/python3" "$d/shims/osascript"
  printf '%s' "$1" > "$d/home/channels/ag2space/.env"
  printf '%s' "$2" > "$d/home/channels/ag2space/relay-client.env"
  # Hermetic regardless of the calling shell's own env: an ambient gateway
  # credential must never leak into this test's stdout or steer its outcome.
  ( cd "$d" && unset REMOTE_TASK_TOKEN REMOTE_TASK_TIER AG2_REMOTE_TOKEN \
        AG2_REMOTE_TIER REMOTE_MEDIA_MARKER AG2_DEVICE_ENV
    env ${3:+"$3"} SUTANDO_PY="$PY" PATH="$d/shims:$PATH" SUTANDO_GATEWAY_BRIDGE_RESTART_DELAY=1 \
        bash src/launchd/gateway-bridge-wrapper.sh 2>&1 ) | grep -E 'BRIDGE_STARTED|nothing to run' | tail -1
  rm -rf "$d"
}

# 1. The defect: token in the sibling, `.env` present but blank.
out="$(run_case '' 'REMOTE_TASK_TOKEN=sibling-token
')"
echo "  sibling layout -> $out"
case "$out" in *"token=sibling-token"*) check 0 "token in a sibling file is found" ;;
               *) check 1 "token in a sibling file is found" ;; esac

# 2. Back-compat control: a host whose `.env` carries the token keeps it.
out="$(run_case 'REMOTE_TASK_TOKEN=dot-env-token
' 'REMOTE_TASK_TOKEN=sibling-token
')"
echo "  .env layout    -> $out"
case "$out" in *"token=dot-env-token"*) check 0 ".env keeps precedence when it has the token" ;;
               *) check 1 ".env keeps precedence when it has the token" ;; esac

# 3. Negative control: no token anywhere still stands down cleanly, and this
#    is what makes case 1 a real result rather than a stub that always prints.
out="$(run_case '' '')"
echo "  no token       -> $out"
case "$out" in *"nothing to run"*) check 0 "no token anywhere: unchanged clean stand-down" ;;
               *) check 1 "no token anywhere: unchanged clean stand-down" ;; esac

# 4. Policy in `.env` must survive a token that lives in the sibling. The gate
#    that picks the token file must not become the only file that is sourced.
out="$(run_case 'REMOTE_TASK_TIER=team
' 'REMOTE_TASK_TOKEN=sibling-token
')"
echo "  policy split  -> $out"
case "$out" in *"token=sibling-token"*) check 0 "sibling token is still found when .env holds only policy" ;;
               *) check 1 "sibling token is still found when .env holds only policy" ;; esac
case "$out" in *"tier=team"*) check 0 ".env policy (tier) survives — the access cap is not dropped" ;;
               *) check 1 ".env policy (tier) survives — the access cap is not dropped" ;; esac

# 5. Shell-form credentials in `.env` still work: the wrapper sources that file,
#    so `export KEY=value` is honoured even though a KEY=VALUE reader skips it.
out="$(run_case 'export REMOTE_TASK_TOKEN=exported-token
' '')"
echo "  export form   -> $out"
case "$out" in *"token=exported-token"*) check 0 "export-form token in .env still starts the bridge" ;;
               *) check 1 "export-form token in .env still starts the bridge" ;; esac

# 6. A blank (whitespace-only) `.env` value is no token by the shared rule: the
#    sibling's real token is used, and with no sibling the wrapper stands down
#    instead of launching a bridge that would exit for lack of a token.
out="$(run_case 'REMOTE_TASK_TOKEN="   "
' 'REMOTE_TASK_TOKEN=sibling-token
')"
echo "  blank .env    -> $out"
case "$out" in *"token=sibling-token"*) check 0 "blank .env token falls through to the sibling" ;;
               *) check 1 "blank .env token falls through to the sibling" ;; esac
out="$(run_case 'REMOTE_TASK_TOKEN="   "
' '')"
echo "  blank, alone  -> $out"
case "$out" in *"nothing to run"*) check 0 "blank token and no sibling: clean stand-down" ;;
               *) check 1 "blank token and no sibling: clean stand-down" ;; esac

# 7. A token the process environment supplied is not replaced by a sibling's.
out="$(run_case '' 'REMOTE_TASK_TOKEN=sibling-token
' 'REMOTE_TASK_TOKEN=env-token')"
echo "  env token     -> $out"
case "$out" in *"token=env-token"*) check 0 "environment-provided token keeps precedence over a sibling" ;;
               *) check 1 "environment-provided token keeps precedence over a sibling" ;; esac

# 8. The resolver reads the export form too, so `.env` keeps precedence over a sibling.
out="$(run_case 'export REMOTE_TASK_TOKEN=exported-token
' 'REMOTE_TASK_TOKEN=sibling-token
')"
echo "  export + sib  -> $out"
case "$out" in *"token=exported-token"*) check 0 "export-form .env token outranks a sibling" ;;
               *) check 1 "export-form .env token outranks a sibling" ;; esac

# 9. Homeserver-agnostic: the pick is by content, so the server the token is for
#    (named only by REMOTE_TASK_URL here) cannot change which file is used.
for host in alpha.example.invalid beta.example.invalid gamma.example.invalid; do
  out="$(run_case "MATRIX_HOMESERVER=https://$host
" "REMOTE_TASK_URL=https://$host/relay
REMOTE_TASK_TOKEN=token-for-$host
")"
  case "$out" in *"token=token-for-$host"*) check 0 "sibling token found for a $host lane" ;;
                 *) check 1 "sibling token found for a $host lane" ;; esac
done

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
