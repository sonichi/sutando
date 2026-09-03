#!/bin/bash
# Install the multi-worker agent pool — N launchd-managed worker sessions
# (`worker-1..N`) that share one workspace and coordinate via the claim primitive (#880).
#
# Usage:
#   bash scripts/install-worker-pool.sh [N] [--force] [--check-only] [--lead-only]
#                                       [--only-worker=<N>]
#                                       [--runtime=<claude|codex>]
#                                       [--worker-runtime=<N>:<claude|codex>]...
#
# --runtime sets every worker's CLI runtime (default `claude`, or
# $SUTANDO_POOL_RUNTIME); --worker-runtime overrides a single worker. Supported
# names match src/agent/start-cli.sh's allowlist; anything else exits 2.
# `--only-core=` / `--core-runtime=` are accepted as one-release aliases.
#
# --only-worker installs or refreshes ONE worker in place. The full run boots out
# the lead and every worker, so switching a single follower's runtime used to
# restart the whole working pool; this path touches that worker's plist and job
# only. Omit N and it is inferred from the installed pool.
#
# Every worker name, launchd label and log stem comes from src/pool_names.py.
# A full run also retires legacy `com.sutando.core-N` jobs and `core-N` tmux
# sessions before installing `worker-N` — that is the in-place migration.
#
# --check-only runs every preflight (config dir, skill, binaries, staging) and
# exits without touching launchd — the seam the preflight tests exercise.
# --lead-only installs just the lead's launchd job (startup.sh's recovery path
# when the pool is installed but the lead job is not).
#
# Defaults to N=3 per #881 design (owner directive 2026-05-18: "Set N=3 by default").
# Set N=1 to disable parallelism while keeping the plumbing installed.
#
# Idempotent: re-running with the same N updates plists in place; re-running
# with a different N removes excess plists and creates new ones to match.
#
# Pre-flight checks: claude CLI on PATH, resolved workspace writable.
# Each plist runs `claude --print "/proactive-loop-pool"` — the pool-aware
# variant of the proactive-loop skill that wedges the claim call before
# task pickup. The pool-aware skill is OUT OF THIS PR's scope; install the
# plists with N=1 first to verify the launchd shape, then bump to N>1 once
# the skill is in place.

set -euo pipefail

N=""
FORCE=0
CHECK_ONLY=0
LEAD_ONLY=0
ONLY_WORKER=""
# This script lives at `<repo>/scripts/install-worker-pool.sh`; the staging block
# and the preflight below both address repo files by absolute path.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Resolved through the shared helper: a bare python3 can be the Xcode-CLT
# stub, which passes `command -v` and raises the install dialog when run.
# shellcheck source=./python-binary.sh
. "$REPO_DIR/scripts/python-binary.sh"
PY_BIN="$(require_python "$REPO_DIR" "install the worker pool (the lead daemon is python)")" || exit 1
# Worker naming has one owner; bash never spells `worker-N` itself.
pool_name() { "$PY_BIN" "$REPO_DIR/src/pool_names.py" "$@"; }
# The runtime allowlist has one owner, shared with the wrapper and the watchdog.
# shellcheck source=./pool-runtime-drive.sh
source "$REPO_DIR/scripts/pool-runtime-drive.sh"
POOL_RUNTIME_DEFAULT="${SUTANDO_POOL_RUNTIME:-claude}"
WORKER_RUNTIME_SPECS=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --check-only) CHECK_ONLY=1 ;;
    --lead-only) LEAD_ONLY=1 ;;
    --only-worker=*|--only-core=*)
      case "$arg" in --only-core=*) echo "note: --only-core is deprecated; use --only-worker" >&2 ;; esac
      ONLY_WORKER="${arg#*=}"
      case "$ONLY_WORKER" in
        ''|*[!0-9]*) echo "error: --only-worker expects a positive integer; got '$ONLY_WORKER'" >&2; exit 2 ;;
      esac
      [ "$ONLY_WORKER" -ge 1 ] || { echo "error: --only-worker must be >= 1; got '$ONLY_WORKER'" >&2; exit 2; } ;;
    --runtime=*)
      POOL_RUNTIME_DEFAULT="${arg#--runtime=}" ;;
    --worker-runtime=*|--core-runtime=*)
      case "$arg" in --core-runtime=*) echo "note: --core-runtime is deprecated; use --worker-runtime" >&2 ;; esac
      _spec="${arg#*=}"
      case "$_spec" in
        *:*) : ;;
        *) echo "error: --worker-runtime expects <N>:<runtime>; got '$_spec'" >&2; exit 2 ;;
      esac
      _spec_idx="${_spec%%:*}"
      _spec_rt="${_spec#*:}"
      case "$_spec_idx" in
        ''|*[!0-9]*) echo "error: --worker-runtime seat must be a positive integer; got '$_spec_idx'" >&2; exit 2 ;;
      esac
      pool_runtime_supported "$_spec_rt" || {
        echo "error: unsupported worker runtime '$_spec_rt' (supported: claude, codex)" >&2
        exit 2
      }
      WORKER_RUNTIME_SPECS="$WORKER_RUNTIME_SPECS$_spec_idx=$_spec_rt"$'\n' ;;
    --*) echo "error: unknown option '$arg'" >&2; exit 2 ;;
    *)
      [ -z "$N" ] || { echo "error: more than one N given ('$N', '$arg')" >&2; exit 2; }
      N="$arg" ;;
  esac
