#!/bin/bash
# --witness <name>: a throwaway core from THIS checkout on its own socket, session and workspace.
#
# --witness-stop <name> removes only what it made, only once its recorded processes are gone (else exit 1,
# state kept); every refusal exits 2 before a side effect.

WITNESS_DEFAULT_SOCKET="/tmp/sutando-tmux.sock"
WITNESS_DEFAULT_SESSION="sutando-core"

witness_refuse() {
  echo "start-cli witness: refusing — $*" >&2
  exit 2
}

# A stop that could not finish: everything recorded is kept so a rerun can.
witness_fail() {
  echo "start-cli witness: stop incomplete — $*; $WITNESS_ROOT is kept, rerun --witness-stop $WITNESS_NAME" >&2
  exit 1
}

witness_tmux_usable() { command -v tmux > /dev/null 2>&1 && tmux -V > /dev/null 2>&1; }

# Physical path of a directory that may not exist yet: its nearest existing ancestor
# is resolved, so /tmp and /private/tmp compare equal.
witness_physical() {
  local p="$1" tail=""
  while [ -n "$p" ] && [ ! -d "$p" ]; do
    tail="/${p##*/}$tail"
    p="${p%/*}"
  done
  [ -n "$p" ] || p="/"
  printf '%s%s\n' "$(cd "$p" && pwd -P)" "$tail"
}

# Sets WITNESS_NAME, _ROOT, _WS, _SOCKET, _SESSION, _CONFIG, _RECORD from <name>.
# The session ends in -core so no witness name is a prefix-match of another's.
witness_paths() {
  local name="$1" base sum
  case "$name" in
    "") witness_refuse "a witness needs a name: --witness <name>" ;;
  esac
  if ! printf '%s' "$name" | grep -Eq '^[a-z0-9][a-z0-9-]{0,19}$'; then
    witness_refuse "witness name '$name' must be 1-20 of [a-z0-9-], starting alphanumeric"
  fi
  base="$(witness_physical "${TMPDIR:-/tmp}")"
  base="${base%/}"
  WITNESS_NAME="$name"
  WITNESS_ROOT="$base/sutando-witness/$name"
  WITNESS_WS="$WITNESS_ROOT/workspace"
  WITNESS_SOCKET="$WITNESS_ROOT/tmux.sock"
  # The root's checksum in the name keeps a same-name witness under another TMPDIR from matching this one's claude.
  sum="$(printf '%s' "$WITNESS_ROOT" | cksum | awk '{print $1}')"
  case "$sum" in ''|*[!0-9]*) witness_refuse "could not checksum $WITNESS_ROOT" ;; esac
  WITNESS_SESSION="witness-$name-$sum-core"
  WITNESS_CONFIG="$REPO/sutando.config.local.json"
  WITNESS_RECORD="$WITNESS_ROOT/witness.env"
}

# Fails closed when a socket or session could be the production core's: the
# launcher defaults, or (when given) the caller's own ambient values.
witness_assert_not_production() {
  local sock="$1" sess="$2" ambient_sock="${3:-}" ambient_sess="${4:-}" phys
  [ -n "$sock" ] && [ -n "$sess" ] || witness_refuse "empty tmux socket or session"
  phys="$(witness_physical "$sock")"
  if [ "$phys" = "$(witness_physical "$WITNESS_DEFAULT_SOCKET")" ]; then
    witness_refuse "tmux socket $sock is the production default"
  fi
  if [ "$sess" = "$WITNESS_DEFAULT_SESSION" ]; then
    witness_refuse "tmux session $sess is the production default"
  fi
  if [ -n "$ambient_sock" ] && [ "$phys" = "$(witness_physical "$ambient_sock")" ]; then
    witness_refuse "tmux socket $sock is the caller's own SUTANDO_TMUX_SOCKET"
  fi
  if [ -n "$ambient_sess" ] && [ "$sess" = "$ambient_sess" ]; then
    witness_refuse "tmux session $sess is the caller's own SUTANDO_TMUX_SESSION"
  fi
  case "$sess" in
    witness-*-core) ;;
    *) witness_refuse "tmux session $sess is not a witness session" ;;
  esac
}

