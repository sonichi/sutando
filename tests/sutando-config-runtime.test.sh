#!/bin/bash
# tests/sutando-config-runtime.test.sh — E2E smoke for the `tmux-socket` and
# `runtime` subcommands on scripts/sutando-config.sh.
#
# These are the OSS-side AgentRuntime resolution contract consumed by the
# desktop app (ag2-space/ag2space-cinny-desktop#98) to decide which runtime to
# attach its Terminal to / route task-drops to / port-vs-new. The load-bearing
# property under test is FOREIGN-CALLER SAFETY: a caller whose own env points
# SUTANDO_TMUX_SOCKET at a different (bundled) socket must still get THIS OSS
# runtime's socket from `runtime` — never the caller's — else the split-brain
# the contract exists to prevent reappears.
#
# Run: bash tests/sutando-config-runtime.test.sh
# Exit: 0 = all pass, 1 = failure

set -uo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT="$REPO_DIR/scripts/sutando-config.sh"
DEFAULT_SOCK="/tmp/sutando-tmux.sock"

pass=0; fail=0
report() {
  if [ "$1" = "0" ]; then
    echo "  PASS: $2"; pass=$((pass+1))
  else
    echo "  FAIL: $2"; fail=$((fail+1))
  fi
}

# -- Test 1: tmux-socket default -------------------------------------------
echo "[1] tmux-socket → default OSS socket when env unset"
out="$(env -u SUTANDO_TMUX_SOCKET bash "$SCRIPT" tmux-socket)"
[ "$out" = "$DEFAULT_SOCK" ]; report "$?" "prints $DEFAULT_SOCK ($out)"

# -- Test 2: tmux-socket honors ambient env (same-runtime caller) -----------
echo "[2] tmux-socket → honors ambient SUTANDO_TMUX_SOCKET"
out="$(SUTANDO_TMUX_SOCKET=/tmp/custom.sock bash "$SCRIPT" tmux-socket)"
[ "$out" = "/tmp/custom.sock" ]; report "$?" "prints the ambient socket ($out)"

# -- Test 3: runtime emits valid JSON with the full descriptor --------------
echo "[3] runtime → valid JSON with all descriptor keys"
json="$(bash "$SCRIPT" runtime)"
echo "$json" | python3 -c "
import json,sys
d=json.load(sys.stdin)
need={'alive','repo','code','workspace','brain','socket','session','voice_ws','vision_control','health','authenticated'}
missing=need - set(d)
assert not missing, f'missing keys: {missing}'
# voice_ws = the runtime's voice-agent WS endpoint (v0.3.0 Live page consumes it)
assert d['voice_ws'].startswith('ws://'), f\"voice_ws {d['voice_ws']!r} not a ws:// url\"
# vision_control = the runtime's vision-control HTTP endpoint (v0.3.0 Watch consumes it)
assert d['vision_control'].startswith('http://'), f\"vision_control {d['vision_control']!r} not an http:// url\"
assert d['repo']=='$REPO_DIR', f\"repo {d['repo']} != $REPO_DIR\"
assert d['brain']==d['workspace']+'/.claude-sutando', f\"brain {d['brain']}\"
assert d['session']=='sutando-core', d['session']
assert isinstance(d['alive'], bool), d['alive']
# code = source-version identity block (Git or packaged build manifest)
c=d['code']
for k in ('commit','revision','branch','describe','tree_sha','dirty','source','built_at','tree_digest'):
    assert k in c, f'code missing {k}'
assert isinstance(c['dirty'], bool), c['dirty']
"
report "$?" "JSON valid; repo/brain/session/alive + code block correct"

# -- Test 4: FOREIGN-CALLER SAFETY (the anti-split guarantee) ---------------
# A caller whose env points at a bundled socket must NOT leak into runtime.socket.
echo "[4] runtime → socket is env-independent (foreign-caller safe)"
sock="$(SUTANDO_TMUX_SOCKET=/tmp/bundled-fake.sock bash "$SCRIPT" runtime \
        | python3 -c 'import json,sys;print(json.load(sys.stdin)["socket"])')"
[ "$sock" = "$DEFAULT_SOCK" ]; report "$?" "socket=$sock stays $DEFAULT_SOCK despite bundled env (no re-split)"

