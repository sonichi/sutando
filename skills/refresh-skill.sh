#!/bin/bash
# refresh-skill.sh — make an UPDATED symlinked skill live in the CURRENT Claude Code
# session WITHOUT restarting it.
#
# WHY: Claude Code's skill live-watcher picks up skill add/edit/remove "within the
# current session without restarting" — BUT it does NOT follow symlinks (verified
# 2026-05-07; memory feedback_skill_watcher_no_symlink_follow). Our skills are symlinks
# into the synced repo, so `git pull` updates the file *behind* the symlink and the
# watcher never gets an fs-event → the running session keeps the old skill.
#
# THE FIX (cp-then-swap): briefly replace the symlink with a REAL directory under the
# watched path — that fires the fs-event the watcher needs, so it re-reads the skill
# mid-session — then restore the clean symlink (registration persists by skill name).
# Only the skill DEFINITION needs to be present for detection, so heavy/transient dirs
# (workspace, node_modules, generated, render scratch) are excluded — copying them is
# slow and can choke on dangling symlinks (the make-wire-episode foot-gun).
#
# Usage:
#   bash scripts/refresh-skill.sh <name> [<name> ...]   # refresh specific skills
#   bash scripts/refresh-skill.sh --all                 # every symlinked skill
set -uo pipefail   # NOT -e: one skill's hiccup must not strand a half-swapped symlink

# Resolve the live skills dir. Pre-revamp: ~/.claude/skills. The workspace-revamp
# relocates it under claude-home, resolved by the main repo's config helper.
# Precedence: SKILLS_DST env > sutando-config helper (if reachable) > ~/.claude/skills.
SKILLS_DST="${SKILLS_DST:-}"
if [ -z "$SKILLS_DST" ]; then
  # The helper lives in THIS script's own repo — a hardcoded path resolves to an
  # empty dir on any clone elsewhere, and every skill then reads as absent.
  _repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
  _cfg="${SUTANDO_REPO_DIR:-$_repo}/scripts/sutando-config.sh"
  # -f, not -x: it is invoked via `bash`, so a missing exec bit must not
  # silently disable resolution and fall through to the wrong directory.
  if [ -f "$_cfg" ]; then
    # A helper that exists but fails is not "no helper": guessing ~/.claude/skills
    # here reads every installed skill as NOT INSTALLED and refreshes nothing.
    _err="$(mktemp)"
    if ! SKILLS_DST="$(bash "$_cfg" claude-home-path skills 2>"$_err")" || [ -z "$SKILLS_DST" ]; then
      echo "refresh-skill: cannot resolve the skills dir — '$_cfg claude-home-path skills' failed:" >&2
      sed 's/^/  /' "$_err" >&2; rm -f "$_err"; exit 3
    fi
    rm -f "$_err"
  elif [ -n "${SUTANDO_REPO_DIR:-}" ]; then
    echo "refresh-skill: SUTANDO_REPO_DIR=$SUTANDO_REPO_DIR has no scripts/sutando-config.sh — refusing to guess the skills dir" >&2
    exit 3
  else
    SKILLS_DST="$HOME/.claude/skills"   # pre-revamp install: no helper anywhere
  fi
fi
echo "refresh-skill: skills dir $SKILLS_DST"
SETTLE_S="${REFRESH_SKILL_SETTLE_S:-1}"
# --all concurrency. The settle is a fixed per-skill wait, so serialising it made
# --all cost SETTLE_S x skill-count — 97s at 97 skills, past a 120s cron budget.
JOBS="${REFRESH_SKILL_JOBS:-8}"
EXCLUDES=(--exclude='workspace' --exclude='node_modules' --exclude='generated'
          --exclude='__pycache__' --exclude='.git' --exclude='*.mp4' --exclude='*.png'
          --exclude='*.wav' --exclude='.venv')
# Per-run scratch for worker results (parallel materialize). Removed on exit.
MAPDIR="$(mktemp -d)"
trap 'rm -rf "$MAPDIR"' EXIT

refresh_one() {
  # 0 = materialized (target on stdout), 3 = skipped (nothing to do), 1 = error
  local name="$1"
  local link="$SKILLS_DST/$name"
  if [ ! -e "$link" ] && [ ! -L "$link" ]; then
    echo "  skip $name (NOT INSTALLED at $SKILLS_DST — nothing to refresh)" >&2; return 3
  fi
  if [ ! -L "$link" ]; then
    echo "  skip $name (not a symlink — won't clobber a local/copy install)" >&2; return 3
  fi
  local target; target="$(readlink "$link")"
  if [ ! -d "$target" ]; then
    echo "  skip $name (symlink target missing: $target)" >&2; return 3
  fi
  # Materialize: replace the symlink with a REAL directory (fires the watcher's
  # fs-event). The settle happens while this directory is still on disk.
  rm -f "$link"
  if ! { rsync -a "${EXCLUDES[@]}" "$target"/ "$link"/ 2>/dev/null || cp -R "$target" "$link" 2>/dev/null; }; then
    echo "  ERROR copying $name — symlink restored, nothing refreshed" >&2
    rm -rf "$link"; ln -s "$target" "$link"
    return 1
  fi
  echo "$target"
  return 0
}