# A linked worktree's .git is a file naming <common>/worktrees/<id>; a main
# checkout (where a production core runs) has a .git directory.
witness_assert_linked_worktree() {
  local gitfile="$REPO/.git" line
  [ -f "$gitfile" ] || witness_refuse "$REPO is not a linked git worktree; run witness mode from a PR worktree"
  IFS= read -r line < "$gitfile" || line=""
  case "$line" in
    gitdir:*/worktrees/*) ;;
    *) witness_refuse "$REPO/.git does not name a linked worktree" ;;
  esac
}

# The workspace this checkout resolves to, through the same helper every service uses.
witness_resolved_workspace() {
  env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" \
    bash "$REPO/scripts/sutando-config.sh" workspace 2>/dev/null
}

# Any .alive beaten within the last 2 minutes means a live core uses that workspace.
witness_workspace_has_live_core() {
  local cores="$1/state/cores"
  [ -d "$cores" ] || return 1
  [ -n "$(find "$cores" -maxdepth 1 -name '*.alive' -mmin -2 2>/dev/null | head -1)" ]
}

# Fails unless a real sha256 came back: an empty hash must never prove ownership.
witness_sha() {
  local out
  out="$(shasum -a 256 "$1" 2>/dev/null)" || return 1
  out="${out%% *}"
  [[ "$out" =~ ^[0-9a-f]{64}$ ]] || return 1
  printf '%s\n' "$out"
}

witness_record_get() { sed -n "s/^$1=//p" "$WITNESS_RECORD" 2>/dev/null | head -1; }

witness_record_set() {
  sed "/^$1=/d" "$WITNESS_RECORD" > "$WITNESS_RECORD.tmp" && printf '%s=%s\n' "$1" "$2" >> "$WITNESS_RECORD.tmp" \
    && mv -f "$WITNESS_RECORD.tmp" "$WITNESS_RECORD"
}

# <pid>:<start time>; the start time keeps a recycled pid from passing as the recorded process.
witness_proc_id() {
  local st
  [ -n "$1" ] || return 1
  st="$(ps -p "$1" -o lstart= 2>/dev/null)" && [ -n "$st" ] && printf '%s:%s\n' "$1" "$st"
}

witness_proc_live() {
  [ -n "$1" ] && [ "$(witness_proc_id "${1%%:*}")" = "$1" ]
}

# Called by start-cli.sh after launch: the server, core pane, any claude under it, and watcher pane.
witness_record_launch() {
  local pid id
  pid="$(tmux -S "$TMUX_SOCKET" display-message -p -t "=$SESSION" '#{pid}' 2>/dev/null)"
  id="$(witness_proc_id "$pid")" && witness_record_set server "$id"
  pid="$(tmux -S "$TMUX_SOCKET" list-panes -t "=$SESSION" -F '#{pane_pid}' 2>/dev/null | head -1)"
  if id="$(witness_proc_id "$pid")"; then
    witness_record_set core "$id"
    for pid in $(claude_named_pids); do
      [ "$(ps -p "$pid" -o ppid= 2>/dev/null | tr -d ' ')" = "${id%%:*}" ] || continue
      id="$(witness_proc_id "$pid")" && witness_record_set core_claude "$id"
    done
  fi
  pid="$(tmux -S "$TMUX_SOCKET" list-panes -t "=$WATCHER_SESSION" -F '#{pane_pid}' 2>/dev/null | head -1)"
  id="$(witness_proc_id "$pid")" && witness_record_set watcher "$id"
  return 0
}

witness_start() {
  local name="$1" ws0 ccd py sha
  witness_paths "$name"
  witness_assert_not_production "$WITNESS_SOCKET" "$WITNESS_SESSION" \
    "${SUTANDO_TMUX_SOCKET:-}" "${SUTANDO_TMUX_SESSION:-}"
  witness_assert_linked_worktree
  [ -e "$WITNESS_ROOT" ] && witness_refuse "witness '$name' already exists at $WITNESS_ROOT; stop it first: --witness-stop $name"
  [ -e "$WITNESS_CONFIG" ] && witness_refuse "$WITNESS_CONFIG exists; witness mode writes its own and will not overwrite or share one"
  witness_tmux_usable || witness_refuse "tmux is missing or not runnable; a witness runs only on its own tmux server"
  witness_sha "$REPO/src/agent/claude/cli/witness-mode.sh" > /dev/null \
    || witness_refuse "no usable sha256 (shasum); stop could not prove the config it writes is its own"
  ws0="$(witness_resolved_workspace)"
  [ -n "$ws0" ] || witness_refuse "could not resolve this checkout's workspace"
  witness_workspace_has_live_core "$ws0" \
    && witness_refuse "a live core beats in this checkout's workspace ($ws0/state/cores)"

  . "$REPO/scripts/python-binary.sh"
  py="$(resolve_python "$REPO")"
  [ -n "$py" ] || witness_refuse "no runnable Python interpreter"
  # Auth comes from the caller's config dir by reference; nothing is copied.
  ccd="${CLAUDE_CONFIG_DIR:-}"

  mkdir -p "$WITNESS_WS"/tasks "$WITNESS_WS"/results "$WITNESS_WS"/state "$WITNESS_WS"/logs \
    || witness_refuse "cannot create $WITNESS_WS"
  chmod 700 "$WITNESS_ROOT"
  _ws="$WITNESS_WS" _ccd="$ccd" _out="$WITNESS_CONFIG" "$py" - <<'PY' || { rm -rf "$WITNESS_ROOT"; witness_refuse "could not write $WITNESS_CONFIG"; }
import json, os
cfg = {"workspace": {"path": os.environ["_ws"]}}
if os.environ["_ccd"]:
    cfg["core_config_dirs"] = [{"id": "claude-default", "type": "claude",
                                "env_name": "CLAUDE_CONFIG_DIR",
                                "value": os.environ["_ccd"], "synced": False}]
with open(os.environ["_out"], "x") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY
  if ! sha="$(witness_sha "$WITNESS_CONFIG")"; then
    rm -f "$WITNESS_CONFIG"; rm -rf "$WITNESS_ROOT"
    witness_refuse "no usable sha256 (shasum) to record $WITNESS_CONFIG's ownership; nothing was launched"
  fi
  {
    echo "repo=$REPO"
    echo "socket=$WITNESS_SOCKET"
    echo "session=$WITNESS_SESSION"
    echo "workspace=$WITNESS_WS"
    echo "config_sha=$sha"
  } > "$WITNESS_RECORD"

  if [ "$(witness_resolved_workspace)" != "$WITNESS_WS" ]; then
    rm -f "$WITNESS_CONFIG"; rm -rf "$WITNESS_ROOT"
    witness_refuse "the workspace override did not take effect; nothing was launched"
  fi
  [ -n "$ccd" ] || echo "  ⚠ no CLAUDE_CONFIG_DIR in the caller's env: the witness gets a fresh one and will need /login in its pane" >&2
  echo "  ✓ witness '$name': workspace $WITNESS_WS"

  local notifier_env=()
  [ -n "${SUTANDO_NOTIFIER_GRACE_PERIOD:-}" ] && notifier_env+=("SUTANDO_NOTIFIER_GRACE_PERIOD=$SUTANDO_NOTIFIER_GRACE_PERIOD")
  [ -n "${SUTANDO_NOTIFIER_ROLE_POLL:-}" ] && notifier_env+=("SUTANDO_NOTIFIER_ROLE_POLL=$SUTANDO_NOTIFIER_ROLE_POLL")
  # A clean env, so nothing of the caller's (memory dir, inbox resolver, instance id) reaches the witness.
  exec env -i PATH="$PATH" HOME="$HOME" USER="${USER:-}" LOGNAME="${LOGNAME:-}" SHELL="${SHELL:-/bin/bash}" \
    TERM="${TERM:-xterm-256color}" LANG="${LANG:-en_US.UTF-8}" TMPDIR="${TMPDIR:-/tmp}" \
    ${notifier_env[@]+"${notifier_env[@]}"} \
    SUTANDO_TMUX_SOCKET="$WITNESS_SOCKET" SUTANDO_TMUX_SESSION="$WITNESS_SESSION" \
    SUTANDO_WORKSPACE_DIR="$WITNESS_WS" SUTANDO_TASKS_DIR="$WITNESS_WS/tasks" \
    SUTANDO_RESULTS_DIR="$WITNESS_WS/results" SUTANDO_CLAUDE_WORKING_DIR="$REPO" \
    /bin/bash "$REPO/src/agent/claude/cli/start-cli.sh" --witness-scrubbed "$name"
}

# The second stage, under the clean env: re-derive everything from the name and
# re-check it, so this stage never trusts what the first one exported.
witness_enter() {
  witness_paths "$1"
  [ -f "$WITNESS_RECORD" ] || witness_refuse "no witness record at $WITNESS_RECORD; start with --witness $1"
  [ "$(witness_record_get repo)" = "$REPO" ] || witness_refuse "witness '$1' belongs to $(witness_record_get repo)"
  [ "${SUTANDO_TMUX_SOCKET:-}" = "$WITNESS_SOCKET" ] && [ "${SUTANDO_TMUX_SESSION:-}" = "$WITNESS_SESSION" ] \
    || witness_refuse "the launch env does not name witness '$1''s socket and session"
  [ "${SUTANDO_WORKSPACE_DIR:-}" = "$WITNESS_WS" ] || witness_refuse "the launch env does not name the witness workspace"
  witness_assert_not_production "$SUTANDO_TMUX_SOCKET" "$SUTANDO_TMUX_SESSION"
  [ "$(witness_resolved_workspace)" = "$WITNESS_WS" ] || witness_refuse "this checkout no longer resolves to the witness workspace"
  witness_tmux_usable || witness_refuse "tmux is missing or not runnable; stop this witness: --witness-stop $1"
  WITNESS="$1"
}

witness_stop() {
  local name="$1" py server role proc left="" want have _
  witness_paths "$name"
  witness_assert_not_production "$WITNESS_SOCKET" "$WITNESS_SESSION"
  [ -f "$WITNESS_RECORD" ] || witness_refuse "no witness '$name' at $WITNESS_ROOT"
  [ "$(witness_record_get repo)" = "$REPO" ] || witness_refuse "witness '$name' was started from $(witness_record_get repo); stop it from there"
  [ "$(witness_record_get socket)" = "$WITNESS_SOCKET" ] && [ "$(witness_record_get session)" = "$WITNESS_SESSION" ] \
    || witness_refuse "the witness record does not match the derived socket/session"

  . "$REPO/scripts/python-binary.sh"
  py="$(resolve_python "$REPO")"
  # The heartbeat writer resolves its workspace through the override, so stop it while that still holds.
  if [ -n "$py" ] && [ "$(witness_resolved_workspace)" = "$WITNESS_WS" ]; then
    env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" \
      SUTANDO_TMUX_SOCKET="$WITNESS_SOCKET" SUTANDO_TMUX_SESSION="$WITNESS_SESSION" \
      "$py" "$REPO/src/core_heartbeat.py" --stop > /dev/null 2>&1 || true
  fi
  server="$(witness_record_get server)"
  if [ -z "$server" ] && [ -S "$WITNESS_SOCKET" ] && witness_tmux_usable; then
    server="$(witness_proc_id "$(tmux -S "$WITNESS_SOCKET" display-message -p '#{pid}' 2>/dev/null)")" || server=""
  fi
  if witness_proc_live "$server" || [ -S "$WITNESS_SOCKET" ]; then
    if witness_tmux_usable; then
      tmux -S "$WITNESS_SOCKET" kill-server 2>/dev/null || echo "  ⚠ tmux kill-server failed on $WITNESS_SOCKET" >&2
    else
      echo "  ⚠ tmux is missing or not runnable; cannot stop the server on $WITNESS_SOCKET" >&2
    fi
  fi
  for _ in $(seq 1 25); do witness_proc_live "$server" || break; sleep 0.2; done
  witness_proc_live "$server" && witness_fail "the witness tmux server (pid ${server%%:*}) is still running"
  if [ -S "$WITNESS_SOCKET" ]; then
    if ! witness_tmux_usable; then
      [ -n "$server" ] || witness_fail "no recorded server and no tmux to prove $WITNESS_SOCKET is dead"
    elif tmux -S "$WITNESS_SOCKET" list-sessions > /dev/null 2>&1; then
      witness_fail "a tmux server still answers on $WITNESS_SOCKET"
    fi
  fi
  # Only the processes this witness recorded at launch; a recycled pid fails the start-time check.
  for role in core_claude core watcher; do
    proc="$(witness_record_get "$role")"
    witness_proc_live "$proc" && { kill "${proc%%:*}" 2>/dev/null || true; }
  done
  for _ in $(seq 1 25); do
    left=""
    for role in core_claude core watcher; do
      proc="$(witness_record_get "$role")"
      witness_proc_live "$proc" && left="$left $role=${proc%%:*}"
    done
    [ -n "$left" ] || break
    sleep 0.2
  done
  [ -z "$left" ] || witness_fail "recorded witness processes still running:$left"
  if [ -f "$WITNESS_CONFIG" ]; then
    want="$(witness_record_get config_sha)"
    if ! [[ "$want" =~ ^[0-9a-f]{64}$ ]] || ! have="$(witness_sha "$WITNESS_CONFIG")"; then
      witness_fail "no usable sha256 to prove $WITNESS_CONFIG is the witness's own; it is left in place"
    elif [ "$have" = "$want" ]; then
      rm -f "$WITNESS_CONFIG"
    else
      echo "  ⚠ $WITNESS_CONFIG changed since the witness wrote it — left in place" >&2
    fi
  fi
  case "$WITNESS_ROOT" in
    */sutando-witness/"$name") rm -rf "$WITNESS_ROOT" ;;
  esac
  echo "  ✓ witness '$name' stopped: tmux server $WITNESS_SOCKET gone, $WITNESS_ROOT removed"
}

# Entry from start-cli.sh. A witness flag anywhere but first is refused: a later
# position would otherwise fall through to a production launch.
witness_dispatch() {
  local i=0 a
  for a in "$@"; do
    i=$((i + 1))
    case "$a" in
      --witness=*|--witness-stop=*) witness_refuse "use '--witness <name>' / '--witness-stop <name>' (no '=')" ;;
      --witness|--witness-stop|--witness-scrubbed)
        [ "$i" = 1 ] || witness_refuse "$a must be the first argument" ;;
    esac
  done
  [ "$#" -le 2 ] || witness_refuse "witness mode takes no other arguments (got: $*)"
  case "${1:-}" in
    --witness) witness_start "${2:-}" ;;
    --witness-stop) witness_stop "${2:-}"; exit 0 ;;
    --witness-scrubbed) witness_enter "${2:-}" ;;
  esac
}
