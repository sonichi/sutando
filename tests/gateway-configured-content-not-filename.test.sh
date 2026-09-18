#!/bin/bash
# "Is the ag2.space gateway configured on this host?" is decided in three
# places — start_gateway_lanes() in src/startup-runtime.sh, and
# _gateway_configured() in src/health-check.py and src/runtime-health.py.
# Each used to read only channels/ag2space/.env by NAME, so a host whose token
# lives in a sibling while `.env` holds another channel's creds never started
# the bridge AND had both health surfaces report the outage as
# correctly-not-running. All three now delegate to src/channel_env_resolve.py.
#
# Hermetic: an isolated repo with a stub bridge and NO launchd installer, so
# the launchd branch is unreachable and a live job can never be touched.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
. "$REPO/scripts/python-binary.sh"
PY="$(require_python "$REPO" "run the gateway configuredness test")" || exit 1

MATRIX_ENV='MATRIX_HOMESERVER=https://example.invalid
MATRIX_ACCESS_TOKEN=matrix-only-not-a-relay-token
'
SIBLING='REMOTE_TASK_TOKEN=sibling-token
'

mkfix() {  # $1=channels root  $2=.env body  $3=sibling body
  mkdir -p "$1/channels/ag2space"
  printf '%s' "$2" > "$1/channels/ag2space/.env"
  printf '%s' "$3" > "$1/channels/ag2space/relay-client.env"
}

# start_gateway_lanes() is SOURCED from the real file — never copied — and run
# against a throwaway $REPO so nothing it launches is real.
drive_startup() {  # $1=.env body  $2=sibling body  $3=PY ("" exercises the degrade path)
  d=$(mktemp -d)
  mkdir -p "$d/repo/scripts" "$d/repo/src" "$d/home" "$d/logs"
  cp "$REPO/scripts/python-binary.sh" "$REPO/scripts/channel-env.sh" "$d/repo/scripts/"
  cp "$REPO/src/channel_env_resolve.py" "$REPO/src/channel_env_containment.py" \
     "$REPO/src/channel_token.py" "$d/repo/src/"
  printf '#!/bin/bash\nif [ "${1:-}" = "claude-home-path" ]; then\n  if [ -n "${2:-}" ]; then echo "%s/home/$2"; else echo "%s/home"; fi\n  exit 0\nfi\nexit 0\n' \
      "$d" "$d" > "$d/repo/scripts/sutando-config.sh"; chmod +x "$d/repo/scripts/sutando-config.sh"
  printf 'import os\nprint("BRIDGE_STARTED token=%%s tier=%%s" %% (os.environ.get("REMOTE_TASK_TOKEN") or "<none>", os.environ.get("REMOTE_TASK_TIER") or "<none>"))\n' \
      > "$d/repo/src/remote-gateway-bridge.py"
  mkfix "$d/home" "$1" "$2"
  env -i PATH=/usr/bin:/bin HOME="$d" SUTANDO_PY="$3" \
    bash -c '
      set -uo pipefail
      source "'"$REPO"'/src/startup-runtime.sh" >/dev/null 2>&1
      REPO="'"$d"'/repo"; PY="'"$3"'"; LOGS_DIR="'"$d"'/logs"
      start_gateway_lanes
      wait
      cat "$LOGS_DIR/remote-gateway-bridge.log" 2>/dev/null
    ' 2>&1 | tr '\n' '|'
  rm -rf "$d"
}

# Both health predicates, in one interpreter, against the same fixture.
drive_health() {  # $1=.env body  $2=sibling body -> "health=<v> runtime=<v>"
  d=$(mktemp -d); mkfix "$d" "$1" "$2"
  env -i PATH=/usr/bin:/bin HOME="$d" CLAUDE_CONFIG_DIR="$d" \
    "$PY" - "$REPO" <<'PYEOF' 2>&1
import importlib.util, os, sys
repo = sys.argv[1]
sys.path.insert(0, os.path.join(repo, "src"))
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod
hc = load("health_check_mod", os.path.join(repo, "src", "health-check.py"))
rh = load("runtime_health_mod", os.path.join(repo, "src", "runtime-health.py"))
print("health=%r runtime=%r" % (hc._gateway_configured(), rh._gateway_configured()))
PYEOF
  rm -rf "$d"
}

# ── 1. The defect: token in a sibling, `.env` holds another channel's creds ──
out="$(drive_startup "$MATRIX_ENV" "$SIBLING" "$PY")"
echo "  sibling layout   -> ${out:-<no output>}"
case "$out" in *"token=sibling-token"*) check 0 "startup starts the bridge on the sibling token" ;;
               *) check 1 "startup starts the bridge on the sibling token" ;; esac
