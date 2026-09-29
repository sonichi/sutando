#!/bin/bash
# Give agy a browser of its own: a headless Chrome on a separate profile, reached through the
# chrome-devtools MCP server. agy's /browser otherwise wants the user's running Chrome restarted.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: agy-browser.sh start|status|stop [--port N] [--profile DIR] [--chrome PATH]

  start   Launch the agy-only headless Chrome (if not already listening) and register the
          chrome-devtools MCP server with agy (if not already registered).
  status  Report whether that Chrome is listening and whether agy has the MCP server.
  stop    Stop only the Chrome running on the agy profile; the user's Chrome is never touched.

A port answered by any other process is refused, never adopted: it may be the user's own Chrome.
Defaults: --port 9222, --profile ~/.gemini/antigravity-browser-profile, --chrome auto-detected.
USAGE
}

fail() { echo "agy-browser.sh: $*" >&2; exit 1; }

ACTION="${1:-}"; [[ $# -gt 0 ]] && shift
PORT=9222
PROFILE="$HOME/.gemini/antigravity-browser-profile"
CHROME=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    --profile) PROFILE="${2:?--profile needs a directory}"; shift 2 ;;
    --chrome) CHROME="${2:?--chrome needs a path}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done
[[ "$PORT" =~ ^[0-9]+$ ]] || fail "--port must be a number"

