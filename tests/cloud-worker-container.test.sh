#!/usr/bin/env bash
# Hermetic checks for deploy/cloud-worker: no docker daemon, no network. Every
# script is exercised as shipped (a fake python3 stands in for the interpreter).
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
D="$REPO/deploy/cloud-worker"
fails=0; total=0
ok()  { total=$((total+1)); printf '  ok   %s\n' "$1"; }
bad() { total=$((total+1)); fails=$((fails+1)); printf '  FAIL %s %s\n' "$1" "${2:-}"; }

TMP="$(mktemp -d "${TMPDIR:-/tmp}/cloud-worker-test.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

echo "cloud worker container:"

# ── 1. files + syntax ─────────────────────────────────────────────────────
for f in Dockerfile entrypoint.sh seat-stub.py seat-ag2-assistant.py assistant-bootstrap.sh healthcheck.sh docker-compose.yml .env.example provision.sh deprovision.sh README.md; do
  [ -f "$D/$f" ] && ok "ships $f" || bad "ships $f" "missing"
done
for f in entrypoint.sh healthcheck.sh provision.sh deprovision.sh; do
  bash -n "$D/$f" 2>/dev/null && ok "bash -n $f" || bad "bash -n $f"
done
python3 -m py_compile "$D/seat-stub.py" 2>/dev/null && ok "seat-stub.py compiles" || bad "seat-stub.py compiles"
python3 -m py_compile "$D/seat-ag2-assistant.py" 2>/dev/null && ok "seat-ag2-assistant.py compiles" || bad "seat-ag2-assistant.py compiles"
sh -n "$D/assistant-bootstrap.sh" 2>/dev/null && ok "sh -n assistant-bootstrap.sh (POSIX sh: runs inside the sidecar image)" || bad "sh -n assistant-bootstrap.sh"
grep -q 'profiles.json \] || ag2-assistant profiles create backup' "$D/assistant-bootstrap.sh" && grep -q 'store.set_key(provider' "$D/assistant-bootstrap.sh" \
  && grep -q 'exec ag2-assistant acp-serve --host 0.0.0.0 --port "${AG2ASSISTANT_ACP_PORT:-8802}" --token "$AG2ASSISTANT_ACP_TOKEN"' "$D/assistant-bootstrap.sh" \
  && ok "assistant-bootstrap.sh: first-boot profile, key seed into the secret store, then acp-serve with the token" || bad "assistant-bootstrap.sh contents"

# ── 2. Dockerfile copies only the client's import graph ───────────────────
ALLOW="sutando.config.json
packages/ag2-sparrow/ag2_sparrow/
src/remote-gateway-bridge.py
src/task_archive.py
src/workspace_default.py
src/sutando_config.py
src/util_paths.py
src/proactive_routing.py
src/task_envelope.py
src/git_binary.py
src/result_markers.py
scripts/sutando-config.sh
scripts/python-binary.sh
deploy/cloud-worker/entrypoint.sh
deploy/cloud-worker/seat-stub.py
deploy/cloud-worker/seat-ag2-assistant.py
deploy/cloud-worker/healthcheck.sh"
copies="$(grep -E '^COPY ' "$D/Dockerfile" | sed -E 's/--[a-z]+=[^ ]+ //g' | awk '{ for (i = 2; i < NF; i++) print $i }')"
[ -n "$copies" ] && ok "Dockerfile has COPY lines" || bad "Dockerfile has COPY lines" "none parsed"
while IFS= read -r src; do
  [ -z "$src" ] && continue
  if printf '%s\n' "$ALLOW" | grep -qxF "$src"; then
    [ -e "$REPO/$src" ] && ok "COPY $src is allowlisted and exists" || bad "COPY $src exists in the repo" "missing"
  else
    bad "COPY $src is allowlisted" "not in the allowlist — a new import needs the list updated"
  fi
done <<< "$copies"
grep -Eq '^COPY[^\n]* (\.|src|scripts|skills|workspace|packages)/? ' "$D/Dockerfile" \
  && bad "no wholesale COPY of the repo, src/, scripts/, skills/, workspace/ or packages/" \
  || ok "no wholesale COPY of the repo, src/, scripts/, skills/, workspace/ or packages/"
