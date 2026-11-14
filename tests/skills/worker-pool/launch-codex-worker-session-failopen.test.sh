#!/bin/bash
# The Codex worker launcher's observer is fail-open: whatever happens to the observer, the worker
# session starts and the supervisor runs exactly as before. tmux, codex, node and the repo's helper
# scripts are stubs; the stub tmux records sessions as files and RUNS the observer's command.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  ok  $2"; pass=$((pass+1)); else echo "  FAIL $2"; fail=$((fail+1)); fi; }
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

# --- a fake repo: the real launcher, stub helpers -----------------------------
FAKE="$TMP/repo"
mkdir -p "$FAKE/skills/worker-pool/scripts" "$FAKE/scripts" "$FAKE/src/agent/codex/cli" "$FAKE/skills/proactive-loop/scripts"
for f in launch-codex-worker-session.sh worker-mcp-credential.sh codex_worker_mcp.py; do
  cp "$REPO/skills/worker-pool/scripts/$f" "$FAKE/skills/worker-pool/scripts/"
done
cp "$REPO/src/agent/launch-path.sh" "$FAKE/src/agent/"
# The Codex home comes from its one owner, which reads the real config module.
mkdir -p "$FAKE/src/agent/codex"
cp "$REPO/src/agent/codex/codex-home.sh" "$FAKE/src/agent/codex/"
cp "$REPO/src/codex_home.py" "$REPO/src/codex_host_instructions.py" "$REPO/src/sutando_config.py" "$FAKE/src/"
touch "$FAKE/src/__init__.py"
printf '{"workspace":{"path":"%s/ws"},"core_config_dirs":[{"id":"x","type":"codex","env_name":"CODEX_HOME","synced":false,"value":"%s/codex"}]}\n' "$TMP" "$TMP" > "$FAKE/sutando.config.json"
printf '#!/bin/bash\necho ""\n' > "$FAKE/scripts/sutando-config.sh"
printf 'require_python() { printf "%%s\\n" "$SUTANDO_PY"; }\nresolve_python() { printf "%%s\\n" "$SUTANDO_PY"; }\n' > "$FAKE/scripts/python-binary.sh"
printf '#!/bin/bash\ntouch "$TEST_TMP/supervisor-ran"\n' > "$FAKE/skills/worker-pool/scripts/worker-watcher-supervisor.sh"
printf '// stub observer: never run directly; the node stub decides the exit code\n' > "$FAKE/src/agent/codex/cli/codex-observer.mjs"
chmod +x "$FAKE"/scripts/*.sh "$FAKE"/skills/worker-pool/scripts/*.sh

# --- stubs on PATH ------------------------------------------------------------
mkdir -p "$TMP/bin" "$TMP/sessions"
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/python3"   # the Darwin reset-timer call, when it runs
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/codex"
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/fswatch"   # the launcher's precondition; never run here
printf '#!/bin/sh\nexit 1\n' > "$TMP/bin/node"       # the observer crashes at once
cat > "$TMP/bin/tmux" <<'TMUX'
#!/bin/bash
# tmux -S sock new-session -d -s NAME [-e K=V]... CMD...   | has-session -t =NAME | kill-session -t =NAME
shift 2
cmd="$1"; shift
case "$cmd" in
  new-session)
    name=""; while [ $# -gt 0 ]; do
      case "$1" in -d) shift ;; -s) name="$2"; shift 2 ;; -e) shift 2 ;; *) break ;; esac
    done
    case "$name" in
      *-observer)
        [ -n "${TMUX_STUB_REFUSE_OBSERVER:-}" ] && exit 1
        touch "$TEST_TMP/sessions/$name"
        ( "$@" >/dev/null 2>&1; echo $? > "$TEST_TMP/observer.rc" ) &
        exit 0 ;;
      *) touch "$TEST_TMP/sessions/$name"; exit 0 ;;
    esac ;;
  has-session) n="${2#=}"; [ -f "$TEST_TMP/sessions/$n" ] ;;
  kill-session) n="${2#=}"; rm -f "$TEST_TMP/sessions/$n"; exit 0 ;;
  *) exit 0 ;;
esac
TMUX
chmod +x "$TMP"/bin/*
STUB_PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin"
SEAT=0123456789abcdef0123456789abcdef

launch() {  # launch <instance id> [extra env...] -> exit code; stderr in $TMP/err
  local id="$1"; shift
  rm -rf "$TMP/sessions" "$TMP/supervisor-ran" "$TMP/observer.rc"; mkdir -p "$TMP/sessions"
  env -i HOME="$TMP" PATH="$STUB_PATH" TEST_TMP="$TMP" SUTANDO_PY="$TMP/bin/python3" \
      SUTANDO_INSTANCE_ID="$id" SUTANDO_TMUX_SESSION="sutando-worker-$id" SUTANDO_TMUX_SOCKET="$TMP/sock" \
      SUTANDO_TASKS_DIR="$TMP/inbox" SUTANDO_WORKSPACE_DIR="$TMP/ws" SUTANDO_RESULTS_DIR="$TMP/ws/results" \
      SUTANDO_INBOX_RESOLVER=/bin/true SUTANDO_POOL_DELIVERY_SCRIPT=/bin/true SUTANDO_CODEX_WORKING_DIR="$TMP/wd" "$@" \
      bash "$FAKE/skills/worker-pool/scripts/launch-codex-worker-session.sh" >/dev/null 2>"$TMP/err"
}
worker_ok() { [ -f "$TMP/sessions/sutando-worker-$1" ] && [ -f "$TMP/supervisor-ran" ]; }

# --- 1. the observer crashes at once: the worker starts and the supervisor runs ---
launch "$SEAT"; rc=$?
sleep 0.5
check "$rc" "observer exits 1: the launcher still exits 0"
worker_ok "$SEAT"; check $? "observer exits 1: the worker session is up and the supervisor ran"
[ -f "$TMP/sessions/sutando-worker-$SEAT-observer" ] && [ "$(cat "$TMP/observer.rc" 2>/dev/null)" = 1 ]
check $? "the observer was started for the worker's session and did crash (exit 1)"

# --- 2. tmux refuses the observer session: logged, worker unaffected ----------
launch "$SEAT" TMUX_STUB_REFUSE_OBSERVER=1; rc=$?
check "$rc" "tmux refuses the observer: the launcher still exits 0"
worker_ok "$SEAT"; check $? "tmux refuses the observer: the worker session is up and the supervisor ran"
grep -q "observer for sutando-worker-$SEAT did not start" "$TMP/err"; check $? "tmux refuses the observer: the launcher says so"

# --- 3. an instance id that is not a 32-hex seat: no observer, logged, worker unaffected ---
launch "w-test"; rc=$?
check "$rc" "bad seat id: the launcher still exits 0"
worker_ok "w-test"; check $? "bad seat id: the worker session is up and the supervisor ran"
[ ! -f "$TMP/sessions/sutando-worker-w-test-observer" ]; check $? "bad seat id: no observer session was spawned"
grep -q "not a 32-hex seat" "$TMP/err"; check $? "bad seat id: the launcher says so"

# --- 4. no node on PATH: no observer, logged, worker unaffected ----------------
mv "$TMP/bin/node" "$TMP/node.off"
launch "$SEAT"; rc=$?
check "$rc" "no node: the launcher still exits 0"
worker_ok "$SEAT"; check $? "no node: the worker session is up and the supervisor ran"
[ ! -f "$TMP/sessions/sutando-worker-$SEAT-observer" ]; check $? "no node: no observer session was spawned"
grep -q "no node; no observer" "$TMP/err"; check $? "no node: the launcher says so"
mv "$TMP/node.off" "$TMP/bin/node"

echo "---"
if [ "$fail" -gt 0 ]; then echo "FAILED — $fail of $((pass+fail)) checks"; exit 1; fi
echo "PASS — $pass checks green"
