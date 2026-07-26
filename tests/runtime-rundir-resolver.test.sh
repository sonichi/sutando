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

# ── runtime-socket: else <run-dir>/sutando-runtime.sock (NOT runtime-api.sock) ─
ok "runtime-socket derives <run-dir>/sutando-runtime.sock" \
   "$(env -u SUTANDO_RUNTIME_SOCKET SUTANDO_RUN_DIR=/r bash "$CFG" runtime-socket)" \
   "/r/sutando-runtime.sock"

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
ok("descriptor: runtimeSocket ends with sutando-runtime.sock",
   str(d.get("runtimeSocket","")).endswith("/sutando-runtime.sock"))
ok("descriptor: runtimeRoot == dirname(run-dir)",
   d.get("runtimeRoot") == d.get("runtimeSocket","").rsplit("/run/",1)[0] if "/run/" in d.get("runtimeSocket","") else True)
ok("descriptor: backend.type == tmux", (d.get("backend") or {}).get("type") == "tmux")
ok("descriptor: back-compat socket key still present", "socket" in d)
ok("descriptor: back-compat session key still present", "session" in d)
sys.exit(1 if fails else 0)
PY
[ $? -ne 0 ] && fails=$((fails+1))

if [ "$fails" -eq 0 ]; then echo "PASS — runtime rundir resolver green"; exit 0
else echo "FAILED ($fails)"; exit 1; fi