grep -q '^USER sutando' "$D/Dockerfile" && ok "runs as the non-root user sutando" || bad "runs as the non-root user sutando"
grep -q '^VOLUME \["/workspace"\]' "$D/Dockerfile" && ok "declares the /workspace volume" || bad "declares the /workspace volume"
grep -q '^HEALTHCHECK' "$D/Dockerfile" && grep -q 'healthcheck.sh' "$D/Dockerfile" \
  && ok "HEALTHCHECK runs healthcheck.sh" || bad "HEALTHCHECK runs healthcheck.sh"
grep -q 'entrypoint.sh"\]$' "$D/Dockerfile" && ok "ENTRYPOINT is entrypoint.sh" || bad "ENTRYPOINT is entrypoint.sh"

# ── 3. entrypoint.sh env contract, with a fake python3 ────────────────────
FAKEBIN="$TMP/bin"; mkdir -p "$FAKEBIN"
ENGINE="$TMP/engine"; WS="$TMP/ws"
mkdir -p "$ENGINE/deploy/cloud-worker" "$ENGINE/scripts" "$ENGINE/src"
cp "$D/entrypoint.sh" "$D/seat-stub.py" "$D/seat-ag2-assistant.py" "$D/healthcheck.sh" "$ENGINE/deploy/cloud-worker/"
cp "$REPO/scripts/sutando-config.sh" "$REPO/scripts/python-binary.sh" "$ENGINE/scripts/"
cp "$REPO/src/sutando_config.py" "$ENGINE/src/"
: > "$ENGINE/src/remote-gateway-bridge.py"
# The fake interpreter: `-c` answers the resolver with $FAKE_WS; a script run
# records its env and exits so the entrypoint's child-exit path is reached.
cat > "$FAKEBIN/python3" <<'PY'
#!/usr/bin/env bash
if [ "${1:-}" = "-c" ]; then printf '%s' "${FAKE_WS:-}"; exit 0; fi
env | sort > "${FAKE_ENV_OUT:-/dev/null}.$$"
sleep 0.3
exit 0
PY
chmod +x "$FAKEBIN/python3"
ERR="$TMP/err"; OUT="$TMP/out"
run_entry() {  # run_entry <extra env assignments...> ; prints rc, stderr in $ERR
  env -i PATH="$FAKEBIN:/usr/bin:/bin" HOME="$TMP" SUTANDO_PY="$FAKEBIN/python3" \
      SUTANDO_ENGINE_DIR="$ENGINE" SUTANDO_CLOUD_WORKSPACE="$WS" FAKE_WS="$WS" \
      FAKE_ENV_OUT="$TMP/child-env" "$@" \
      bash "$ENGINE/deploy/cloud-worker/entrypoint.sh" >"$OUT" 2>"$ERR"
  echo $?
}
FULL=(REMOTE_TASK_URL=http://broker.invalid REMOTE_TASK_TOKEN=secret SUTANDO_WORKER_ID=cloud-t)

rc="$(run_entry)"
[ "$rc" = 2 ] && grep -q 'missing required env: REMOTE_TASK_URL REMOTE_TASK_TOKEN SUTANDO_WORKER_ID' "$ERR" \
  && ok "empty env → exit 2 naming all three required vars" || bad "empty env → exit 2 naming all three" "rc=$rc $(cat "$ERR")"
for v in REMOTE_TASK_URL REMOTE_TASK_TOKEN SUTANDO_WORKER_ID; do
  partial=(); for kv in "${FULL[@]}"; do [ "${kv%%=*}" = "$v" ] || partial+=("$kv"); done
  rc="$(run_entry "${partial[@]}")"
  [ "$rc" = 2 ] && grep -q "missing required env: $v\$" "$ERR" \
    && ok "missing $v alone → exit 2 naming exactly it" || bad "missing $v alone → exit 2 naming it" "rc=$rc $(cat "$ERR")"
done

rm -f "$TMP"/child-env.*
rc="$(run_entry "${FULL[@]}")"
[ "$rc" = 5 ] && grep -q 'a child exited' "$ERR" \
  && ok "all env set, children exit → exit 5 (restart policy relaunches)" || bad "all env set → exit 5" "rc=$rc $(cat "$ERR")"
grep -q '"path": "'"$WS"'"' "$ENGINE/sutando.config.local.json" 2>/dev/null \
  && ok "writes sutando.config.local.json pointing at the workspace" || bad "writes sutando.config.local.json" "$(cat "$ENGINE/sutando.config.local.json" 2>/dev/null)"
[ -d "$WS/tasks" ] && [ -d "$WS/results" ] && [ -d "$WS/state" ] && ok "creates tasks/ results/ state/ on the volume" || bad "creates workspace dirs"
child_env="$(cat "$TMP"/child-env.* 2>/dev/null)"
grep -q '^SUTANDO_WORKER_LOCATION=cloud$' <<< "$child_env" && ok "children see SUTANDO_WORKER_LOCATION=cloud" || bad "SUTANDO_WORKER_LOCATION=cloud" "$child_env"
grep -q '^GATEWAY_INSTANCE=cloud$' <<< "$child_env" && ok "GATEWAY_INSTANCE defaults to cloud" || bad "GATEWAY_INSTANCE defaults to cloud"
grep -q '^SUTANDO_WORKER_ID=cloud-t$' <<< "$child_env" && ok "children see the worker id" || bad "worker id passed to children"
grep -q 'seat=cloud-t instance=cloud runtime=stub' "$OUT" && ok "announces seat/instance/runtime on stdout" || bad "announces seat on stdout" "$(cat "$OUT")"

rm -f "$TMP"/child-env.*
rc="$(run_entry "${FULL[@]}" GATEWAY_INSTANCE=dev)"
grep -q '^GATEWAY_INSTANCE=dev$' <<< "$(cat "$TMP"/child-env.* 2>/dev/null)" && ok "an explicit GATEWAY_INSTANCE is kept" || bad "explicit GATEWAY_INSTANCE kept"

rc="$(run_entry "${FULL[@]}" FAKE_WS=/elsewhere)"
[ "$rc" = 3 ] && grep -q "expected '$WS'" "$ERR" && ok "workspace resolving elsewhere → exit 3" || bad "workspace mismatch → exit 3" "rc=$rc $(cat "$ERR")"
rc="$(run_entry "${FULL[@]}" SUTANDO_WORKER_RUNTIME=bogus)"
[ "$rc" = 2 ] && grep -q "unknown SUTANDO_WORKER_RUNTIME='bogus'" "$ERR" && ok "unknown runtime → exit 2" || bad "unknown runtime → exit 2" "rc=$rc"
rc="$(run_entry "${FULL[@]}" SUTANDO_WORKER_RUNTIME=adapter)"
[ "$rc" = 4 ] && grep -q 'runtime.sh' "$ERR" && ok "adapter without /workspace/runtime.sh → exit 4 naming the hook" || bad "adapter slot → exit 4" "rc=$rc $(cat "$ERR")"
rc="$(run_entry "${FULL[@]}" SUTANDO_WORKER_RUNTIME=ag2-assistant)"
[ "$rc" = 2 ] && grep -q 'AG2ASSISTANT_ACP_TOKEN' "$ERR" && ok "ag2-assistant without the sidecar token → exit 2 naming it" || bad "ag2-assistant token gate" "rc=$rc $(cat "$ERR")"
rm -f "$TMP"/child-env.*
rc="$(run_entry "${FULL[@]}" SUTANDO_WORKER_RUNTIME=ag2-assistant AG2ASSISTANT_ACP_TOKEN=t)"
child_env="$(cat "$TMP"/child-env.* 2>/dev/null)"
[ "$rc" = 5 ] && grep -q '^AG2ASSISTANT_ACP_URL=ws://assistant:8802$' <<< "$child_env" \
  && ok "ag2-assistant seat launches with AG2ASSISTANT_ACP_URL defaulting to ws://assistant:8802" || bad "ag2-assistant seat launch" "rc=$rc"
rc="$(run_entry "${FULL[@]}" SUTANDO_WORKER_RUNTIME=claude)"
[ "$rc" = 4 ] && grep -q 'claude CLI on PATH' "$ERR" && ok "claude without the CLI on PATH → exit 4" || bad "claude without CLI → exit 4" "rc=$rc $(cat "$ERR")"

# ── 4. seat-stub answers a pending task, ignores claimed/assigned names ───
SWS="$TMP/stub-ws"; mkdir -p "$SWS/tasks" "$SWS/results"
printf 'id: task-T1\ntask: hi\n' > "$SWS/tasks/task-T1.txt"
printf 'id: task-T2\ntask: hi\n' > "$SWS/tasks/task-T2.claimed-core-1.txt"
SUTANDO_CLOUD_WORKSPACE="$SWS" SUTANDO_WORKER_ID=cloud-t SUTANDO_STUB_SCAN_S=0.1 \
  python3 "$D/seat-stub.py" >"$TMP/stub.out" 2>&1 &
sp=$!; sleep 1; kill "$sp" 2>/dev/null; wait "$sp" 2>/dev/null
[ "$(cat "$SWS/results/task-T1.txt" 2>/dev/null)" = "answered by cloud-t" ] \
  && ok "stub answers task-T1 with 'answered by cloud-t'" || bad "stub answers task-T1" "$(ls "$SWS/results")"
[ ! -e "$SWS/results/task-T2.claimed-core-1.txt" ] && ok "stub leaves a claimed file alone" || bad "stub leaves a claimed file alone"

# ── 4b. ag2-assistant seat: ACP turn over an in-process transport ──────────
AWS="$TMP/acp-ws"; mkdir -p "$AWS/tasks" "$AWS/results"
printf 'id: task-A1\nsource: remote-gateway\ntask: What is 2+2?\nSecond line of the task.\n' > "$AWS/tasks/task-A1.txt"
printf 'id: task-A2\ntask: never answered\n' > "$AWS/tasks/task-A2.txt"
SUTANDO_CLOUD_WORKSPACE="$AWS" SUTANDO_WORKER_ID=cloud-t AG2ASSISTANT_ACP_TOKEN=tok python3 - "$D/seat-ag2-assistant.py" "$AWS" > "$TMP/acp.out" 2>&1 <<'PY'
import asyncio, importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("seat", sys.argv[1]); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ws = Path(sys.argv[2])
LOG = []

class FakeTransport:
    """Scripted ACP agent: initialize → session/new → (permission request, two chunks) → end_turn."""
    def __init__(self): self.q = asyncio.Queue(); self.closed = False
    async def send(self, msg):
        LOG.append(msg)
        m = msg.get("method")
        if m == "initialize":
            await self.q.put({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []}})
        elif m == "session/new":
            await self.q.put({"jsonrpc": "2.0", "id": msg["id"], "result": {"sessionId": "s1"}})
        elif m == "session/prompt":
            self.prompt_id = msg["id"]
            await self.q.put({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {"sessionId": "s1", "options": [{"optionId": "allow", "kind": "allow_once", "name": "Allow"}]}})
        elif m is None and msg.get("id") == 900:
            LOG.append(("permission-reply", msg))
            for t in ("The answer ", "is 4."):
                await self.q.put({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": t}}}})
            await self.q.put({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s1", "update": {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "(ignored)"}}}})
            await self.q.put({"jsonrpc": "2.0", "id": self.prompt_id, "result": {"stopReason": "end_turn"}})
    async def receive(self): return await self.q.get()
    async def close(self): self.closed = True

class SilentTransport(FakeTransport):
    async def send(self, msg): pass  # never answers → the turn must time out

fails = []
def check(c, msg):
    print(("  ok   " if c else "  FAIL ") + msg)
    if not c: fails.append(msg)

t = FakeTransport()
async def f1(): return t
body = m.answer(ws / "tasks" / "task-A1.txt", ws / "results", f1, 5)
res = (ws / "results" / "task-A1.txt").read_text()
check(res == body and res.startswith("The answer is 4.\n"), f"agent chunks concatenated into results/task-A1.txt: {res.splitlines()[0]!r}")
check(res.rstrip().endswith("— cloud-t (ag2-assistant)"), "result ends with the signature line from SUTANDO_WORKER_ID")
methods = [x.get("method") for x in LOG if isinstance(x, dict)]
check(methods[:3] == ["initialize", "session/new", "session/prompt"], f"turn order initialize → session/new → session/prompt: {methods[:3]}")
init = LOG[0]["params"]
check(init["protocolVersion"] == 1 and init["clientCapabilities"]["fs"] == {"readTextFile": False, "writeTextFile": False}, "initialize declares protocol 1 and no fs/terminal capabilities")
prompt = LOG[2]["params"]
check(prompt["sessionId"] == "s1" and prompt["prompt"] == [{"type": "text", "text": "What is 2+2?\nSecond line of the task."}], "prompt is the task: value (multi-line body kept) on the new session")
reply = [x for x in LOG if isinstance(x, tuple)]
check(reply and reply[0][1]["result"] == {"outcome": {"outcome": "cancelled"}}, "a server permission request is answered 'cancelled' (owner-side approvals only)")
check(t.closed, "transport closed after the turn")
check(m.answer(ws / "tasks" / "task-A1.txt", ws / "results", f1, 5) is None, "an already-answered task is not answered twice")

async def f2(): return SilentTransport()
body = m.answer(ws / "tasks" / "task-A2.txt", ws / "results", f2, 0.5)
check("no answer within 0s" in body or "no answer within 1s" in body, f"a silent agent → timeout failure result: {body.splitlines()[0]!r}")
check(body.rstrip().endswith("— cloud-t (ag2-assistant)"), "the failure result is signed too")

async def f3(): raise ConnectionRefusedError("sidecar down")
body = m.turn("x", f3, 2); body = asyncio.run(body) if asyncio.iscoroutine(body) else body
check(body.startswith("ag2-assistant seat: turn failed (ConnectionRefusedError"), f"a transport failure → short failure text: {body[:60]!r}")
check(m.prompt_of("id: x\nsource: s\ntask: hello\nworld\n") == "hello\nworld" and m.prompt_of("no headers") == "no headers", "prompt_of: task: is the last header; headerless text passes through")
sys.exit(1 if fails else 0)
PY
rc=$?
sed 's/^/  /' "$TMP/acp.out" | grep -E '^\s+(ok|FAIL)' | sed 's/^  //'
[ "$rc" = 0 ] && ok "ag2-assistant seat: in-process ACP turn suite passed" || bad "ag2-assistant seat: in-process ACP turn suite" "$(grep -vE '^\s+(ok|FAIL)' "$TMP/acp.out" | tail -5)"

# ── 5. provision.sh / deprovision.sh argument gates (before any docker call) ──
rc=0; out="$(env -i PATH=/usr/bin:/bin bash "$D/provision.sh" cloud-t "$TMP/nope.env" 2>&1)" || rc=$?
[ "$rc" = 3 ] && grep -q 'env file not readable' <<< "$out" && ok "provision.sh refuses a missing env file (exit 3)" || bad "provision refuses missing env file" "rc=$rc $out"
rc=0; env -i PATH=/usr/bin:/bin bash "$D/provision.sh" >/dev/null 2>&1 || rc=$?
[ "$rc" = 2 ] && ok "provision.sh without args → usage (exit 2)" || bad "provision.sh usage exit 2" "rc=$rc"
rc=0; env -i PATH=/usr/bin:/bin bash "$D/provision.sh" 'bad id!' "$D/.env.example" >/dev/null 2>&1 || rc=$?
[ "$rc" = 2 ] && ok "provision.sh rejects a bad worker id (exit 2)" || bad "provision.sh bad id exit 2" "rc=$rc"
grep -q '^WORKER_ID=""; ENV_FILE=""; BUILD=0; BUILD_ONLY=0; TARGET="base"' "$D/provision.sh" \
  && ok "provision.sh builds the base target by default (claude is opt-in)" || bad "provision.sh default target is base"
grep -q '^FROM base AS claude' "$D/Dockerfile" && ok "Dockerfile has the opt-in claude stage" || bad "Dockerfile claude stage"
grep -q "pip install .*'agent-client-protocol\[http\]==" "$D/Dockerfile" && ok "Dockerfile pins the ACP SDK with the websocket extra" || bad "Dockerfile pins the ACP SDK"
rc=0; env -i PATH=/usr/bin:/bin bash "$D/deprovision.sh" >/dev/null 2>&1 || rc=$?
[ "$rc" = 2 ] && ok "deprovision.sh without args → usage (exit 2)" || bad "deprovision.sh usage exit 2" "rc=$rc"

# ── 5b. provision/deprovision against a fake docker (real argv, real flow) ──
# The fake mimics the CLI shapes that matter: `inspect` of a missing name
# prints a blank line and exits 1; `build` snapshots its context directory.
FDLOG="$TMP/docker.log"; FDCTX="$TMP/docker-ctx.txt"
cat > "$FAKEBIN/docker" <<'DK'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$1 ${2:-}" in
  "info ") exit 0 ;;
  "image inspect") [ "${FAKE_IMAGE_PRESENT:-0}" = 1 ] ;;
  "pull "*) exit 1 ;;
  "build "*) ctx="${@: -1}"; (cd "$ctx" && find . -type f | sort) > "$FAKE_DOCKER_CTX"; exit 0 ;;
  "container inspect") [ "${3:-}" = "-f" ] && { [ -n "${FAKE_STATE:-}" ] && { echo "$FAKE_STATE"; exit 0; }; echo; exit 1; }; [ -n "${FAKE_STATE:-}" ] ;;
  "inspect "*) echo "fake docker: untyped inspect is ambiguous (network/volume share the name)" >&2; exit 99 ;;
  "run "*) echo cid; exit 0 ;;
  "network inspect") [ "${FAKE_NET:-0}" = 1 ] ;;
  "start "*|"rm "*|"volume "*|"network "*) exit 0 ;;
  *) exit 0 ;;
