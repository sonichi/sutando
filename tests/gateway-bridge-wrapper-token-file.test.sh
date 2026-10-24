#!/usr/bin/env bash
# gateway-bridge-wrapper.sh must hand the bridge REMOTE_TASK_TOKEN_FILE pointing at
# the channel .env it sourced, so the bridge can re-read a rotated token on 401.
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

( cd "$d"; env -u AG2_REMOTE_TOKEN -u REMOTE_TASK_TOKEN -u REMOTE_TASK_TOKEN_FILE \
    PATH="$d/shims:$PATH" bash src/launchd/gateway-bridge-wrapper.sh > o.log 2>&1 )
got=$(grep '^REMOTE_TASK_TOKEN_FILE=' "$d/o.log" | head -1 | cut -d= -f2-)
echo "sourced env file: \$TMP${ENV_FILE#"$d"}"
shown="${got:-<no output>}"; [ "${got#"$d"}" != "$got" ] && shown="\$TMP${got#"$d"}"
echo "bridge saw REMOTE_TASK_TOKEN_FILE: $shown"
if [ "$got" = "$ENV_FILE" ]; then
  echo "PASS: the bridge can re-read a rotated token from the file the wrapper sourced"
  exit 0
fi
echo "FAIL: REMOTE_TASK_TOKEN_FILE does not point at the sourced channel .env"
exit 1
