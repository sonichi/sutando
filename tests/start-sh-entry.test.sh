#!/usr/bin/env bash
# start.sh is the one-command front door. These exercise the SHIPPED file —
# a stubbed src/startup.sh in a temp repo, so the dispatch under test is real.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fails=0
check() { if [ "$1" = "0" ]; then echo "PASS  $2"; else echo "FAIL  $2"; fails=$((fails+1)); fi; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/src"
cp "$REPO/start.sh" "$TMP/start.sh"
cat > "$TMP/src/startup.sh" <<'STUB'
#!/usr/bin/env bash
echo "STARTUP_ARGS: $*"
STUB
chmod +x "$TMP/start.sh" "$TMP/src/startup.sh"

out="$(SUTANDO_OPEN_DASHBOARD=0 bash "$TMP/start.sh" 2>&1)"
[ "$out" = "STARTUP_ARGS: --with-app" ]; check $? "delegates to src/startup.sh with --with-app"

out="$(SUTANDO_OPEN_DASHBOARD=0 bash "$TMP/start.sh" --runtime codex 2>&1)"
[ "$out" = "STARTUP_ARGS: --with-app --runtime codex" ]; check $? "extra args pass through after --with-app"

# The repo root must come from BASH_SOURCE, not the caller's cwd: an OSS user
# running `~/sutando/start.sh` from elsewhere must still find its own startup.sh.
out="$(cd / && SUTANDO_OPEN_DASHBOARD=0 bash "$TMP/start.sh" 2>&1)"
[ "$out" = "STARTUP_ARGS: --with-app" ]; check $? "works when invoked from an unrelated cwd"

# The browser open must never gate the core: an unreachable URL must not delay it.

# Redirect to a file, not $(...): the backgrounded poller inherits stdout and
# would block the substitution for the whole poll window.
start=$(date +%s)
SUTANDO_DASHBOARD_URL=http://127.0.0.1:1 bash "$TMP/start.sh" > "$TMP/out.txt" 2>&1 &
runner=$!
for _ in $(seq 1 20); do grep -q "STARTUP_ARGS" "$TMP/out.txt" 2>/dev/null && break; sleep 0.5; done
elapsed=$(( $(date +%s) - start ))
pkill -P "$runner" 2>/dev/null; kill "$runner" 2>/dev/null; wait "$runner" 2>/dev/null
grep -q "STARTUP_ARGS: --with-app" "$TMP/out.txt"; check $? "unreachable dashboard does not block startup"
[ "$elapsed" -lt 10 ]; check $? "startup dispatched without waiting on the dashboard poll (${elapsed}s)"

grep -qE '^\s*(exec )?open ' "$REPO/start.sh" && grep -q 'SUTANDO_OPEN_DASHBOARD' "$REPO/start.sh"
check $? "browser open is guarded by SUTANDO_OPEN_DASHBOARD"

! grep -qE '/Users/[a-z]+/|/home/[a-z]+/' "$REPO/start.sh"; check $? "no hardcoded host paths"

echo
[ "$fails" -eq 0 ] && echo "all start.sh assertions passed" || { echo "$fails FAILED"; exit 1; }