esac
DK
chmod +x "$FAKEBIN/docker"
run_prov() {  # run_prov <FAKE_STATE> <FAKE_IMAGE_PRESENT> <args...>
  local st="$1" img="$2"; shift 2
  : > "$FDLOG"
  env -i PATH="$FAKEBIN:/usr/bin:/bin" HOME="$TMP" FAKE_DOCKER_LOG="$FDLOG" FAKE_DOCKER_CTX="$FDCTX" \
      FAKE_STATE="$st" FAKE_IMAGE_PRESENT="$img" bash "$@" >"$OUT" 2>"$ERR"
  echo $?
}
printf 'REMOTE_TASK_URL=http://x\nREMOTE_TASK_TOKEN=t\nSUTANDO_WORKER_ID=from-file\n' > "$TMP/u.env"

rc="$(run_prov "" 1 "$D/provision.sh" cloud-t "$TMP/u.env" -- --add-host h:host-gateway)"
runline="$(grep '^run ' "$FDLOG")"
[ "$rc" = 0 ] && [ -n "$runline" ] && ok "absent container → docker run (exit 0)" || bad "absent container → docker run" "rc=$rc $(cat "$ERR") log=$(cat "$FDLOG")"
grep -q -- '--name sutando-worker-cloud-t ' <<< "$runline" && ok "run: --name sutando-worker-<id>" || bad "run --name" "$runline"
grep -q -- '--restart unless-stopped' <<< "$runline" && ok "run: --restart unless-stopped" || bad "run --restart" "$runline"
grep -q -- "--env-file $TMP/u.env -e SUTANDO_WORKER_ID=cloud-t -e SUTANDO_WORKER_LOCATION=cloud" <<< "$runline" \
  && ok "run: argv worker id is set AFTER --env-file (beats the file's value)" || bad "run: -e after --env-file" "$runline"