done
# A single-worker refresh must not redeclare the pool size, so infer the installed
# size and either adopt it or require an explicit N to agree with it.
if [ -n "$ONLY_WORKER" ]; then
  # Installed size comes from the plists ALONE (either spelling, until the
  # legacy jobs are migrated). Seeding it with $ONLY_WORKER made the
  # "installed" size depend on which worker was being asked for.
  _installed=0
  shopt -s nullglob
  for _p in "$HOME/Library/LaunchAgents"/com.sutando.worker-[0-9]*.plist \
            "$HOME/Library/LaunchAgents"/com.sutando.core-[0-9]*.plist; do
    _s="$(basename "$_p" .plist)"; _s="${_s#com.sutando.}"; _s="${_s%-heartbeat}"
    _s="$(pool_name seat_of "$_s" || true)"
    [ -n "$_s" ] || continue
    [ "$_s" -gt "$_installed" ] && _installed="$_s"
  done
  shopt -u nullglob
  if [ -z "$N" ]; then
    N="$_installed"; [ "$ONLY_WORKER" -gt "$N" ] && N="$ONLY_WORKER"
  elif [ "$_installed" -gt 0 ] && [ "$N" -ne "$_installed" ]; then
    # Writing N into this worker alone would leave it declaring a size its peers
    # do not; a full install is how the size changes for everyone.
    echo "error: --only-worker preserves the installed pool size ($_installed), but N=$N" >&2
    echo "       was given. Drop the N, or run a full install to resize the pool." >&2
    exit 2
  fi
fi
N="${N:-3}"
case "$N" in
  ''|*[!0-9]*) echo "error: N must be a positive integer; got '$N'" >&2; exit 2 ;;
esac
if [ "$N" -lt 1 ] || [ "$N" -gt 16 ]; then
  echo "error: N must be in [1, 16]; got $N" >&2
  exit 2
fi

pool_runtime_supported "$POOL_RUNTIME_DEFAULT" || {
  echo "error: unsupported worker runtime '$POOL_RUNTIME_DEFAULT' (supported: claude, codex)" >&2
  exit 2
}
# Last spec for an index wins; an out-of-range index is a typo, not a no-op.
worker_runtime_for() {
  local want="$1" line answer="$POOL_RUNTIME_DEFAULT"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    case "$line" in "$want="*) answer="${line#*=}" ;; esac
  done <<< "$WORKER_RUNTIME_SPECS"
  printf '%s' "$answer"
}
while IFS= read -r _line; do
  [ -n "$_line" ] || continue
  _line_idx="${_line%%=*}"
  if [ "$_line_idx" -lt 1 ] || [ "$_line_idx" -gt "$N" ]; then
    echo "error: --worker-runtime names worker $_line_idx, outside the installed pool [1, $N]" >&2
    exit 2
  fi
