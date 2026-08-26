#!/usr/bin/env bash
# Verify sutando-config.sh's run-dir / runtime-socket resolvers mirror #2325's
# ag2space runtime-api rundir.py EXACTLY (so shell + daemon never disagree), and
# that the `runtime` descriptor gains the additive standard.md task-A fields
# without dropping the back-compat socket/session keys.
set -u
REPO="$(cd "$(dirname "$0")/.." && pwd)"
CFG="$REPO/scripts/sutando-config.sh"
fails=0
ok() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: expected [$3] got [$2]"; fails=$((fails+1)); fi; }

# ── run-dir: SUTANDO_RUN_DIR override always wins ────────────────────────────
ok "run-dir honors SUTANDO_RUN_DIR override" \
   "$(SUTANDO_RUN_DIR=/custom/run bash "$CFG" run-dir)" "/custom/run"

# ── run-dir: OS default branch (rundir.py order) ─────────────────────────────
case "$(uname -s)" in
  Darwin) exp="$HOME/Library/Application Support/space.ag2.app/run" ;;
  *) if [ -n "${XDG_RUNTIME_DIR:-}" ]; then exp="$XDG_RUNTIME_DIR/sutando"; else exp="$HOME/.sutando/run"; fi ;;
esac
ok "run-dir OS default matches rundir.py chain" \
   "$(env -u SUTANDO_RUN_DIR bash "$CFG" run-dir)" "$exp"

# ── runtime-socket: env override wins ────────────────────────────────────────
ok "runtime-socket honors SUTANDO_RUNTIME_SOCKET override" \
   "$(SUTANDO_RUNTIME_SOCKET=/tmp/x.sock bash "$CFG" runtime-socket)" "/tmp/x.sock"

# ── runtime-socket: else EXACTLY what rundir.py resolves. Pinning the shell to
# a literal is what let the shell keep publishing the pre-actor flat socket
# while the daemon listened on the (actor, instance) scoped one (review P1) —
# the expectation now comes from the resolver both other consumers use.
RD="$REPO/src/runtime-api/rundir.py"
ok "runtime-socket equals rundir.py socket_path (no shell copy of the chain)" \
   "$(env -u SUTANDO_RUNTIME_SOCKET SUTANDO_RUN_DIR=/r bash "$CFG" runtime-socket)" \
   "$(env -u SUTANDO_RUNTIME_SOCKET SUTANDO_RUN_DIR=/r python3 "$RD" --socket)"
ok "runtime-socket is (actor, instance) scoped, not the flat legacy path" \
   "$(env -u SUTANDO_RUNTIME_SOCKET SUTANDO_RUN_DIR=/r bash "$CFG" runtime-socket)" \
   "/r/$(env -u SUTANDO_AGENT_ID -u AGENT_MXID -u AGENT_ID -u SUTANDO_RUNTIME_STATE \
         SUTANDO_RUN_DIR=/r python3 -c "import sys;sys.path.insert(0,'$REPO/src/runtime-api');import rundir;print(rundir.instance_key(rundir.agent_id()))")/runtime.sock"

# ── drift guard: the descriptor's runtimeSocket (its own chain copy) must equal
# the `runtime-socket` subcommand, so the two copies of the rundir.py chain can
# never drift silently (review nit — de-dup by assertion, not fragile subprocess).
ok "descriptor.runtimeSocket == runtime-socket subcommand (no chain drift)" \
   "$(bash "$CFG" runtime 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("runtimeSocket",""))')" \
   "$(bash "$CFG" runtime-socket)"

# ── descriptor: additive fields present + back-compat keys intact ────────────
DESC="$(bash "$CFG" runtime 2>/dev/null)"
python3 - "$DESC" <<'PY'
import sys, json
d = json.loads(sys.argv[1])
fails = 0
def ok(name, cond):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond: fails += 1
ok("descriptor: schemaVersion == 1", d.get("schemaVersion") == 1)
ok("descriptor: runtimeId present", bool(d.get("runtimeId")))
ok("descriptor: runtimeSocket is a runtime.sock under the run dir",
   str(d.get("runtimeSocket","")).endswith("/runtime.sock")
   or str(d.get("runtimeSocket","")).endswith("/sutando-runtime.sock"))
ok("descriptor: runtimeRoot present + is a prefix of runtimeSocket (OS-independent)",
   bool(d.get("runtimeRoot")) and str(d.get("runtimeSocket","")).startswith(str(d.get("runtimeRoot",""))))
ok("descriptor: backend.type == tmux", (d.get("backend") or {}).get("type") == "tmux")
ok("descriptor: back-compat socket key still present", "socket" in d)
ok("descriptor: back-compat session key still present", "session" in d)
sys.exit(1 if fails else 0)
PY
[ $? -ne 0 ] && fails=$((fails+1))

if [ "$fails" -eq 0 ]; then echo "PASS — runtime rundir resolver green"; exit 0
else echo "FAILED ($fails)"; exit 1; fi
