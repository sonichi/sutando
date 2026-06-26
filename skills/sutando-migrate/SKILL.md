---
name: sutando-migrate
description: "M1 Part 2 workspace migration — guided walkthrough that scans legacy state across sources A (repo-root), B (~/.sutando/workspace/), C ($SUTANDO_WORKSPACE env-override), surfaces collisions, gets owner greenlight, commits, verifies, and (if sutando-plus + sync configured) re-routes the vault .git via sutando-migrate-sync.sh. Wraps scripts/sutando-migrate.sh for agent-invoked use; bash entry path remains available."
user-invocable: true
---

# Sutando Migrate

Guided workspace migration for existing users (M1 Part 2). Reach for this when:
- The legacy-state-detected warning fires (health-check, init.sh, check-pending-questions)
- A user has data at a pre-M0 location (`<repo>/{notes,state,results,...}/`, `~/.sutando/workspace/`, or a custom `$SUTANDO_WORKSPACE`-pointed path) and wants it folded into the M0 canonical `<repo>/workspace/`
- Owner explicitly says "migrate workspace" or "run sutando-migrate"

**Usage**: `/sutando-migrate [--auto]`

`--auto` proceeds without per-step greenlight. Without it the skill waits for owner OK at each phase boundary.

## What this skill does (5 phases)

### Phase 1 — Scan + summary

Run `bash scripts/sutando-migrate.sh scan --json`. Parse the JSON for:
- Total unique relpaths across A+B+C+dest
- Per-source byte counts
- Cross-source collision triage: `identical_content` (drop-dup, no action) + `mtime_only_diff` (commit's newest-mtime auto-resolves) + `size_mismatch` (REAL content conflicts — owner attention)
- Notable size-mismatch entries (build_log, conversation.log, state/* divergences)

Present a concise 4–6 line summary to owner via:
- Discord DM (text)
- macOS notification (`osascript -e 'display notification "scan ready: N actionable / M ignorable" with title "Sutando-Migrate"'`)
- If voice client connected, a brief voice summary

Wait for greenlight unless `--auto`.

### Phase 2 — Commit (data move)

If owner says go:
- `bash scripts/sutando-migrate.sh commit` (no `--delete-source`; honors `workspace_m1_no_auto_commit`)
- Surface the commit-output to owner: backup-id, per-source counts (copied/identical-drop/kept-dest/sidecar/skipped), sentinel paths

After commit, the script prints the phase-2 footer ("After ~7d run --commit --delete-source --backup-id <id>"). Acknowledge it; don't run delete-source automatically.

### Phase 3 — Verify

- `bash scripts/sutando-migrate.sh verify` — confirms hash + count match per indexed source-file entry
- If verify FAILS (returns non-zero), report the failure + suggest rollback. Don't proceed.

### Phase 4 — Sync re-route (sutando-plus users only)

Detect whether the user has the sync engine configured:
- Check for `~/Documents/github/sutando-plus/scripts/sync-workspace.sh` (or `${SUTANDO_PLUS_DIR:-}/scripts/sync-workspace.sh`)
- Check whether the source-C location has `.git` with a remote

If both: run `bash $SUTANDO_PLUS_DIR/scripts/sutando-migrate-sync.sh --dry-run` first; show owner the plan; on greenlight run without `--dry-run`. Reports vault remote intact + restart instruction.

If either missing: skip this phase silently; OSS users don't need it.

### Phase 5 — Report + clear nag

Summarize end-state to owner:
- Files migrated (count + bytes)
- Sources preserved at original paths (per `workspace_m1_no_auto_commit`)
- Backup-id for rollback (`bash scripts/sutando-migrate.sh rollback --backup-id <id>`)
- Phase-2 cleanup reminder (`--delete-source` after ~7d observation)
- Re-run `python3 src/health-check.py` and note: the legacy-state-detected warning WILL still fire — that's expected. The default `commit` preserves sources (matches `workspace_m1_no_auto_commit` + the (b)-style reader-fallback contract). The warning only clears after the two-phase phase 2 (`commit --delete-source --backup-id <id>` after ~7d observing no source-side writes). Mention this to owner: nag-clearance is intentional follow-up, not an unfixed bug.

Write a build_log entry summarizing what migrated.

## Failure modes + fallbacks

- **`scan` fails to find any source**: report "no migration needed; you're already on M0" and exit.
- **`commit` partial-fail mid-file** (rsync interrupt etc.): rollback via the just-created backup-id, report the failure path, exit.
- **`verify` mismatch**: report which sources failed, suggest rollback or `--force` re-commit (the latter only if owner confirms the diff is OK).
- **`sutando-migrate-sync.sh` fails post-commit**: data is at dest; just sync is broken. Suggest manual `cd <ws>; git init; git remote add origin <vault-url>; git push -u origin <hostname>` as the recovery path. Don't auto-rollback data.

## Why this is a skill (not just a script)

The script (`scripts/sutando-migrate.sh`) is the engine — operator-invocable from any shell. This SKILL adds:
- **Guided UX**: surface collision triage + phase boundaries to owner, not raw stdout
- **Owner-aware sequencing**: macOS notification + voice + Discord (channel-of-origin aware)
- **Sync auto-detect**: skip phase 4 for OSS users without sutando-plus
- **Discoverability**: `/sutando-migrate` is more memorable than `bash scripts/sutando-migrate.sh scan`

OSS users with sufficient bash literacy use the script directly. Most owners use the skill.

## Notes for the agent invoking this skill

- This skill is interactive (waits for owner greenlight at phase boundaries). Do NOT use `--auto` unless explicitly directed by owner.
- The legacy-state-detected warning is a DISCOVERABILITY signal — when you see it in cron-probe stderr, surface to owner as a one-liner ("legacy-state detected; run /sutando-migrate to fold in") rather than running silently.
- Per `feedback_workspace_m1_no_auto_commit`: this skill modifies workspace DATA but never runs `git commit` against the repo. Owner reviews any code changes (e.g. CLAUDE.md updates that may need to follow migration) manually.
- Per `feedback_design_quality`: surface the COMPLETE per-phase outcome to owner, don't suppress non-blocking warnings.
