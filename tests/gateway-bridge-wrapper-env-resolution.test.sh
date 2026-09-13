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
run_case() {  # $1=.env body  $2=relay-client.env body -> prints the stub's line
  d=$(mktemp -d); mkdir -p "$d/src/launchd" "$d/scripts" "$d/home/channels/ag2space"
  cp "$REPO/src/launchd/gateway-bridge-wrapper.sh" "$d/src/launchd/"
  cp "$REPO/scripts/channel-env.sh" "$REPO/scripts/python-binary.sh" "$d/scripts/"
  # the resolver plus the two modules it imports — a real resolution, not a stub
  mkdir -p "$d/src"
  cp "$REPO/src/channel_env_resolve.py" "$REPO/src/channel_env_containment.py" \
     "$REPO/src/channel_token.py" "$d/src/"
  # Faithful to the real helper: `claude-home-path [suffix]` appends the suffix,
  # so the pre-fix wrapper's own lookup works here and case 2 is a real control.
  printf '#!/bin/bash\nif [ "${1:-}" = "claude-home-path" ]; then\n  if [ -n "${2:-}" ]; then echo "%s/home/$2"; else echo "%s/home"; fi\n  exit 0\nfi\nexit 0\n' \
      "$d" "$d" > "$d/scripts/sutando-config.sh"; chmod +x "$d/scripts/sutando-config.sh"
  printf 'import os\nprint("BRIDGE_STARTED token=%%s" %% (os.environ.get("REMOTE_TASK_TOKEN") or "<none>"))\n' \
      > "$d/src/remote-gateway-bridge.py"
  printf '%s' "$1" > "$d/home/channels/ag2space/.env"
  printf '%s' "$2" > "$d/home/channels/ag2space/relay-client.env"
  ( cd "$d" && SUTANDO_PY="$PY" bash src/launchd/gateway-bridge-wrapper.sh 2>&1 ) | tail -1
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

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