done <<< "$WORKER_RUNTIME_SPECS"
if [ -n "$ONLY_WORKER" ] && [ "$ONLY_WORKER" -gt "$N" ]; then
  echo "error: --only-worker names worker $ONLY_WORKER, outside the pool [1, $N]" >&2
  exit 2
fi
# The set of workers this run touches. --only-worker narrows it to one.
if [ -n "$ONLY_WORKER" ]; then WORKER_SEATS="$ONLY_WORKER"; else WORKER_SEATS="$(seq 1 "$N")"; fi
POOL_USES_CODEX=0
POOL_USES_CLAUDE=0
for _i in $WORKER_SEATS; do
  case "$(worker_runtime_for "$_i")" in
    codex) POOL_USES_CODEX=1 ;;
    claude) POOL_USES_CLAUDE=1 ;;
  esac
done

# Resolve workspace via the canonical default (matches every other Sutando
# component). Resolved via the M0 helper (env override retired post-#1440).
WORKSPACE="$(bash "$(dirname "$0")/sutando-config.sh" workspace)"
# capture the installer's PATH: launchd strips env, and the sessions need brew bins
POOL_PATH="${PATH}"
# npm's global bin is on PATH only for shells that sourced the user profile, so
# an installer run from a non-login context silently produced followers that
# could not see globally-installed CLIs (codex → exit 127 → sandbox unavailable).
# Resolve it from npm itself rather than trusting the caller's environment.
_npm_bin=""
if command -v npm > /dev/null 2>&1; then
  _npm_prefix="$(npm prefix -g 2>/dev/null || true)"
  [ -n "$_npm_prefix" ] && [ -d "$_npm_prefix/bin" ] && _npm_bin="$_npm_prefix/bin"
fi
case ":$POOL_PATH:" in
  *":$_npm_bin:"*) : ;;
  *) [ -n "$_npm_bin" ] && POOL_PATH="$_npm_bin:$POOL_PATH" ;;
esac
# Followers must share the LIVE session's credential store, so resolve it the
# way every other Sutando launcher does (startup.sh, start-cli.sh) instead of
# guessing: an ad-hoc default silently selects a foreign credential store.
# shellcheck source=../src/claude_config_dir.sh
source "$REPO_DIR/src/claude_config_dir.sh"
if CLAUDE_CONFIG_DIR_EFFECTIVE="$(resolve_claude_config_dir "$REPO_DIR" install-worker-pool)"; then
  :
else
  # 2 = config helper absent and the caller scoped it; any other code means the
  # helper refused, and refusing to install beats installing against the wrong store.
  _ccd_rc=$?
  [ "$_ccd_rc" = "2" ] || exit 1
  echo "  ~ CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR_EFFECTIVE (caller-provided; config helper absent)"
fi
CLAUDE_CONFIG_DIR_EFFECTIVE="${CLAUDE_CONFIG_DIR_EFFECTIVE/#\~/$HOME}"

# Preflight: followers fail with opaque one-liners when these are wrong —
# check here where the operator can still see and fix them.
if [ ! -f "$CLAUDE_CONFIG_DIR_EFFECTIVE/.credentials.json" ]; then
  echo "WARN: no credentials at $CLAUDE_CONFIG_DIR_EFFECTIVE (followers will fail auth)" >&2
fi
mkdir -p "$CLAUDE_CONFIG_DIR_EFFECTIVE/skills"
ln -sfn "$REPO_DIR/skills/proactive-loop-pool" "$CLAUDE_CONFIG_DIR_EFFECTIVE/skills/proactive-loop-pool"
WORKSPACE="${WORKSPACE/#\~/$HOME}"
mkdir -p "$WORKSPACE/logs"
mkdir -p "$WORKSPACE/state/cores"