out="$(drive_health "$MATRIX_ENV" "$SIBLING")"
echo "  sibling layout   -> $out"
case "$out" in "health=True runtime=True") check 0 "both health surfaces report configured" ;;
               *) check 1 "both health surfaces report configured" ;; esac

# ── 2. A present-but-EMPTY value must not count — the prefix-only grep's own
#      defect, and what made `.env` win over the sibling holding the real token.
BLANK='REMOTE_TASK_TOKEN=
'
out="$(drive_startup "$BLANK" "$SIBLING" "$PY")"
echo "  blank .env       -> ${out:-<no output>}"
case "$out" in *"token=sibling-token"*) check 0 "a blank REMOTE_TASK_TOKEN= does not shadow the sibling" ;;
               *) check 1 "a blank REMOTE_TASK_TOKEN= does not shadow the sibling" ;; esac

# ── 3. `.env` POLICY survives a token that lives elsewhere: the file that
#      supplies the token must not become the only file sourced.
out="$(drive_startup 'REMOTE_TASK_TIER=team
' "$SIBLING" "$PY")"
echo "  policy split     -> ${out:-<no output>}"
case "$out" in *"tier=team"*) check 0 ".env tier cap survives a sibling-held token" ;;
               *) check 1 ".env tier cap survives a sibling-held token" ;; esac

# ── 4. Back-compat control: a host whose `.env` HAS the token is unchanged.
out="$(drive_startup 'REMOTE_TASK_TOKEN=dot-env-token
' "$SIBLING" "$PY")"
echo "  .env layout      -> ${out:-<no output>}"
case "$out" in *"token=dot-env-token"*) check 0 ".env keeps precedence when it holds a token" ;;
               *) check 1 ".env keeps precedence when it holds a token" ;; esac
out="$(drive_health 'REMOTE_TASK_TOKEN=dot-env-token
' '')"
case "$out" in "health=True runtime=True") check 0 "health surfaces unchanged on the .env layout" ;;
               *) check 1 "health surfaces unchanged on the .env layout" ;; esac

# ── 5. Negative control. Without this, every check above would pass against a
#      predicate hardwired to True and a stub that always prints.
out="$(drive_startup "$MATRIX_ENV" '' "$PY")"
echo "  no token         -> ${out:-<no output>}"
case "$out" in *BRIDGE_STARTED*) check 1 "no token anywhere: startup stays silent" ;;
               *) check 0 "no token anywhere: startup stays silent" ;; esac
out="$(drive_health "$MATRIX_ENV" '')"
case "$out" in "health=False runtime=False") check 0 "no token anywhere: both report unconfigured" ;;
               *) check 1 "no token anywhere: both report unconfigured" ;; esac

# ── 6. Degrade path: with no runnable interpreter the resolver cannot run, and
#      a configured host must still reach the explicit skip rather than read as
#      unconfigured (which would hide the reason from the operator).
out="$(drive_startup 'REMOTE_TASK_TOKEN=dot-env-token
' '' "")"
echo "  no python3       -> ${out:-<no output>}"
case "$out" in *"no runnable python3"*) check 0 "unrunnable resolver degrades to the legacy gate and says why" ;;
               *) check 1 "unrunnable resolver degrades to the legacy gate and says why" ;; esac

# ── 7. A non-UTF-8 byte in `.env` must not raise out of the shared reader: a
#      decode error escaping health-check's OSError guard would crash the probe.
d=$(mktemp -d); mkdir -p "$d/channels/ag2space"
printf 'MATRIX_ACCESS_TOKEN=\xff\xfe\n' > "$d/channels/ag2space/.env"
printf '%s' "$SIBLING" > "$d/channels/ag2space/relay-client.env"
out="$(env -i PATH=/usr/bin:/bin HOME="$d" "$PY" -c '
import sys; sys.path.insert(0, sys.argv[1])
from channel_env_resolve import resolve_channel_env
print(resolve_channel_env(sys.argv[2] + "/channels", "ag2space"))' "$REPO/src" "$d" 2>&1)"
echo "  undecodable .env -> $out"
case "$out" in *relay-client.env) check 0 "an undecodable .env is skipped, not raised" ;;
               *) check 1 "an undecodable .env is skipped, not raised" ;; esac
rm -rf "$d"

echo
if [ "$fail" -eq 0 ]; then echo "PASS — $pass checks green"; else echo "FAIL — $fail failed, $pass passed"; exit 1; fi
