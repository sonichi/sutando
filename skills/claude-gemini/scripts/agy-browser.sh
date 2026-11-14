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
# The repo's resolver, never a bare `command -v python3`: on macOS that can be the CLT stub, whose
# first run opens the install-tools dialog.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="$(cd "$SELF_DIR/../../.." && pwd -P)"
PYTHON=""
if [[ -f "$REPO/scripts/python-binary.sh" ]]; then
  . "$REPO/scripts/python-binary.sh"
  PYTHON="$(resolve_python "$REPO")"
elif [[ -n "${SUTANDO_PY:-}" && -x "$SUTANDO_PY" ]]; then
  PYTHON="$SUTANDO_PY"
fi
LSOF="$(command -v lsof 2>/dev/null || true)"
for c in /usr/sbin/lsof /usr/bin/lsof; do [[ -z "$LSOF" && -x "$c" ]] && LSOF="$c"; done
CDP_URL="http://127.0.0.1:$PORT"
LOCK="$HOME/.gemini/agy-browser.lock"
MCP_PKG="chrome-devtools-mcp@1.10.1"

cdp_up() { curl -fsS --max-time 2 "$CDP_URL/json/version" >/dev/null 2>&1; }
# PIDs whose argv holds exactly one --user-data-dir, this profile, compared per argv element since
# a profile path may contain a space. With $1, only those in process group $1.
profile_pids() {
  P="$PROFILE" G="${1:-}" "$PYTHON" - <<'PY'
import ctypes, os, struct, subprocess
want = b"--user-data-dir=" + os.fsencode(os.environ["P"])
group = int(os.environ["G"]) if os.environ["G"] else None
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
        if group is not None and os.getpgid(pid) != group:
            continue
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
# TERM every process on the profile (in process group $1, if given), then wait up to 5s for all of
# them to be gone. An argv read that fails is not "none left", so it fails too.
stop_profile() {
  local pids; pids="$(profile_pids "${1:-}")" || return 1
  [[ -n "$pids" ]] || return 0
  kill $pids 2>/dev/null || true
  for _ in $(seq 1 25); do
    pids="$(profile_pids "${1:-}")" || return 1
    [[ -z "$pids" ]] && return 0
    sleep 0.2
  done
  return 1
}
# Fail, first tearing down the Chrome this run launched, if any. The launcher reports its group
# before it reads the cancel file, so wait for that report or its exit; on_exit decides about $HS.
abort_start() {
  [[ -n "$launched" ]] || fail "$1"
  : >"$HS/cancel"
  local pg=""
  for _ in $(seq 1 50); do
    pg="$(cat "$HS/pgid" 2>/dev/null)" || true
    [[ -n "$pg" ]] && break
    [[ "$(launcher_state)" == dead ]] && break
    sleep 0.1
  done
  [[ -n "$pg" ]] || pg="$(cat "$HS/pgid" 2>/dev/null)" || true
  if [[ -z "$pg" ]]; then
    [[ "$(launcher_state)" == dead ]] && fail "$1; Chrome's launcher exited without starting it"
    fail "$1; Chrome's launcher has not started it yet; it will read $HS/cancel and exit without starting it"
  fi
  group_owned "$pg" || { settled=1; fail "$1; $(unproved "$pg")"; }
  kill -TERM -- "-$pg" 2>/dev/null || true
  stop_profile "$pg" || fail "$1; the Chrome this run started on $PROFILE did not exit, or its processes could not be read"
  settled=1; rm -rf "$HS"
  fail "$1; stopped the Chrome this run started"
}
# Every exit, signals included, that did not end in success cancels the launch. $HS is removed only
# once the launcher is proved gone: before its report, the cancel file is what stops it.
on_exit() {
  [[ -n "${HS:-}" && -z "$settled" ]] || return 0
  # $! is set at the fork, before $launcher is: empty means no launcher exists to read the cancel file.
  # Read it with -u off: bash 3.2 rejects even ${!:-} while no job has ever been started.
  set +u; launcher="${launcher:-$!}"; set -u
  [[ -n "$launcher" ]] || { rm -rf "$HS"; return 0; }
  : >"$HS/cancel" 2>/dev/null || true
  local st pg rc
  st="$(launcher_state)"
  pg="$(cat "$HS/pgid" 2>/dev/null)" || true
  if [[ -n "$pg" ]]; then
    rc=0; end_group "$pg" || rc=$?
    if [[ $rc == 0 ]]; then rm -rf "$HS"
    elif [[ $rc == 2 ]]; then echo "agy-browser.sh: $(unproved "$pg")" >&2
    else echo "agy-browser.sh: process group $pg (Chrome on $PROFILE) did not exit; kill it with 'kill -KILL -- -$pg'" >&2; fi
  elif [[ "$st" == dead ]]; then rm -rf "$HS"
  else echo "agy-browser.sh: Chrome's launcher has not reported its process group; it will read $HS/cancel and exit without starting Chrome" >&2; fi
}
unproved() { echo "could not prove process group $1 is still the Chrome this run started, so it was not signalled; $HS is kept"; }
# True only when ps read the table and no live (non-zombie) process is left in group $1.
group_gone() {
  local rows; rows="$(ps -Ao pgid=,stat= 2>/dev/null)" || return 1
  [[ -n "$rows" ]] || return 1
  ! awk -v g="$1" '$1 == g && $2 !~ /^Z/ { f = 1 } END { exit !f }' <<<"$rows"
}
# True only while group $1 is provably this run's: the launcher's own group, led by the launcher
# (same start time), or, once the leader is proved gone, with every live member running on $PROFILE.
group_owned() {
  [[ -n "${launcher:-}" && "$1" == "$launcher" ]] || return 1
  local st rows members owned p
  case "$(pid_state "$1")" in
    present)
      st="$(ps -o lstart= -p "$1" 2>/dev/null)" || return 1
      # A bare return in the EXIT trap would yield the status from before the trap, not this test's.
      [[ -n "$st" && -n "$LSTART" && "$st" == "$LSTART" ]] && return 0; return 1 ;;
    absent) ;;
    *) return 1 ;;
  esac
  rows="$(ps -Ao pid=,pgid=,stat= 2>/dev/null)" || return 1
  members="$(awk -v g="$1" '$2 == g && $3 !~ /^Z/ { print $1 }' <<<"$rows")"
  [[ -n "$members" ]] || return 1
  owned="$(profile_pids "$1")" || return 1
  for p in $members; do grep -qx "$p" <<<"$owned" || return 1; done
}
# Prints present, absent or unknown for pid $1; only ESRCH proves it absent.
pid_state() {
  "$PYTHON" - "$1" 2>/dev/null <<'PY' || echo unknown
import os, sys
try:
    os.kill(int(sys.argv[1]), 0)
except ProcessLookupError:
    print("absent")
except PermissionError:
    print("present")
else:
    print("present")
PY
}
# TERM group $1, KILL it after 5s, revalidating ownership before each signal. Returns 0 once no
# process in it is left, 2 when ownership could not be proved, 1 when it would not exit.
end_group() {
  group_gone "$1" && return 0
  group_owned "$1" || return 2
  kill -TERM -- "-$1" 2>/dev/null || true
  for _ in $(seq 1 25); do group_gone "$1" && return 0; sleep 0.2; done
  group_owned "$1" || return 2
  kill -KILL -- "-$1" 2>/dev/null || true
  for _ in $(seq 1 25); do group_gone "$1" && return 0; sleep 0.2; done
  return 1
}
# Prints alive, dead or unknown. dead only once the launcher (this shell's child, so a failed
# signal probe means it was reaped) is gone or a zombie; an unreadable ps is unknown, never dead.
launcher_state() {
  kill -0 "$launcher" 2>/dev/null || { echo dead; return; }
  local st; st="$(ps -o stat= -p "$launcher" 2>/dev/null)" || st=""
  if [[ -n "$st" ]]; then [[ "$st" == Z* ]] && echo dead || echo alive
  elif kill -0 "$launcher" 2>/dev/null; then echo unknown
  else echo dead; fi
}
# Serialize the registry read-add-read across runs: 'agy mcp add' overwrites, with no compare-and-swap.
# fd 9 holds the flock until exit; writers other than this script are not covered.
take_lock() {
  mkdir -p "$(dirname "$LOCK")" && exec 9>>"$LOCK" || return 1
  "$PYTHON" - <<'PY'
import fcntl, signal, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    print("agy-browser.sh: waiting for another run registering with agy", file=sys.stderr, flush=True)
    signal.alarm(60)
    fcntl.flock(9, fcntl.LOCK_EX)
PY
}
need_npx() { command -v npx >/dev/null 2>&1; }
NPX_MSG="npx not found; install Node.js to run chrome-devtools-mcp"
NOT_OURS="$CDP_URL is answered by a process not running on $PROFILE (possibly your own Chrome); not using it. Pick another --port."
LIST_FAILED="'agy mcp list' failed; cannot tell what chrome-devtools points at"
ELSEWHERE="agy's chrome-devtools server points at another browser URL; not overwriting it. Remove it with 'agy mcp remove chrome-devtools' or pass that --port."
# agy mcp list omits env and headers, so an overwritten entry cannot be put back.
UNPINNED="agy's chrome-devtools server uses a package other than $MCP_PKG; not overwriting it. Remove it with 'agy mcp remove chrome-devtools', then run start."
# Prints pinned, other (this URL, another package), elsewhere (another URL) or none; fails when
# agy cannot list its servers.
mcp_state() {
  [[ -n "$AGY_BIN" ]] || { echo none; return; }
  local list; list="$("$AGY_BIN" mcp list 9>&- 2>/dev/null)" || return 1
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
  [[ -n "$PYTHON" ]] || fail "no runnable python3 (set SUTANDO_PY, or install python3); it is needed to read process argv" ;;
esac

case "$ACTION" in
  start)
    [[ -n "$AGY_BIN" ]] || fail "agy not found on PATH or in ~/.local/bin"
    need_npx || fail "$NPX_MSG"
    [[ -n "$LSOF" ]] || fail "lsof not found; it is needed to check who owns $CDP_URL"
    state="$(mcp_state)" || fail "$LIST_FAILED"
    [[ "$state" == elsewhere ]] && fail "$ELSEWHERE"
    [[ "$state" == other ]] && fail "$UNPINNED"
    launched="" settled="" HS="" LSTART=""
    if cdp_up; then
      ours || fail "$NOT_OURS"
      echo "browser: already listening on $CDP_URL"
    else
      busy="$(profile_pids)" || fail "could not read process argv"
      [[ -z "$busy" ]] || fail "$PROFILE is already in use (PIDs $(tr '\n' ' ' <<<"$busy" | sed 's/ $//')) but not listening on $CDP_URL; stop it first or pick another --profile."
      chrome="$(find_chrome)"
      HS="$(mktemp -d)" || fail "could not create a temporary directory"
      trap on_exit EXIT
      nohup "$PYTHON" -c '