grep -q -- '-v sutando-worker-cloud-t:/workspace' <<< "$runline" && ok "run: per-user volume on /workspace" || bad "run -v" "$runline"
grep -q -- '--add-host h:host-gateway sutando-cloud-worker:local$' <<< "$runline" && ok "run: extra args after --, image last" || bad "run extra args" "$runline"
grep -q '^container inspect sutando-worker-cloud-t$' "$FDLOG" && ! grep -q '^inspect ' "$FDLOG" \
  && ok "existence is asked with a TYPED inspect (a bare inspect also matches the same-named network/volume)" || bad "typed existence check" "$(tr '\n' '|' < "$FDLOG")"

rc="$(run_prov running 1 "$D/provision.sh" cloud-t "$TMP/u.env")"
[ "$rc" = 0 ] && ! grep -q '^run ' "$FDLOG" && grep -q 'already running' "$OUT" && ok "running container → no-op (exit 0)" || bad "running → no-op" "rc=$rc $(cat "$FDLOG")"
rc="$(run_prov exited 1 "$D/provision.sh" cloud-t "$TMP/u.env")"
[ "$rc" = 0 ] && grep -q '^start sutando-worker-cloud-t$' "$FDLOG" && ! grep -q '^run ' "$FDLOG" && ok "exited container → docker start (exit 0)" || bad "exited → start" "rc=$rc $(cat "$FDLOG")"