# -- Test 5: custom socket is honored via the runtime-authored heartbeat --------
# Regression for the #2113 review (High): an OSS core on a NON-default socket must
# still be reported alive on THAT socket. The socket is sourced from the core's
# .alive heartbeat (runtime-authored), so we plant a fresh heartbeat recording a
# custom socket + a live sutando-core session there, point the resolver at that
# workspace, and assert `runtime` reports it.
echo "[5] runtime → honors a custom socket via heartbeat, resolving the host label like the writer (SUTANDO_HOST_LABEL)"
if command -v tmux >/dev/null 2>&1; then
  CFG="$REPO_DIR/sutando.config.local.json"; CFG_BAK=""
  [ -f "$CFG" ] && { CFG_BAK="$(mktemp)"; cp "$CFG" "$CFG_BAK"; }
  T5WS="$(mktemp -d)"; CUSTOM="/tmp/pr2113-runtime-test-$$.sock"; LBL="pr2113-review-host"
  mkdir -p "$T5WS/state/cores"
  tmux -S "$CUSTOM" new-session -d -s sutando-core 'sleep 120' 2>/dev/null
  # Heartbeat written under the LABEL — as core_heartbeat.py would when
  # SUTANDO_HOST_LABEL is set (it names the file via util_paths._host_label).
  python3 -c "import json,time;json.dump({'host':'$LBL','last_beat_at':time.time(),'socket':'$CUSTOM','schema_version':1},open('$T5WS/state/cores/$LBL.alive','w'))"
  printf '{\"workspace\":{\"path\":\"%s\"}}' "$T5WS" > "$CFG"
  # The reader must resolve the SAME label to find the heartbeat. Regression for
  # the c91a68c review: a bare `from util_paths` fell back to gethostname(),
  # missed the labelled file, and returned the default socket.
  sock5="$(SUTANDO_HOST_LABEL="$LBL" bash "$SCRIPT" runtime | python3 -c 'import json,sys;print(json.load(sys.stdin)["socket"])')"
  [ "$sock5" = "$CUSTOM" ]; report "$?" "socket=$sock5 == custom $CUSTOM (heartbeat found via SUTANDO_HOST_LABEL, not default)"
  tmux -S "$CUSTOM" kill-server 2>/dev/null
  rm -rf "$T5WS"; rm -f "$CFG"; [ -n "$CFG_BAK" ] && mv "$CFG_BAK" "$CFG"
else
  echo "  SKIP: tmux not available"
fi

# -- Test 6: brain follows the canonical claude-config resolver ------------------
# Regression for the #2113 review (Medium): runtime.brain must match
# resolve_claude_sutando_config_dir(), including a customized core_config_dirs.
echo "[6] runtime.brain == canonical resolver under a custom core_config_dirs[type=claude]"
CFG="$REPO_DIR/sutando.config.local.json"; CFG_BAK=""
[ -f "$CFG" ] && { CFG_BAK="$(mktemp)"; cp "$CFG" "$CFG_BAK"; }
T6WS="$(mktemp -d)"
cat > "$CFG" <<JSON
{
  "workspace": {"path": "$T6WS"},
  "core_config_dirs": [{"id":"claude-custom","type":"claude","env_name":"CLAUDE_CONFIG_DIR","value":"\${WORKSPACE_DIR}/custom-claude-state","synced":true}]
}
JSON
canon="$(bash "$SCRIPT" claude-sutando-config-dir)"
brain6="$(bash "$SCRIPT" runtime | python3 -c 'import json,sys;print(json.load(sys.stdin)["brain"])')"
[ "$brain6" = "$canon" ]; report "$?" "brain=$brain6 == canonical $canon (not hardcoded .claude-sutando)"
rm -rf "$T6WS"; rm -f "$CFG"; [ -n "$CFG_BAK" ] && mv "$CFG_BAK" "$CFG"

# -- Test 7: voice_ws honors a custom PORT via runtime-authored state ------------
# Regression for the #2115 review (Medium): voice_ws hardcoded ws://127.0.0.1:9900
# is wrong for a voice-agent on a non-default PORT. It must be sourced from the
# runtime-authored state voice-agent.ts writes (state/voice-agent.json), validated
# by the recorded pid. Plant a state file recording a NON-default port + THIS
# shell's pid (alive), point the resolver at that workspace, assert it's reported.
echo "[7] runtime.voice_ws → reports a custom voice port from runtime-authored state (pid-validated)"
CFG="$REPO_DIR/sutando.config.local.json"; CFG_BAK=""
[ -f "$CFG" ] && { CFG_BAK="$(mktemp)"; cp "$CFG" "$CFG_BAK"; }
T7WS="$(mktemp -d)"; mkdir -p "$T7WS/state"
python3 -c "import json;json.dump({'voice_ws':'ws://127.0.0.1:19900','port':19900,'pid':$$,'ts':0},open('$T7WS/state/voice-agent.json','w'))"
printf '{"workspace":{"path":"%s"}}' "$T7WS" > "$CFG"
vws="$(bash "$SCRIPT" runtime | python3 -c 'import json,sys;print(json.load(sys.stdin)["voice_ws"])')"
[ "$vws" = "ws://127.0.0.1:19900" ]; report "$?" "voice_ws=$vws == custom :19900 (from state, pid alive)"