import os, shutil, sys
os.setsid()
hs = sys.argv[1]
with open(hs + "/pgid.tmp", "w") as f:
    f.write(str(os.getpid()))
os.rename(hs + "/pgid.tmp", hs + "/pgid")
if os.path.exists(hs + "/cancel"):
    shutil.rmtree(hs, ignore_errors=True)
    sys.exit(1)
os.execv(sys.argv[2], sys.argv[2:])' "$HS" \
        "$chrome" --user-data-dir="$PROFILE" --remote-debugging-port="$PORT" \
        --remote-debugging-address=127.0.0.1 --no-first-run --no-default-browser-check \
        --headless=new about:blank >/dev/null 2>&1 &
      launcher=$!
      LSTART="$(ps -o lstart= -p "$launcher" 2>/dev/null)" || LSTART=""
      [[ -n "$LSTART" ]] || echo "agy-browser.sh: could not read the launcher's start time; if this run fails, the Chrome it starts may be left running" >&2
      launched=1
      for _ in $(seq 1 30); do cdp_up && break; sleep 0.5; done
      cdp_up || abort_start "Chrome did not start listening on $CDP_URL within 15s"
      ours || abort_start "$NOT_OURS"
      echo "browser: started on $CDP_URL (profile $PROFILE)"
    fi
    # Recheck both under the lock, which may have waited 60s; the listener again after the registry
    # read, which can block. The read after the add catches writers outside the lock.
    take_lock || abort_start "could not lock $LOCK within 60s"
    ours || abort_start "$NOT_OURS"
    state="$(mcp_state)" || abort_start "$LIST_FAILED"
    [[ "$state" == elsewhere ]] && abort_start "$ELSEWHERE"
    [[ "$state" == other ]] && abort_start "$UNPINNED"
    ours || abort_start "$NOT_OURS"
    added=""
    if [[ "$state" == pinned ]]; then
      msg="mcp: chrome-devtools already registered"
    else
      "$AGY_BIN" mcp add chrome-devtools npx -y "$MCP_PKG" --browserUrl "$CDP_URL" >/dev/null 9>&- \
        || abort_start "agy mcp add failed"
      added=1
      after="$(mcp_state)" || abort_start "$LIST_FAILED"
      [[ "$after" == pinned ]] || abort_start "agy's chrome-devtools entry changed while registering (now: $after); check 'agy mcp list'"
      msg="mcp: chrome-devtools registered with agy"
    fi
    # The add and the list after it can block too; a listener swapped meanwhile is never reported as ours.
    # agy has no compare-and-remove, and the entry may already be another writer's, so it is left in place.
    ours || abort_start "$NOT_OURS${added:+; the chrome-devtools entry this run added was left in place: check 'agy mcp list' and run 'agy mcp remove chrome-devtools' if it points at $CDP_URL}"
    echo "$msg"
    settled=1
    if [[ -n "$HS" ]]; then rm -rf "$HS"; fi
    ;;
  status)
    rc=0
    if ! cdp_up; then echo "browser: not listening on $CDP_URL"; rc=1
    elif [[ -z "$LSOF" ]]; then echo "browser: lsof not found; cannot check who owns $CDP_URL"; rc=1
    elif ours; then echo "browser: listening on $CDP_URL"
    else echo "browser: $CDP_URL is held by a process not running on $PROFILE"; rc=1; fi
    case "$(mcp_state || echo failed)" in
      pinned) echo "mcp: chrome-devtools registered" ;;
      other) echo "mcp: chrome-devtools registered with a package other than $MCP_PKG; remove it with 'agy mcp remove chrome-devtools', then run start"; rc=1 ;;
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