: > "$FDCTX"
rc="$(run_prov "" 0 "$D/provision.sh" cloud-t "$TMP/u.env")"
[ "$rc" = 0 ] && grep -q '^pull sutando-cloud-worker:local$' "$FDLOG" && grep -q -- '^build .* --target base ' "$FDLOG" \
  && grep -q '^run ' "$FDLOG" && ok "image absent → pull tried, then build of the base target, then run" \
  || bad "image absent → pull then build then run" "rc=$rc $(tr '\n' '|' < "$FDLOG")"
ctx_files="$(sed 's|^\./||' "$FDCTX")"
[ -n "$ctx_files" ] && ok "build context was staged ($(wc -l < "$FDCTX" | tr -d ' ') files)" || bad "build context staged" "empty"
extra=0
prefixes="$(printf '%s\n' "$ALLOW" | grep '/$')"
while IFS= read -r f; do
  [ -z "$f" ] && continue
  [ "$f" = "deploy/cloud-worker/Dockerfile" ] && continue
  printf '%s\n' "$ALLOW" | grep -qxF "$f" && continue
  hit=0; while IFS= read -r pre; do case "$f" in "$pre"*) hit=1 ;; esac; done <<< "$prefixes"
  [ "$hit" -eq 1 ] && continue
  extra=$((extra+1)); echo "    unexpected in context: $f"