# TCC: launchd cannot exec scripts under ~/Documents nor open log paths
# there — stage the wrapper and logs outside (memory: feedback_pool_wrapper_tcc).
STAGE_DIR="$HOME/.sutando/bin"
LOG_DIR="$HOME/Library/Application Support/Sutando/logs"
mkdir -p "$STAGE_DIR" "$LOG_DIR"
# kick-pool is staged from the repo too, so the recovery watchdog cannot drift
# away from the session naming the wrapper creates (an unversioned copy rotted).
for w in pool-worker-wrapper.sh pool-follower-beat.sh pool-lead-wrapper.sh kick-pool.sh \
         pool-runtime-drive.sh; do
  cp "$REPO_DIR/scripts/$w" "$STAGE_DIR/$w"
  chmod +x "$STAGE_DIR/$w"
done

# Resolve claude + tmux binaries. Caller's $PATH may not include the
# install dirs on launchd-spawned processes, so capture absolute paths now.
CLAUDE_BIN="$(command -v claude || true)"
TMUX_BIN="$(command -v tmux || true)"
if [ -z "$TMUX_BIN" ]; then
  echo "error: 'tmux' not found on \$PATH (persistent-form followers run in tmux)" >&2
  exit 1
fi
if [ -z "$CLAUDE_BIN" ] && [ "$POOL_USES_CLAUDE" -eq 1 ]; then
  echo "error: 'claude' CLI not found on \$PATH" >&2
  exit 1
fi
CODEX_BIN=""
CODEX_CONFIG_ENV=""
CODEX_CONFIG_DIR=""
if [ "$POOL_USES_CODEX" -eq 1 ]; then
  CODEX_BIN="$(command -v codex || true)"
  if [ -z "$CODEX_BIN" ]; then
    echo "error: 'codex' CLI not found on \$PATH (a worker was declared runtime=codex)" >&2
    exit 1
  fi
  if [ ! -f "$REPO_DIR/skills/proactive-loop-pool/CODEX.md" ]; then
    echo "error: missing $REPO_DIR/skills/proactive-loop-pool/CODEX.md (the Codex pool entry)" >&2
    exit 1
  fi
  # Codex followers must share the operator's authenticated store, resolved the
  # same way src/agent/codex/cli/start-cli.sh resolves it.
  CODEX_CONFIG_ENV="$(bash "$REPO_DIR/scripts/sutando-config.sh" core-config-dir-env-name codex 2>/dev/null || true)"
  CODEX_CONFIG_DIR="$(bash "$REPO_DIR/scripts/sutando-config.sh" core-config-dir-value codex 2>/dev/null || true)"
fi

# Absolute binary per runtime — launchd hands the follower no usable PATH.
runtime_bin_for() {
  case "$1" in
    claude) printf '%s' "$CLAUDE_BIN" ;;
    codex) printf '%s' "$CODEX_BIN" ;;
    *) echo "install-worker-pool: unsupported worker runtime: $1" >&2; return 2 ;;
  esac
}

LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
mkdir -p "$LAUNCH_AGENTS"
UID_VAL="$(id -u)"
DOMAIN="gui/$UID_VAL"

# launchctl bootout is async — the service can still be in the "unloading"
# state when bootstrap fires, surfacing as "Bootstrap failed: 5: Input/output
# error". Retry bootstrap a few times with backoff before giving up. Observed
# 2026-05-19 on a 3-worker re-install after the first slot's bootout.
bootstrap_with_retry() {
  local plist="$1"
  local i
  for i in 1 2 3 4 5; do
    if launchctl bootstrap "$DOMAIN" "$plist" 2>/dev/null; then return 0; fi
    sleep 0.5
  done
  # Final attempt with stderr visible so the script aborts with diagnostic.
  launchctl bootstrap "$DOMAIN" "$plist"
}