# Absolute, symlink-free path, so the argv match below cannot hit another cwd's relative profile.
canon() {
  if [[ -d "$1" ]]; then (cd "$1" && pwd -P)
  elif parent="$(cd "$(dirname "$1")" 2>/dev/null && pwd -P)"; then echo "$parent/$(basename "$1")"
  elif [[ "$1" == /* ]]; then echo "$1"
  else echo "$PWD/$1"; fi
}
[[ "$ACTION" == start ]] && mkdir -p "$PROFILE"
PROFILE="$(canon "$PROFILE")"

AGY_BIN="$(command -v agy 2>/dev/null || true)"
[[ -z "$AGY_BIN" && -x "$HOME/.local/bin/agy" ]] && AGY_BIN="$HOME/.local/bin/agy"
PYTHON="$(command -v python3 2>/dev/null || true)"
LSOF="$(command -v lsof 2>/dev/null || true)"
for c in /usr/sbin/lsof /usr/bin/lsof; do [[ -z "$LSOF" && -x "$c" ]] && LSOF="$c"; done
CDP_URL="http://127.0.0.1:$PORT"
MCP_PKG="chrome-devtools-mcp@1.10.1"

cdp_up() { curl -fsS --max-time 2 "$CDP_URL/json/version" >/dev/null 2>&1; }
# PIDs whose argv carries exactly one --user-data-dir, and it is this profile. Compared per argv
# element, since ps joins argv with spaces and a profile path may itself contain one.
profile_pids() {
  P="$PROFILE" "$PYTHON" - <<'PY'
import ctypes, os, struct, subprocess
want = b"--user-data-dir=" + os.fsencode(os.environ["P"])
def proc_argv(pid):
    with open(f"/proc/{pid}/cmdline", "rb") as f:
        return f.read().split(b"\0")[:-1]
def sysctl_argv(pid, libc=ctypes.CDLL(None, use_errno=True)):
    # KERN_PROCARGS2: int argc, exec path, NUL padding, then argc NUL-terminated strings.
    size = ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
    argmax = ctypes.c_int(0)
    if libc.sysctl((ctypes.c_int * 2)(1, 8), 2, ctypes.byref(argmax), ctypes.byref(size), None, 0):
        raise OSError
    size = ctypes.c_size_t(argmax.value)
    buf = ctypes.create_string_buffer(argmax.value)
    if libc.sysctl((ctypes.c_int * 3)(1, 49, pid), 3, buf, ctypes.byref(size), None, 0):
        raise OSError
    raw = buf.raw[:size.value]
    argc, i = struct.unpack_from("i", raw)[0], raw.index(b"\0", 4)
    while i < len(raw) and raw[i] == 0:
        i += 1
    return raw[i:].split(b"\0")[:argc]
if os.path.isdir("/proc/self"):
    pids, read = [int(p) for p in os.listdir("/proc") if p.isdigit()], proc_argv
else:
    pids, read = [int(p) for p in subprocess.run(["ps", "-Ao", "pid="], capture_output=True,
                                                 text=True, check=True).stdout.split()], sysctl_argv
for pid in pids:
    try:
        flags = [a for a in read(pid) if a.startswith(b"--user-data-dir=")]
    except (OSError, ValueError, struct.error):
        continue
    if flags == [want]:
        print(pid)
PY
}
# True only when every process listening on the port is itself one of the profile's processes.
ours() {
  [[ -n "$LSOF" ]] || return 1
  local listeners owned p
  listeners="$("$LSOF" -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
  [[ -n "$listeners" ]] || return 1
  owned="$(profile_pids)"
  for p in $listeners; do grep -qx "$p" <<<"$owned" || return 1; done
}
# TERM every process on the profile, then wait up to 5s for all of them to be gone. An argv read
# that fails is not "none left", so it fails too.
stop_profile() {
  local pids; pids="$(profile_pids)" || return 1
  [[ -n "$pids" ]] || return 0
  kill $pids 2>/dev/null || true
  for _ in $(seq 1 25); do
    pids="$(profile_pids)" || return 1
    [[ -z "$pids" ]] && return 0
    sleep 0.2
  done
  return 1
}
# Fail, first tearing down the Chrome this run launched, if any. start launches only on a profile
# with no processes, so every process on it now came from this run.
abort_start() {
  [[ -n "$launched" ]] || fail "$1"
  stop_profile || fail "$1; the Chrome this run started on $PROFILE did not exit, or its processes could not be read"
  fail "$1; stopped the Chrome this run started"
}
need_npx() { command -v npx >/dev/null 2>&1; }
NPX_MSG="npx not found; install Node.js to run chrome-devtools-mcp"
NOT_OURS="$CDP_URL is answered by a process not running on $PROFILE (possibly your own Chrome); not using it. Pick another --port."
LIST_FAILED="'agy mcp list' failed; cannot tell what chrome-devtools points at"
ELSEWHERE="agy's chrome-devtools server points at another browser URL; not overwriting it. Remove it with 'agy mcp remove chrome-devtools' or pass that --port."
# Prints pinned, other (this URL, another package), elsewhere (another URL) or none; fails when
# agy cannot list its servers.
mcp_state() {
  [[ -n "$AGY_BIN" ]] || { echo none; return; }
  local list; list="$("$AGY_BIN" mcp list 2>/dev/null)" || return 1
  printf '%s\n' "$list" | U=" --browserUrl $CDP_URL " K=" $MCP_PKG " awk '
    $1 != "chrome-devtools" { next }
    index($0 " ", ENVIRON["U"]) { s = index($0 " ", ENVIRON["K"]) ? "pinned" : "other"; next }
    s == "" { s = "elsewhere" }
    END { print s ? s : "none" }'
}

find_chrome() {
  [[ -n "$CHROME" ]] && { [[ -x "$CHROME" ]] || fail "--chrome is not executable: $CHROME"; echo "$CHROME"; return; }
  local c
  for c in "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
           "/Applications/Chromium.app/Contents/MacOS/Chromium"; do
    [[ -x "$c" ]] && { echo "$c"; return; }
  done
  for c in google-chrome google-chrome-stable chromium chromium-browser; do
    command -v "$c" >/dev/null 2>&1 && { command -v "$c"; return; }
  done
  fail "no Chrome or Chromium found; pass --chrome PATH"
}

case "$ACTION" in start|status|stop)
  [[ -n "$PYTHON" ]] || fail "python3 not found; it is needed to read process argv" ;;
esac

case "$ACTION" in
  start)
    [[ -n "$AGY_BIN" ]] || fail "agy not found on PATH or in ~/.local/bin"
    need_npx || fail "$NPX_MSG"
    [[ -n "$LSOF" ]] || fail "lsof not found; it is needed to check who owns $CDP_URL"
    state="$(mcp_state)" || fail "$LIST_FAILED"
    [[ "$state" == elsewhere ]] && fail "$ELSEWHERE"
    launched=""
    if cdp_up; then
      ours || fail "$NOT_OURS"
      echo "browser: already listening on $CDP_URL"
    else
      busy="$(profile_pids)" || fail "could not read process argv"
      [[ -z "$busy" ]] || fail "$PROFILE is already in use (PIDs $(tr '\n' ' ' <<<"$busy" | sed 's/ $//')) but not listening on $CDP_URL; stop it first or pick another --profile."
      chrome="$(find_chrome)"
      nohup "$chrome" --user-data-dir="$PROFILE" --remote-debugging-port="$PORT" \
        --remote-debugging-address=127.0.0.1 --no-first-run --no-default-browser-check \
        --headless=new about:blank >/dev/null 2>&1 &
      launched=$!
      for _ in $(seq 1 30); do cdp_up && break; sleep 0.5; done
      cdp_up || abort_start "Chrome did not start listening on $CDP_URL within 15s"
      ours || abort_start "$NOT_OURS"
      echo "browser: started on $CDP_URL (profile $PROFILE)"
    fi
    # Recheck both after the launch wait, the listener first so the registry read sits right
    # before the add. agy has no compare-and-swap; the read after the add catches later writers.
    ours || abort_start "$NOT_OURS"
    state="$(mcp_state)" || abort_start "$LIST_FAILED"
    [[ "$state" == elsewhere ]] && abort_start "$ELSEWHERE"
    if [[ "$state" == pinned ]]; then
      echo "mcp: chrome-devtools already registered"
    else
      "$AGY_BIN" mcp add chrome-devtools npx -y "$MCP_PKG" --browserUrl "$CDP_URL" >/dev/null \
        || abort_start "agy mcp add failed"
      after="$(mcp_state)" || abort_start "$LIST_FAILED"
      [[ "$after" == pinned ]] || abort_start "agy's chrome-devtools entry changed while registering (now: $after); check 'agy mcp list'"
      if [[ "$state" == other ]]; then echo "mcp: chrome-devtools re-registered with $MCP_PKG"
      else echo "mcp: chrome-devtools registered with agy"; fi
    fi
    ;;
  status)
    rc=0
    if ! cdp_up; then echo "browser: not listening on $CDP_URL"; rc=1
    elif [[ -z "$LSOF" ]]; then echo "browser: lsof not found; cannot check who owns $CDP_URL"; rc=1
    elif ours; then echo "browser: listening on $CDP_URL"
    else echo "browser: $CDP_URL is held by a process not running on $PROFILE"; rc=1; fi
    case "$(mcp_state || echo failed)" in
      pinned) echo "mcp: chrome-devtools registered" ;;
      other) echo "mcp: chrome-devtools registered with a package other than $MCP_PKG; run start to re-register"; rc=1 ;;
      elsewhere) echo "mcp: chrome-devtools points at another browser URL, not $CDP_URL"; rc=1 ;;
      failed) echo "mcp: $LIST_FAILED"; rc=1 ;;
      *) echo "mcp: chrome-devtools not registered"; rc=1 ;;
    esac
    need_npx || { echo "mcp: $NPX_MSG"; rc=1; }
    exit "$rc"
    ;;
  stop)
    pids="$(profile_pids)" || fail "could not read process argv"
    if [[ -z "$pids" ]]; then
      echo "browser: none running on $PROFILE"
    elif stop_profile; then
      echo "browser: stopped the Chrome on $PROFILE"
    else
      fail "the Chrome on $PROFILE did not exit within 5s, or its processes could not be read; still running: $(profile_pids | tr '\n' ' ')"
    fi
    ;;
  *) usage; exit 2 ;;
esac