restore_one() {
  # Swap the real directory back to the clean symlink. ALWAYS restore even if
  # the materialize step errored — a symlink is safer than a half copy.
  local name="$1" link="$SKILLS_DST/$1" target="$2"
  [ -n "$target" ] || { echo "  ERROR restoring $name symlink! (no target)"; return 1; }
  rm -rf "$link"
  ln -s "$target" "$link"
  if [ -L "$link" ]; then
    echo "  refreshed $name"
    return 0
  fi
  echo "  ERROR restoring $name symlink!"
  return 1
}

# Settle ONCE while the chunk's real directories are on disk, so the watcher
# has a window to read them before they are swapped back to symlinks. Paying
# SETTLE_S per skill made --all cost SETTLE_S x skill-count (90s at 90 skills).
settle_once() {
  sync
  sleep "$SETTLE_S"
}

# Materialize a batch, settle while the real dirs are visible, then restore.
# Materialize and restore run in parallel chunks of $JOBS, so the wall clock is
# copy-time + ONE settle, not copy-time + one settle per chunk.
refresh_batch() {
  local name rc=0 any=0 target
  local -a done_names=() done_targets=()
  local -a pids=()
  local p

  # Phase 1 — materialize in parallel waves of $JOBS.
  for name in "$@"; do
    (
      t="$(refresh_one "$name")"
      echo "$?|$name|$t" > "$MAPDIR/$(printf '%s' "$name" | tr -c 'A-Za-z0-9._-' '_').map"
    ) &
    pids+=("$!")
    if [ "${#pids[@]}" -ge "$JOBS" ]; then
      for p in "${pids[@]}"; do wait "$p" 2>/dev/null || rc=1; done
      pids=()
    fi
  done
  if [ "${#pids[@]}" -gt 0 ]; then
    wait "${pids[@]}" 2>/dev/null || rc=1
  fi

  for f in "$MAPDIR"/*.map; do
    [ -e "$f" ] || continue
    IFS='|' read -r code mname mtarget < "$f"
    case "$code" in
      0) any=1; done_names+=("$mname"); done_targets+=("$mtarget") ;;
      3) ;;      # skipped
      *) rc=1 ;; # copy error, symlink already restored in the worker
    esac
    rm -f "$f"
  done

  # Phase 2 — ONE settle while every real directory is on disk.
  if [ "$any" = 1 ]; then
    settle_once
    # Phase 3 — restore in parallel waves of $JOBS.
    local i=0
    pids=()
    for name in "${done_names[@]}"; do
      restore_one "$name" "${done_targets[$i]}" &
      pids+=("$!")
      i=$((i + 1))
      if [ "${#pids[@]}" -ge "$JOBS" ]; then
        for p in "${pids[@]}"; do wait "$p" 2>/dev/null || rc=1; done
        pids=()
      fi
    done
    if [ "${#pids[@]}" -gt 0 ]; then
      for p in "${pids[@]}"; do wait "$p" 2>/dev/null || rc=1; done
    fi
  fi
  return "$rc"
}

main() {
  mkdir -p "$SKILLS_DST"
  [ "$#" -ge 1 ] || { echo "usage: refresh-skill.sh <name> [<name> ...] | --all" >&2; exit 2; }
  local failed=0
  if [ "$1" = "--all" ]; then
    # One batch for the whole tree: materialize all (parallel waves of $JOBS),
    # ONE settle while every real dir is visible, then restore all. A crash
    # strands at most $JOBS copies, which restore_one refuses to touch.
    local -a names=()
    local saw_any=0
    for link in "$SKILLS_DST"/*; do
      [ -L "$link" ] || continue
      names+=("$(basename "$link")")
      saw_any=1
    done
    if [ "$saw_any" = 1 ]; then
      refresh_batch "${names[@]}" || failed=1
    else
      echo "  (no symlinked skills under $SKILLS_DST)"
    fi
  else
    refresh_batch "$@" || failed=1
  fi
  if [ "$failed" = 0 ]; then
    echo "done — updated skills are live in the running session (no restart)."
  else
    echo "done — one or more skills failed to refresh; see errors above." >&2
  fi
  return "$failed"
}
main "$@"