done <<< "$ctx_files"
[ "$extra" -eq 0 ] && ok "build context holds only allowlisted paths + the Dockerfile" || bad "build context minimal" "$extra unexpected"
grep -q '^workspace/\|^\.env$\|^skills/' <<< "$ctx_files" && bad "build context has no workspace/, .env or skills/" || ok "build context has no workspace/, .env or skills/"

printf 'REMOTE_TASK_URL=http://x\nREMOTE_TASK_TOKEN=t\nSUTANDO_WORKER_RUNTIME=ag2-assistant\nAG2ASSISTANT_ACP_TOKEN=shared\nGEMINI_API_KEY=g\n' > "$TMP/a.env"
rc="$(run_prov "" 1 "$D/provision.sh" cloud-t "$TMP/a.env")"
side="$(grep '^run .*sutando-assistant-cloud-t' "$FDLOG")"; work="$(grep '^run .*--name sutando-worker-cloud-t' "$FDLOG")"
[ "$rc" = 0 ] && grep -q '^network create sutando-worker-cloud-t$' "$FDLOG" && ok "runtime=ag2-assistant in the env file → per-user network created" || bad "sidecar: network create" "rc=$rc $(tr '\n' '|' < "$FDLOG")"
grep -q -- '--network sutando-worker-cloud-t --network-alias assistant ' <<< "$side" && grep -q -- 'ghcr.io/ag2ai/ag2-assistant:latest /bootstrap.sh' <<< "$side" \
  && ok "sidecar: official image on the network with alias assistant" || bad "sidecar run" "$side"
