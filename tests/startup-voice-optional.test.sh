#!/bin/bash
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

run_runtime_config() {
  local gemini_key="${1:-}"
  env -i PATH="/usr/bin:/bin" GEMINI_API_KEY="$gemini_key" \
    bash -c 'cd "$1"; source "$2/src/startup-runtime.sh"; configure_startup_runtime; printf "SKIP_VOICE=%s\n" "${SKIP_VOICE:-0}"' \
    _ "$TMP" "$REPO"
}

without_key="$(run_runtime_config)"
grep -q 'credential-free services' <<<"$without_key"
grep -q 'voice agent disabled' <<<"$without_key"
grep -q 'SKIP_VOICE=1' <<<"$without_key"

with_key="$(run_runtime_config test-key)"
grep -q 'SKIP_VOICE=0' <<<"$with_key"
if grep -q 'voice agent disabled' <<<"$with_key"; then
  echo "voice was disabled despite configured credentials" >&2
  exit 1
fi

# Exercise verify-setup rather than inspecting its source. Stub only external
# prerequisites; the actual verifier resolves the selected runtime and auth.
BIN="$TMP/bin"
mkdir -p "$BIN" "$TMP/repo/src" "$TMP/repo/scripts" "$TMP/repo/node_modules"
cp "$REPO/src/verify-setup.sh" "$TMP/repo/src/"
cp "$REPO/scripts/sutando-config.sh" "$TMP/repo/scripts/"
cp "$REPO/src/sutando_config.py" "$TMP/repo/src/"
touch "$TMP/repo/src/__init__.py"
printf '{"core":{"runtime":"codex"}}\n' > "$TMP/repo/sutando.config.json"
for file in voice-agent.ts task-bridge.ts web-client.ts health-check.py agent-api.py dashboard.py; do
  touch "$TMP/repo/src/$file"
done
touch "$TMP/repo/CLAUDE.md"
for command in fswatch lsof; do
  printf '#!/bin/bash\nexit 0\n' > "$BIN/$command"
  chmod +x "$BIN/$command"
done
cat > "$BIN/node" <<'EOF'
#!/bin/bash
echo v22.0.0
EOF
cat > "$BIN/codex" <<'EOF'
#!/bin/bash
[ "${1:-}" = login ] && [ "${2:-}" = status ]
EOF
chmod +x "$BIN/node" "$BIN/codex"

verify_output="$(cd "$TMP/repo" && env -i HOME="$TMP/home" PATH="$BIN:/usr/bin:/bin" bash src/verify-setup.sh || true)"
grep -q '✓ codex core CLI' <<<"$verify_output"
grep -q '.env file missing (optional for text-only + core operation)' <<<"$verify_output"
if grep -q 'GEMINI_API_KEY.*✗\|✗.*GEMINI_API_KEY' <<<"$verify_output"; then
  echo "verify-setup treated optional Gemini credentials as a failure" >&2
  exit 1
fi

echo "PASS: startup and verifier allow credential-free Codex core operation"