# The lead is supervised like every other Sutando service. Without a job of its
# own it was the single pool component nothing restarted, and the followers
# degrade to leaderless claiming for as long as it stays dead.
install_pool_lead() {
  local template="$REPO_DIR/src/launchd/com.sutando.pool-lead.plist"
  local plist="$LAUNCH_AGENTS/com.sutando.pool-lead.plist"
  [ -f "$template" ] || { echo "error: missing lead plist template: $template" >&2; return 1; }
  STAGE_DIR="$STAGE_DIR" LOG_DIR="$LOG_DIR" REPO_DIR="$REPO_DIR" \
  PY_BIN="$PY_BIN" POOL_PATH="$POOL_PATH" \
  "$PY_BIN" - "$template" "$plist" <<'PY'
import os, plistlib, sys
src, dst = sys.argv[1:]
sub = {"__STAGE_DIR__": os.environ["STAGE_DIR"], "__LOG_DIR__": os.environ["LOG_DIR"],
       "__REPO__": os.environ["REPO_DIR"], "__PY__": os.environ["PY_BIN"],
       "__PATH__": os.environ["POOL_PATH"], "__HOME__": os.environ["HOME"]}
def rep(v):
    if isinstance(v, str):
        for k, n in sub.items():
            v = v.replace(k, n)
    elif isinstance(v, list):
        v = [rep(x) for x in v]
    elif isinstance(v, dict):
        v = {k: rep(x) for k, x in v.items()}
    return v
with open(src, "rb") as fh:
    data = plistlib.load(fh)
with open(dst, "wb") as fh:
    plistlib.dump(rep(data), fh, sort_keys=False)
PY
  launchctl bootout "$DOMAIN/com.sutando.pool-lead" 2>/dev/null || true
  bootstrap_with_retry "$plist"
  echo "installed: com.sutando.pool-lead (log: $LOG_DIR/pool-lead.log)"
}

# Regression guard: plists invoking a skill that is not on disk make launchd's
# KeepAlive restart the failing process every ThrottleInterval seconds and burn
# quota. Refuse unless the skill is present OR the caller passes --force.
#
# Check the dir the followers actually load skills from — the resolved
# CLAUDE_CONFIG_DIR this script just symlinked into. `-d` follows the symlink,
# so a checkout without the skill leaves a dangling link and still fails here.
SKILL_DIR_CANDIDATES=(
  "$CLAUDE_CONFIG_DIR_EFFECTIVE/skills/proactive-loop-pool"
)
SKILL_FOUND=0
for d in "${SKILL_DIR_CANDIDATES[@]}"; do
  if [ -d "$d" ]; then SKILL_FOUND=1; break; fi
done

# The lead runs the python daemon, not the skill, so --lead-only is not gated on it.
if [ "$SKILL_FOUND" -eq 0 ] && [ "$FORCE" -eq 0 ] && [ "$LEAD_ONLY" -eq 0 ]; then
  cat >&2 <<MSG
error: '/proactive-loop-pool' skill not found at:
$(for d in "${SKILL_DIR_CANDIDATES[@]}"; do echo "  $d"; done)

The skill ships in this repo under skills/proactive-loop-pool/; install.sh
symlinks it into the runtime's skills directory. Not finding it means that
link is missing — installing anyway spawns launchd jobs that loop-fail on
the missing skill and burn quota.

To bypass and install anyway (e.g. for plist-shape testing), re-run with:
  bash scripts/install-worker-pool.sh $N --force
MSG
  exit 1
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "preflight OK: CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR_EFFECTIVE"
  echo "preflight OK: skill=${SKILL_DIR_CANDIDATES[0]}"
  echo "preflight OK: workspace=$WORKSPACE"
  for i in $WORKER_SEATS; do
    echo "preflight OK: $(pool_name worker_name "$i") runtime=$(worker_runtime_for "$i") bin=$(runtime_bin_for "$(worker_runtime_for "$i")")"
  done
  exit 0
fi

# A single-worker refresh leaves the lead running: booting it out would stall
# assignment for every other follower to change one worker.
if [ -z "$ONLY_WORKER" ]; then
  install_pool_lead
  if [ "$LEAD_ONLY" -eq 1 ]; then
    exit 0
  fi
elif [ "$LEAD_ONLY" -eq 1 ]; then
  echo "error: --only-worker and --lead-only are mutually exclusive" >&2
  exit 2
fi