grep -q -- '-v sutando-assistant-cloud-t-data:/data -v sutando-assistant-cloud-t-workspace:/workspace' <<< "$side" && ok "sidecar: /data and /workspace volumes" || bad "sidecar volumes" "$side"
grep -q -- "-v $D/assistant-bootstrap.sh:/bootstrap.sh:ro --entrypoint sh ghcr.io/ag2ai/ag2-assistant:latest /bootstrap.sh\$" <<< "$side" && ok "sidecar: runs the mounted assistant-bootstrap.sh (profile, key seed, acp-serve)" || bad "sidecar command" "$side"
! grep -q -- "--env-file $TMP/a.env" <<< "$side" && ok "sidecar: does NOT get the user's env file (no broker token)" || bad "sidecar env-file isolation" "$side"
grep -q -- '--network sutando-worker-cloud-t -e AG2ASSISTANT_ACP_URL=ws://assistant:8802 ' <<< "$work" && ok "worker: joins the network and dials ws://assistant:8802" || bad "worker network args" "$work"
printf 'REMOTE_TASK_URL=http://x\nSUTANDO_WORKER_RUNTIME=ag2-assistant\n' > "$TMP/b.env"
rc="$(run_prov "" 1 "$D/provision.sh" cloud-t "$TMP/b.env")"
[ "$rc" = 3 ] && grep -q 'AG2ASSISTANT_ACP_TOKEN' "$ERR" && ok "runtime=ag2-assistant without AG2ASSISTANT_ACP_TOKEN → exit 3" || bad "sidecar token gate" "rc=$rc $(cat "$ERR")"

rc="$(run_prov "" 1 "$D/deprovision.sh" cloud-t)"
[ "$rc" = 0 ] && ! grep -q '^rm ' "$FDLOG" && grep -q 'already absent' "$OUT" && ok "deprovision absent → exit 0, no rm" || bad "deprovision absent" "rc=$rc $(cat "$FDLOG")"
rc="$(run_prov running 1 "$D/deprovision.sh" cloud-t --purge)"
[ "$rc" = 0 ] && grep -q '^rm -f sutando-worker-cloud-t$' "$FDLOG" && grep -q '^rm -f sutando-assistant-cloud-t$' "$FDLOG" \
  && grep -q '^volume rm sutando-worker-cloud-t$' "$FDLOG" && grep -q '^volume rm sutando-assistant-cloud-t-data$' "$FDLOG" \
  && ok "deprovision --purge → rm -f worker + sidecar, volume rm for all three" || bad "deprovision purge" "rc=$rc $(tr '\n' '|' < "$FDLOG")"

