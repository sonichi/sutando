#!/usr/bin/env bash
# smoke-bundle.sh — CI guard for `build:bundle`.
#
# Catches the failure mode that only bites at RELEASE otherwise: someone adds a
# dependency or a dynamic import, `npm run build:bundle` still "succeeds", but the
# artifact throws at load under plain node (e.g. "Dynamic require of X is not
# supported", a missing dynamic-import target, or a node-version builtin gap).
# `node --check` only parses — it does NOT run top-level code, so it misses that
# class. This script runs each artifact far enough to evaluate its module graph
# and fails on any load-time error signature.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "── build:bundle ──"
npm run build:bundle

echo "── parse check (node --check) ──"
for f in dist/*.js; do node --check "$f" && echo "  ✓ parse $(basename "$f")"; done

# Portable capped run: prints the process output; exit status is the child's,
# or 124 if we had to kill it after CAP seconds (i.e. it was still running — a
# server that started and stayed up). Uses `timeout` when present (CI/Linux),
# else a bash background+kill fallback (macOS has no `timeout`).
CAP=8
run_capped() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "$CAP" node "$1" 2>&1; return $?
  fi
  local out rc
  out="$(node "$1" 2>&1 & _p=$!; ( sleep "$CAP"; kill "$_p" 2>/dev/null ) & _w=$!; wait "$_p" 2>/dev/null; rc=$?; kill "$_w" 2>/dev/null; exit $rc)"
  rc=$?
  printf '%s' "$out"
  # bash `kill` on a still-running node makes wait return 143 (128+SIGTERM); map to 124
  [ "$rc" = 143 ] && return 124
  return $rc
}

echo "── runtime load smoke (each artifact must evaluate its module graph cleanly) ──"
# A bundle-load failure surfaces during top-level evaluation (dotenv's require,
# skill-loader dynamic imports, node:sqlite builtin, etc.). We run each artifact
# briefly and FAIL only on a load-error signature — a clean start (service binds
# a port / reaches a config gate) or a fast clean exit both PASS.
# FATAL = errors that actually KILL the process (a broken bundle / bad interop /
# wrong node target). NOT included: bare "Cannot find module" — the skill-loader
# tolerates dynamic-import misses (manifest-skill tools.ts reaching into src/*.js)
# by catching them and continuing; those are logged warnings, the service still
# starts, and treating them as fatal false-fails a working bundle.
LOAD_ERR='Dynamic require of| is not supported|ERR_UNKNOWN_BUILTIN_MODULE|SyntaxError|ReferenceError: (require|__dirname|__filename|import)|Cannot use import statement'
# Artifacts that are LIBRARIES, not services. They are built for the browser and
# loaded by a <script> tag, so "exited with no output" is correct behaviour for
# them, not the entrypoint-guard bug the rc=0-with-no-output check exists to
# catch. They still get the parse check above and the load-error check below —
# only the must-start assertion is skipped.
NON_SERVICE='^web-voice-transport\.browser\.js$'

fail=0
for f in dist/*.js; do
  name="$(basename "$f")"
  set +e
  out="$(run_capped "$f")"; rc=$?
  set -e
  # (1) load-time error → the bundle is broken (dep/dynamic-import can't be bundled).
  if printf '%s' "$out" | grep -qE "$LOAD_ERR"; then
    echo "  ✗ SMOKE FAIL: $name — bundle load error:"
    printf '%s\n' "$out" | grep -E "$LOAD_ERR" | head -3
    printf '    (last lines)\n'; printf '%s\n' "$out" | tail -6 | sed 's/^/    /'
    fail=1
    continue
  fi
  # (2) silent no-op → the entrypoint LOADED but never STARTED. rc 124 = timeout
  # killed a still-running service (it started, good). Any other exit with EMPTY
  # output means it fell straight through without binding/logging — the exact
  # class of the isMain-mismatch bug (entrypoint guard matches .ts, not the
  # bundled .js). A real startup or config-gate always prints something.
  if printf '%s' "$name" | grep -qE "$NON_SERVICE"; then
    echo "  ✓ load $name (browser library — parse + load checked, not expected to start)"
    continue
  fi
  if [ "$rc" != 124 ] && [ -z "$(printf '%s' "$out" | tr -d '[:space:]')" ]; then
    echo "  ✗ SMOKE FAIL: $name — exited (rc=$rc) with NO output: loaded but did not start."
    echo "    Likely an entrypoint guard that matches .ts but not the bundled .js."
    fail=1
    continue
  fi
  [ "$rc" = 124 ] && echo "  ✓ start $name (still running at timeout — bound/looping)" \
                  || echo "  ✓ start $name (reached a startup/config gate; rc=$rc)"
done

# Positive bind assertion for web-client (design: wu-air's verify-services exit
# test). web-client is the one service that binds a fixed port (:8080) with ZERO
# env/secrets — so CI can confirm it doesn't just "stay alive" but actually
# SERVES. Stronger than the generic still-running check for this entrypoint.
if [ -f dist/web-client.js ]; then
  echo "── web-client positive bind (:8080, zero-env) ──"
  node dist/web-client.js >/tmp/wc-smoke.log 2>&1 &
  wc_pid=$!
  bound=0
  for _ in $(seq 1 20); do
    if curl -sf -o /dev/null http://localhost:8080/ 2>/dev/null; then bound=1; break; fi
    kill -0 "$wc_pid" 2>/dev/null || break   # process died — stop polling
    sleep 0.5
  done
  kill "$wc_pid" 2>/dev/null || true; wait "$wc_pid" 2>/dev/null || true
  if [ "$bound" = 1 ]; then
    echo "  ✓ web-client served :8080 under plain node (no env)"
  else
    echo "  ✗ SMOKE FAIL: web-client did not serve :8080 within 10s"
    tail -8 /tmp/wc-smoke.log | sed 's/^/    /'
    fail=1
  fi
fi

# ── entrypoint guard under a SPACED path ────────────────────────────────────
# The bundled install lives under "~/Library/Application Support/…"; every dev
# checkout does not. An entrypoint guard comparing `import.meta.url` (which
# percent-encodes) against a raw argv path is therefore false ONLY in
# production, and the process exits 0 with no output — it has shipped three
# times that way. This runs the real artifact from a directory whose name
# contains a space and asserts the OBSERVABLE effect, not the source text: a
# source-shape assertion passes for any guard that merely looks plausible.
#
# CI-only, same reason as smoke-startup-gate.sh case 3: a passing guard means
# the entrypoint actually runs, which for emit-call-tiers means writing into a
# workspace. Gated so it never fires against a developer's live install.
if [ "${SMOKE_SPACED_ENTRYPOINT:-0}" = "1" ] && [ -f dist/emit-call-tiers.js ]; then
  echo "── entrypoint guard: spaced path (CI-only) ──"
  _sp_root="$(mktemp -d)"
  _sp_dir="$_sp_root/Application Support"
  mkdir -p "$_sp_dir"
  cp dist/emit-call-tiers.js "$_sp_dir/"
  # Redirect the workspace so the run cannot touch anything real. The resolver
  # honours SUTANDO_WORKSPACE only under SUTANDO_TEST_MODE=1.
  _sp_ws="$_sp_root/ws"; mkdir -p "$_sp_ws/state"
  ( cd "$_sp_dir" && SUTANDO_WORKSPACE="$_sp_ws" SUTANDO_TEST_MODE=1 node ./emit-call-tiers.js ) \
    > "$_sp_root/out.log" 2>&1 || true
  if [ -s "$_sp_root/out.log" ] && grep -q "call-tiers written" "$_sp_root/out.log"; then
    echo "  ✓ entrypoint ran from a path containing a space"
  else
    echo "  ✗ SMOKE FAIL: entrypoint did NOT run from a spaced path (guard is false in production)"
    echo "    expected 'call-tiers written' on stdout; got:"
    sed 's/^/      /' "$_sp_root/out.log" | head -5
    fail=1
  fi
  rm -rf "$_sp_root"
fi

if [ "$fail" -ne 0 ]; then
  echo "bundle smoke FAILED — an artifact breaks at load. A dependency or dynamic import likely can't be bundled."
  exit 1
fi
echo "bundle smoke OK — all artifacts build + load under plain node."