# Retire one legacy-named worker (`core-N`): its launchd job, the retired
# heartbeat sidecar, its tmux session and its beat file. Idempotent.
retire_legacy_worker() {
  local legacy="$1" lbl
  for lbl in "com.sutando.$legacy" "com.sutando.$legacy-heartbeat"; do
    launchctl bootout "$DOMAIN/$lbl" 2>/dev/null || true
    rm -f "$LAUNCH_AGENTS/$lbl.plist"
  done
  "$TMUX_BIN" kill-session -t "$legacy" 2>/dev/null || true
  rm -f "$WORKSPACE/state/cores/$legacy.alive"
}

# Migration: a full run retires every legacy `com.sutando.core-N` job before
# installing `worker-N`, so the same seat never runs twice under two names.
#
# CRITICAL: glob is `com.sutando.core-[0-9]*.plist`, NOT `com.sutando.core-*.plist`.
# The narrower pattern intentionally excludes `com.sutando.core-agent.plist`
# (the pre-existing Sutando.app-managed agent) — removing that would tear
# down the menu-bar app's agent liaison. Regression caught 2026-05-18 23:14
# PT during script self-test; the recovery was `cp .plist.bak .plist &&
# launchctl bootstrap`. Don't loosen this glob. (`com.sutando.worker-[0-9]*`
# has no such neighbour, but the shrink loop below keeps the digit anchor too.)
shopt -s nullglob
for existing in "$LAUNCH_AGENTS"/com.sutando.core-[0-9]*.plist; do
  [ -z "$ONLY_WORKER" ] || continue
  legacy="$(basename "$existing" .plist)"; legacy="${legacy#com.sutando.}"
  legacy="${legacy%-heartbeat}"
  [ -n "$(pool_name seat_of "$legacy" || true)" ] || continue
  echo "migrating legacy pool member: $legacy → $(pool_name canonical "$legacy")"
  retire_legacy_worker "$legacy"
done

# Stop any pre-existing pool members beyond N so we shrink cleanly when
# the user runs `install-worker-pool.sh 2` after a prior `install-worker-pool.sh 5`.
for existing in "$LAUNCH_AGENTS"/com.sutando.worker-[0-9]*.plist; do
  # A single-worker refresh declares nothing about the pool's size, so it must
  # never shrink it: the inferred N would remove nothing, but an explicit
  # `--only-worker=2 3` would silently tear down worker 4.
  [ -z "$ONLY_WORKER" ] || continue
  base="$(basename "$existing")"
  idx="$(pool_name seat_of "${base%.plist}" || true)"
  [ -n "$idx" ] || continue
  if [ "$idx" -gt "$N" ]; then
    label="${base%.plist}"
    echo "removing stale pool member: $base"
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    rm -f "$existing"
    "$TMUX_BIN" kill-session -t "${label#com.sutando.}" 2>/dev/null || true
    rm -f "$WORKSPACE/state/cores/${label#com.sutando.}.alive"
  fi
done
shopt -u nullglob

# Generate / refresh each targeted plist. Bootout-then-bootstrap so a re-install
# picks up changes to env / paths / command.
for i in $WORKER_SEATS; do
  WNAME="$(pool_name worker_name "$i")"
  LABEL="$(pool_name launchd_label "$i")"
  PLIST="$LAUNCH_AGENTS/$LABEL.plist"
  # POOL_CLAUDE_BIN below is retained unchanged: a wrapper staged by an earlier
  # install reads it and knows nothing about POOL_RUNTIME/POOL_RUNTIME_BIN.
  WORKER_RUNTIME="$(worker_runtime_for "$i")"
  WORKER_RUNTIME_BIN="$(runtime_bin_for "$WORKER_RUNTIME")"
  # Read the runtime this slot was installed with BEFORE overwriting the plist:
  # the wrapper reuses an existing tmux session, so a converted worker would keep
  # running the old CLI until something ends that session.
  PREV_RUNTIME="$(pool_runtime_from_plist "$PLIST" || true)"
  # Templated, not hand-edited: a plist edited in place is silently replaced by
  # the next install, so the tuning would vanish without any error.
  SWEEP_NUDGE_ENTRY=""
  if [ -n "${SUTANDO_POOL_SWEEP_NUDGE_S:-}" ]; then
    SWEEP_NUDGE_ENTRY="    <key>SUTANDO_POOL_SWEEP_NUDGE_S</key><string>$SUTANDO_POOL_SWEEP_NUDGE_S</string>"
  fi
  RUNTIME_CONFIG_KEYS=""
  if [ "$WORKER_RUNTIME" = "codex" ] && [ -n "$CODEX_CONFIG_ENV" ] && [ -n "$CODEX_CONFIG_DIR" ]; then
    RUNTIME_CONFIG_KEYS="    <key>POOL_RUNTIME_CONFIG_ENV</key><string>$CODEX_CONFIG_ENV</string>
    <key>POOL_RUNTIME_CONFIG_DIR</key><string>$CODEX_CONFIG_DIR</string>
