#!/usr/bin/env bash
# gateway-bridge-wrapper.sh must hand the bridge REMOTE_TASK_TOKEN_FILE pointing at
# the file that holds its token, so the bridge can re-read a rotated token on 401.
#
# Run: bash tests/gateway-bridge-wrapper-token-file.test.sh
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
. "$REPO/scripts/python-binary.sh"
PY="$(require_python "$REPO" "run the gateway wrapper token-file test")" || exit 1

d=$(mktemp -d); trap 'rm -rf "$d"' EXIT
mkdir -p "$d/src/launchd" "$d/scripts" "$d/ws" "$d/shims" "$d/channels/ag2space"
ENV_FILE="$d/channels/ag2space/.env"
printf 'REMOTE_TASK_TOKEN=fake-test-token\n' > "$ENV_FILE"
cat > "$d/scripts/sutando-config.sh" <<EOS
#!/bin/bash
case "\${1:-}" in workspace) echo "$d/ws" ;; *) echo "$ENV_FILE" ;; esac
EOS
chmod +x "$d/scripts/sutando-config.sh"
# Stub bridge: report only whether the variable is set and its path, then stand down (rc 75).
cat > "$d/src/remote-gateway-bridge.py" <<'EOS'
import os, sys
v = os.environ.get("REMOTE_TASK_TOKEN_FILE")
print("REMOTE_TASK_TOKEN_FILE=" + (v if v else "<unset>"))
sys.exit(75)
EOS
cp "$REPO/src/launchd/gateway-bridge-wrapper.sh" "$d/src/launchd/"
printf '#!/bin/bash\nexec "%s" "$@"\n' "$PY" > "$d/shims/python3"
printf '#!/bin/bash\nexit 0\n' > "$d/shims/osascript"
chmod +x "$d/shims/python3" "$d/shims/osascript"

run_wrapper() {
  ( cd "$d"; env -u AG2_REMOTE_TOKEN -u REMOTE_TASK_TOKEN -u REMOTE_TASK_TOKEN_FILE \
      PATH="$d/shims:$PATH" bash src/launchd/gateway-bridge-wrapper.sh > o.log 2>&1 )
  grep '^REMOTE_TASK_TOKEN_FILE=' "$d/o.log" | head -1 | cut -d= -f2-
}
fail=0
check() {  # <case> <expected file> <got>
  local shown="${3:-<no output>}"; [ "${3#"$d"}" != "$3" ] && shown="\$TMP${3#"$d"}"
  if [ "$3" = "$2" ]; then echo "PASS: $1 (bridge saw $shown)"; else
    echo "FAIL: $1: expected \$TMP${2#"$d"}, bridge saw $shown"; fail=1; fi
}

# Token in the channel .env: the bridge re-reads the .env.
check "token held in the channel .env" "$ENV_FILE" "$(run_wrapper)"

# The shared resolver names a separate token file: the bridge re-reads that file.
TOK_FILE="$d/channels/ag2space/token.env"
printf 'REMOTE_TASK_TIER=owner\n' > "$ENV_FILE"
printf 'REMOTE_TASK_TOKEN=fake-test-token\n' > "$TOK_FILE"
printf '#!/bin/bash\necho "%s"\n' "$TOK_FILE" > "$d/scripts/channel-env.sh"
chmod +x "$d/scripts/channel-env.sh"
check "token held in the file the resolver names" "$TOK_FILE" "$(run_wrapper)"

exit "$fail"