# ── 6. compose + env example ──────────────────────────────────────────────
if python3 -c 'import yaml' 2>/dev/null; then
  python3 - "$D/docker-compose.yml" <<'PY' && ok "docker-compose.yml parses: worker + ag2-assistant sidecar templates, volumes, restart" || bad "docker-compose.yml structure"
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
svcs = d["services"]; assert len(svcs) >= 1
for name, s in svcs.items():
    assert any(str(v).endswith(":/workspace") for v in s["volumes"]), name
    assert s.get("restart"), name
    if name.startswith("worker-"):
        assert s.get("env_file") and s["environment"]["SUTANDO_WORKER_ID"], name
        assert s["environment"]["AG2ASSISTANT_ACP_URL"].startswith("ws://assistant-"), name
    if name.startswith("assistant-"):
        assert any(str(v).endswith(":/data") for v in s["volumes"]), name
        assert s["entrypoint"] == ["sh", "/bootstrap.sh"], name
        assert "./assistant-bootstrap.sh:/bootstrap.sh:ro" in s["volumes"], name
        assert "GEMINI_API_KEY" in s["environment"] and "env_file" not in s, name
assert {n[:6] for n in svcs} == {"worker", "assist"}, "one worker + one sidecar template"
named = {v.split(":")[0] for s in svcs.values() for v in s["volumes"] if not str(v).startswith((".", "/"))}
assert set(d["volumes"]) >= named, (set(d["volumes"]), named)
PY
else
  grep -q '^services:' "$D/docker-compose.yml" && grep -q ':/workspace$' "$D/docker-compose.yml" \
    && grep -q 'restart:' "$D/docker-compose.yml" && ok "docker-compose.yml (structural grep; pyyaml absent)" || bad "docker-compose.yml structural"
fi
for k in REMOTE_TASK_URL REMOTE_TASK_TOKEN SUTANDO_WORKER_ID AG2ASSISTANT_ACP_TOKEN GEMINI_API_KEY; do
  grep -qx "$k=" "$D/.env.example" && ok ".env.example lists $k with no value" || bad ".env.example lists $k with no value"
done
grep -Eq '^[A-Z_]+=.*#' "$D/.env.example" && bad ".env.example has no inline comments (docker --env-file keeps them as value)" \
  || ok ".env.example has no inline comments"

# ── 7. healthcheck.sh: fresh / stale / absent ─────────────────────────────
HWS="$TMP/hc"; mkdir -p "$HWS/state"
rc=0; SUTANDO_CLOUD_WORKSPACE="$HWS" bash "$D/healthcheck.sh" >/dev/null 2>&1 || rc=$?
[ "$rc" = 1 ] && ok "healthcheck: no status file → unhealthy" || bad "healthcheck absent" "rc=$rc"
echo '{}' > "$HWS/state/gateway-status.cloud.json"
rc=0; SUTANDO_CLOUD_WORKSPACE="$HWS" bash "$D/healthcheck.sh" >/dev/null 2>&1 || rc=$?
[ "$rc" = 0 ] && ok "healthcheck: fresh status file → healthy" || bad "healthcheck fresh" "rc=$rc"
touch -t 202001010000 "$HWS/state/gateway-status.cloud.json"
rc=0; SUTANDO_CLOUD_WORKSPACE="$HWS" bash "$D/healthcheck.sh" >/dev/null 2>&1 || rc=$?
[ "$rc" = 1 ] && ok "healthcheck: status older than 3 minutes → unhealthy" || bad "healthcheck stale" "rc=$rc"
echo '{}' > "$HWS/state/gateway-status.dev.json"
rc=0; SUTANDO_CLOUD_WORKSPACE="$HWS" GATEWAY_INSTANCE=dev bash "$D/healthcheck.sh" >/dev/null 2>&1 || rc=$?
[ "$rc" = 0 ] && ok "healthcheck: honours GATEWAY_INSTANCE in the file name" || bad "healthcheck instance" "rc=$rc"

echo "  Total: $total — pass: $((total-fails)), fail: $fails"
[ "$fails" -eq 0 ]