"
  fi
  cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
                       "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$STAGE_DIR/pool-worker-wrapper.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$HOME</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SUTANDO_WORKER_ID</key><string>$WNAME</string>
    <key>SUTANDO_WORKER_SEAT</key><string>$i</string>
    <key>SUTANDO_WORKER_POOL_SIZE</key><string>$N</string>
    <key>SUTANDO_CORE_ID</key><string>$i</string>
    <key>SUTANDO_CORE_POOL_SIZE</key><string>$N</string>
$SWEEP_NUDGE_ENTRY
    <key>POOL_REPO_DIR</key><string>$REPO_DIR</string>
    <key>POOL_CLAUDE_BIN</key><string>$CLAUDE_BIN</string>
    <key>POOL_TMUX_BIN</key><string>$TMUX_BIN</string>
    <key>POOL_WORKSPACE</key><string>$WORKSPACE</string>
    <key>CLAUDE_CONFIG_DIR</key><string>$CLAUDE_CONFIG_DIR_EFFECTIVE</string>
    <key>PATH</key><string>$POOL_PATH</string>
    <key>POOL_RUNTIME</key><string>$WORKER_RUNTIME</string>
    <key>POOL_RUNTIME_BIN</key><string>$WORKER_RUNTIME_BIN</string>
${RUNTIME_CONFIG_KEYS}  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/$WNAME.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/$WNAME.err</string>
</dict>
</plist>
PLIST_EOF
  # A --only-worker refresh skips the migration loop above, so retire this
  # seat's legacy job here as well (idempotent on a migrated pool).
  retire_legacy_worker "$(pool_name legacy_name "$WNAME")"
  # bootout first (idempotent — succeeds if not loaded) then bootstrap.
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  if [ -n "$PREV_RUNTIME" ] && [ "$PREV_RUNTIME" != "$WORKER_RUNTIME" ]; then
    echo "$WNAME: runtime $PREV_RUNTIME → $WORKER_RUNTIME; ending the old session"
    "$TMUX_BIN" kill-session -t "$WNAME" 2>/dev/null || true
  fi
  bootstrap_with_retry "$PLIST"
  # bootstrap alone leaves the job loaded-but-never-started (observed: a
  # scaled-up worker sat dead until poked), and RunAtLoad does not close it.
  launchctl kickstart "$DOMAIN/$LABEL" 2>/dev/null || true
  echo "installed: $LABEL (workspace=$WORKSPACE, runtime=$WORKER_RUNTIME)"
done

echo
if [ -n "$ONLY_WORKER" ]; then
  echo "Refreshed $(pool_name launchd_label "$ONLY_WORKER") only (lead and other workers untouched)."
  echo "Log: $LOG_DIR/$(pool_name worker_name "$ONLY_WORKER").log"
  exit 0
fi
echo "Installed pool of $N worker(s) + lead."
echo "Logs: $LOG_DIR/<worker>.log per worker below, $LOG_DIR/pool-lead.log"
echo
for _i in $(seq 1 "$N"); do
  echo "  $(pool_name worker_name "$_i"): runtime=$(worker_runtime_for "$_i")"
done
echo "Watch the pool: bash scripts/pool-status.sh   |   observe a worker: tmux attach -t <worker>"