# -- Test 8: voice_ws falls back to default when the recorded pid is dead --------
# A stale state file (voice-agent exited) must NOT report a port nothing is on.
echo "[8] runtime.voice_ws → falls back to default when the state pid is dead"
DEADPID=$(python3 -c "import os;p=os.fork()
if p==0: os._exit(0)
os.waitpid(p,0);print(p)")
python3 -c "import json;json.dump({'voice_ws':'ws://127.0.0.1:19900','port':19900,'pid':$DEADPID,'ts':0},open('$T7WS/state/voice-agent.json','w'))"
vws8="$(bash "$SCRIPT" runtime | python3 -c 'import json,sys;print(json.load(sys.stdin)["voice_ws"])')"
[ "$vws8" = "ws://127.0.0.1:9900" ]; report "$?" "voice_ws=$vws8 == default (dead pid $DEADPID ignored)"
rm -rf "$T7WS"; rm -f "$CFG"; [ -n "$CFG_BAK" ] && mv "$CFG_BAK" "$CFG"

# -- Test 9: vision_control honors a custom PORT via runtime-authored state -------
# Same guarantee as voice_ws (#2115) for the Watch endpoint: vision-tools.ts writes
# state/vision-control.json at listen with its real port (:7847 or VISION_CONTROL_PORT);
# the descriptor must report that, pid-validated, not a hardcoded default.
echo "[9] runtime.vision_control → reports a custom vision port from runtime-authored state (pid-validated)"
CFG="$REPO_DIR/sutando.config.local.json"; CFG_BAK=""
[ -f "$CFG" ] && { CFG_BAK="$(mktemp)"; cp "$CFG" "$CFG_BAK"; }
T9WS="$(mktemp -d)"; mkdir -p "$T9WS/state"
python3 -c "import json;json.dump({'vision_control':'http://127.0.0.1:17847','port':17847,'pid':$$,'ts':0},open('$T9WS/state/vision-control.json','w'))"
printf '{"workspace":{"path":"%s"}}' "$T9WS" > "$CFG"
vc="$(bash "$SCRIPT" runtime | python3 -c 'import json,sys;print(json.load(sys.stdin)["vision_control"])')"
[ "$vc" = "http://127.0.0.1:17847" ]; report "$?" "vision_control=$vc == custom :17847 (from state, pid alive)"

# -- Test 10: vision_control falls back to default when the recorded pid is dead --
echo "[10] runtime.vision_control → falls back to default when the state pid is dead"
DEADPID2=$(python3 -c "import os;p=os.fork()
if p==0: os._exit(0)
os.waitpid(p,0);print(p)")
python3 -c "import json;json.dump({'vision_control':'http://127.0.0.1:17847','port':17847,'pid':$DEADPID2,'ts':0},open('$T9WS/state/vision-control.json','w'))"
vc10="$(bash "$SCRIPT" runtime | python3 -c 'import json,sys;print(json.load(sys.stdin)["vision_control"])')"
[ "$vc10" = "http://127.0.0.1:7847" ]; report "$?" "vision_control=$vc10 == default (dead pid $DEADPID2 ignored)"

# -- Test 11: vision_control rejects a non-loopback URL even with a live pid ------
# Hardening (#2118 review): the control server binds 127.0.0.1, so a state file
# recording a non-loopback host is stale/crafted — must fall back to the default,
# never hand the desktop client a URL it should not call.
echo "[11] runtime.vision_control → rejects a non-loopback recorded URL (live pid)"
python3 -c "import json;json.dump({'vision_control':'http://10.0.0.5:7847','port':7847,'pid':$$,'ts':0},open('$T9WS/state/vision-control.json','w'))"
vc11="$(bash "$SCRIPT" runtime | python3 -c 'import json,sys;print(json.load(sys.stdin)["vision_control"])')"
[ "$vc11" = "http://127.0.0.1:7847" ]; report "$?" "vision_control=$vc11 == default (non-loopback 10.0.0.5 rejected)"
rm -rf "$T9WS"; rm -f "$CFG"; [ -n "$CFG_BAK" ] && mv "$CFG_BAK" "$CFG"

echo
echo "sutando-config-runtime: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
