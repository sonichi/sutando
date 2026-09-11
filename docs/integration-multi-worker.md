# Integration branch: multi-worker stack

**`integration/multi-worker` is never merged.** It exists so a person can run the
whole multi-worker design before its PRs land, and so CI exercises the heads
together. Every line belongs to a PR below; merging it would bury fifteen
reviewable diffs in one commit. Land the PRs, then delete this branch.

## Merge order (each a `--no-ff` merge of the branch head)
| # | PR | Branch | Head |
|---|----|--------|------|
| 1 | #4108 | feat/spawn-worker-launcher | 4c8536c7c |
| 2 | #4110 | feat/pool-router-pass | 42279eae7 |
| 3 | #4115 | feat/create-worker-command | a729f7c3c |
| 4 | #4119 | feat/advertise-pool-to-broker | 9053d1ad6 |
| 5 | #4120 | feat/worker-picker-commands | ff1abf050 |
| 6 | #4121 | feat/picker-structured-command | d011246ff |
| 7 | #4162 | feat/bridge-advertise-reader | 9e65e8410 |
| 8 | #4175 | fix/result-stamp-finds-pool-worker | 43d8b53cc |
| 9 | #4176 | fix/pool-roster-refuses-silent-loss | b31babf38 |
| 10 | #4167 | feat/watcher-inbox-and-workspace-seams | ca2eec0f7 |
| 11 | #4163 | fix/restart-warns-watcher-stopped | 62674c0f1 |
| 12 | #4168 | fix/restart-stops-only-own-watcher | 72e63cf27 |
| 13 | #4174 | feat/watcher-sentinel-record | 6936a7e03 |
| 14 | #4164 | fix/handler-runner-evidence | 54ed5fd07 |
| 15 | #4165 | fix/reaper-reads-runner-rc | c10543bb9 |

## Trying it
Deploy order: core first, then workers — the roster is the core's artifact.

1. `python3 scripts/create-worker.py --folder <dir> --label "<name>"` — one
   command for identity, delivery folder, session, binding and roster row.
2. It spawns the session with four env seams — also what to set by hand when
   driving a worker without the spawner:
   `SUTANDO_INSTANCE_ID` (this instance's id; unset means "I am the core"),
   `SUTANDO_TASKS_DIR` (its inbox — `<workspace>/deliveries/<id>` for a worker),
   `SUTANDO_WORKSPACE_DIR` (named explicitly, never inferred from the inbox),
   `SUTANDO_INBOX_KIND` (`tasks` or `deliveries`; a sentinel inbox is read as
   names pointing at payloads, not task bodies).
3. The core routes with `src/pool_router.py` + `src/pool_route_handler.py`;
   `src/worker_picker_commands.py` answers who took what.
