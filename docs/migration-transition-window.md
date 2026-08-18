# Migration transition window (moved verbatim from CLAUDE.md 2026-08-17)

## Migration transition window (30-day reader-fallback)

After `bash scripts/sutando-migrate.sh commit` lands, sources are preserved by default (per `feedback_workspace_m1_no_auto_commit`). The script's footer prints the phase-2 cleanup step, but the actual transition policy is: **readers should prefer the new canonical location first AND fall back to the legacy location for ~30 days**, emitting a one-line stderr deprecation warning when the fallback fires. This bridges the gap until any straggler writers (Sutando.app's Swift, backup tools, or in-flight services that hold pre-M0 fd's) have updated to the new path.

After 30 days of observing zero source-side writes (visible by mtime check on the legacy paths), the cleanup is safe: `bash scripts/sutando-migrate.sh commit --delete-source --backup-id <id-from-phase-1>`. The legacy-state-detected nag in `health-check.py` + `init.sh` only clears once the cleanup runs.

The reader-side fallback code is implemented in writers/readers separately — sibling PR scope, not part of the migration script itself.

