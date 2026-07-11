#!/usr/bin/env bash
# disk-hygiene.sh — weekly reclaimable-space sweep for a Sutando checkout.
#
# Safe by construction: only ever removes (a) orphaned git temp packs left by
# an interrupted repack, (b) already-reclaimable git garbage, (c) migration
# backup tarballs older than a retention window, and (d) rotated logs older
# than a retention window. Never touches tracked files, working-tree state, or
# git history. Non-interactive; safe to run from cron.
#
# Motivating incident (2026-07-11): an interrupted `git repack` on Jun 5 left a
# 17 GB `tmp_pack_*` orphan in the workspace vault's .git — invisible to
# `du workspace/*` (it's under .git) and never cleaned because nothing else
# triggered a gc. This script's step 1 catches exactly that class.
#
# Self-locating: resolves its own repo root from BASH_SOURCE, so it does NOT
# depend on the caller's CWD (a bare `cd <parent> && bash scripts/...` would
# otherwise break workspace resolution).
#
# Usage:
#   bash scripts/disk-hygiene.sh            # do it
#   bash scripts/disk-hygiene.sh --dry-run  # report what it would reclaim
#
# Tunables (env overrides):
#   HYGIENE_BACKUP_RETENTION_DAYS   (default 14) — migration-backup-*.tar* older than this are removed
#   HYGIENE_LOG_RETENTION_DAYS      (default 30) — rotated *.log.* / *.gz logs older than this are removed
#   HYGIENE_TMPPACK_AGE_HOURS       (default 24) — git tmp_pack_* older than this are removed (no real repack runs this long)

set -uo pipefail

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

BACKUP_RETENTION_DAYS="${HYGIENE_BACKUP_RETENTION_DAYS:-14}"
LOG_RETENTION_DAYS="${HYGIENE_LOG_RETENTION_DAYS:-30}"
TMPPACK_AGE_HOURS="${HYGIENE_TMPPACK_AGE_HOURS:-24}"

# ── Resolve repo root + workspace (CWD-independent) ──────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
WORKSPACE="$(bash "$REPO_ROOT/scripts/sutando-config.sh" workspace 2>/dev/null)"
if [ -z "${WORKSPACE:-}" ] || [ ! -d "$WORKSPACE" ]; then
  echo "disk-hygiene: could not resolve workspace (got '${WORKSPACE:-}'). Aborting — refusing to operate on an unknown path." >&2
  exit 1
fi

_action() { if [ "$DRY_RUN" = "1" ]; then echo "  [dry-run] would $*"; else echo "  $*"; fi; }
free_before_kb="$(df -k / | awk 'NR==2{print $4}')"

echo "disk-hygiene $(date -u +%Y-%m-%dT%H:%M:%SZ)  repo=$REPO_ROOT"
echo "workspace=$WORKSPACE  (dry-run=$DRY_RUN)"

# ── Step 1: reclaim git bloat in the workspace vault repo ────────────────────
# The workspace often has its own .git (the synced vault). Remove stale interrupted-repack
# temp packs first, then gc --prune=now to drop unreferenced objects.
VAULT_GIT="$WORKSPACE/.git"
if [ -d "$VAULT_GIT" ]; then
  echo "[1/4] workspace vault git:"
  pack_dir="$VAULT_GIT/objects/pack"
  if [ -d "$pack_dir" ]; then
    # tmp_pack_* / tmp_*.pack older than N hours = orphaned interrupted repack
    while IFS= read -r -d '' tp; do
      sz="$(du -h "$tp" 2>/dev/null | cut -f1)"
      _action "remove orphaned temp pack $tp ($sz)"
      [ "$DRY_RUN" = "1" ] || rm -f "$tp"
    done < <(find "$pack_dir" -maxdepth 1 -type f -name 'tmp_*' -mmin +$((TMPPACK_AGE_HOURS*60)) -print0 2>/dev/null)
  fi
  if [ "$DRY_RUN" = "1" ]; then
    garbage="$(git -C "$WORKSPACE" count-objects -vH 2>/dev/null | awk '/size-garbage/{print $2, $3}')"
    echo "  [dry-run] would git gc --prune=now (current reclaimable garbage: ${garbage:-unknown})"
  else
    git -C "$WORKSPACE" gc --prune=now --quiet 2>/dev/null && echo "  git gc --prune=now done" || echo "  git gc skipped/failed (non-fatal)"
  fi
else
  echo "[1/4] no workspace vault git — skip"
fi

# ── Step 2: prune old migration-backup tarballs ─────────────────────────────
echo "[2/4] migration backups older than ${BACKUP_RETENTION_DAYS}d:"
found=0
while IFS= read -r -d '' f; do
  found=1; sz="$(du -h "$f" 2>/dev/null | cut -f1)"
  _action "remove $f ($sz)"
  [ "$DRY_RUN" = "1" ] || rm -f "$f"
done < <(find "$WORKSPACE/state" -maxdepth 1 -type f -name 'migration-backup-*.tar*' -mtime +"$BACKUP_RETENTION_DAYS" -print0 2>/dev/null)
[ "$found" = "0" ] && echo "  none"

# ── Step 3: prune old rotated logs ──────────────────────────────────────────
echo "[3/4] rotated logs older than ${LOG_RETENTION_DAYS}d:"
found=0
if [ -d "$WORKSPACE/logs" ]; then
  while IFS= read -r -d '' f; do
    found=1; sz="$(du -h "$f" 2>/dev/null | cut -f1)"
    _action "remove $f ($sz)"
    [ "$DRY_RUN" = "1" ] || rm -f "$f"
  done < <(find "$WORKSPACE/logs" -type f \( -name '*.log.*' -o -name '*.gz' -o -name '*.[0-9]' \) -mtime +"$LOG_RETENTION_DAYS" -print0 2>/dev/null)
fi
[ "$found" = "0" ] && echo "  none"

# ── Step 4: report ──────────────────────────────────────────────────────────
free_after_kb="$(df -k / | awk 'NR==2{print $4}')"
reclaimed_mb=$(( (free_after_kb - free_before_kb) / 1024 ))
echo "[4/4] done. free-space delta this run: ${reclaimed_mb} MB (df / reflects system-wide activity, approximate)."
echo "disk-hygiene: OK"
