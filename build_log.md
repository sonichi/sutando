
## Pass 207 (2026-05-25)

**fix(health-check): multi-bridge source-path warn** (commit `d9a384c` on main):
- `multiple processes` warn now shows full script paths per PID
- Before: `2 PIDs: 25023,25065` — opaque, unactionable
- After: `25023:/Applications/Sutando.app/.../discord-bridge.py, 25065:/Users/.../sutando/src/discord-bridge.py`
- Root cause of dual-bridge: app-bundle startup.sh + dev repo both launched bridges at 5:20AM — different source paths, different workspaces
- 5 structural tests in `tests/health-check-bridge-multi-pid.test.py`

**Push go-ahead needed**: `main` branch has this commit (not yet pushed to origin/main).

## Pass 208–209 (2026-05-25)

**feat(content): Ep4 LinkedIn park walk post — READY TO POST**
- Created `notes/linkedin-ep4-park-walk-final.md` — polished LinkedIn copy for Ep4
- Text-only (Option C), no filming required — full narrative arc from Twitter thread expanded for LinkedIn
- Supersedes `linkedin-ep4-draft.md` (that file has the stale Meeting Gap/Linear angle)
- Status: copy-paste ready; posting window Tue–Thu 8–10am or 12–1pm local

**feat(obsidian): mirror script + 44 tests** (already on `merge/stando-sync`, commit `34ecea7`):
- `src/obsidian-mirror.py` — sonichi #1082 port, idempotency bug fixed in `_write_result_mirror`
- `tests/obsidian-mirror.test.py` — 44 tests, all passing, importlib import pattern for hyphenated filename
- `skills/obsidian-vault/SKILL.md`, `manifest.json`, `scripts/dream.py`, `tools.ts` ported

**Pending push go-aheads**:
- `port/watch-tasks-attempts-stando-63` (10 tests, `4852e48`)
- `port/share-screen-standalone-1073` (4 tests, `6f3eca0`)
- `port/result-channel-key-1090` (48 tests, `c138b2d`)
- `port/za-warudo-mention-precedence-1091` (11 tests, `2b5828c`)
- `port/codeql-stack-trace-1092` (10 tests, `cf10b7f`)
- `merge/stando-sync` (162 commits ahead of origin/main)
- `main` (health-check + obsidian-vault commits)
- `fix/slack-bridge-py39-annotations` (3 tests added, commit `990fc11`)

## Pass 210 (2026-05-25)

**test(slack-bridge): py39 regression guard** (commit `990fc11` on `fix/slack-bridge-py39-annotations`):
- Added 3 structural tests: `from __future__ import annotations` present + at top + `str | None` still exists
- Branch now push-ready (was missing tests per "every script ships with tests" rule)

**content(ep4): confirmed final LinkedIn post fixed + memory updated**:
- `notes/linkedin-ep4-park-walk-final.md` corrected to Bassil's confirmed "THIS IS IT" version (shorter)
- Earlier pass had an unapproved expanded draft — replaced with the exact confirmed text from memory
- `project-always-on-content.md` memory updated: "filming pending" → text-only, files listed

**Push go-ahead needed** (new additions this pass):
- `fix/slack-bridge-py39-annotations` (3 tests, `990fc11`)

## Pass 211 (2026-05-25)

**feat(x-post): thread subcommand** (commit `07445a0` on `feat/x-post-thread-command`):
- `skills/x-twitter/x-post.py` — added `thread` subcommand: reads `---`-delimited file, posts each section as reply-to-previous chain
- `--dry-run` flag prints all tweets without API call; `--delay` controls inter-tweet gap (default 2s)
- 9 tests in `tests/x-post-thread.test.py` — all passing
- **Unlocks**: `python3 skills/x-twitter/x-post.py thread --file <path>` for Ep4, Ep5, WWDC threads

**feat(content): WWDC June 10 dev thread file created** (`skills/x-twitter/threads/wwdc-2026-dev-thread.txt`):
- 7-tweet dev angle: "Build your own proactive agent before Apple ships theirs"
- Ready to post June 10 with: `python3 skills/x-twitter/x-post.py thread --file skills/x-twitter/threads/wwdc-2026-dev-thread.txt`
- The note at `notes/wwdc-june10-dev-twitter-thread.md` referenced this file but it didn't exist — gap closed

**Outreach signals (fresh scan 05:57 May 25):**
- 12 signals, same as yesterday — no new companies, no fills
- Carta CoS (Chief Product Officer) now day 3 — still open, highest urgency
- Still blocked on HUNTER_API_KEY + INSTANTLY_API_KEY

**Push go-ahead needed** (new this pass):
- `feat/x-post-thread-command` (9 tests, `07445a0`)

## Pass 212 (2026-05-25)

**feat(content): Ep5 barber thread file + test** (commit `05181a4` on `feat/x-post-thread-command`):
- `skills/x-twitter/threads/always-on-ep5-barber.txt` — 5-tweet thread for Ep5 barber chair delegation story
- The concept note referenced this file but it didn't exist (same gap as WWDC dev thread, now closed)
- 10/10 tests passing (added `test_ep5_thread_file_parseable`)
- Post command: `python3 skills/x-twitter/x-post.py thread --file skills/x-twitter/threads/always-on-ep5-barber.txt`

**Note (from Discord log):** agent-universe PR #48 (WebView OAuth fix) already merged — task was processed and result archived correctly.

**All thread files now complete:**
- Ep4: `always-on-ep4-park-walk.txt` (6 tweets) ✓
- Ep5: `always-on-ep5-barber.txt` (5 tweets) ✓ NEW
- Ep6 (park-demo): `always-on-ep6-park-demo.txt` ✓
- WWDC dev (June 10): `wwdc-2026-dev-thread.txt` (7 tweets) ✓

## Pass 213 (2026-05-25)

**feat(cross-node-sync): configurable scope via JSON config (closes #1044)** (commit `a072033` on `feat/cross-node-sync-config-1044`):
- `skills/cross-node-sync/scripts/setup-rsync-sync.sh` — reads `$SUTANDO_WORKSPACE/config/cross-node-sync.json` to enable/disable memory/notes/assets/data scopes independently
- `_scope_enabled()` bash function with inline Python JSON parsing; falls back to all-enabled on missing file or invalid JSON
- `skills/cross-node-sync/config/cross-node-sync.example.json` — example config ships with skill
- 15 tests in `tests/cross-node-sync-config.test.py` — all passing; uses `patched_sync_peer()` context manager to work around `.env` loading that overrides env vars
- Filed by Susan Xueqing Liu — enables per-operator customization without script PRs
- Cherry-picked onto `merge/stando-sync` (`42fb28d`)

**Pending push go-ahead** (new this pass):
- `feat/cross-node-sync-config-1044` (15 tests, `a072033`)

## Pass 214 (2026-05-25)

**fix(watch-tasks-stream): Node .mjs closes both orphan paths (closes #1088)** (commit `8751618` on `fix/watch-tasks-orphan-1088`):
- `src/watch-tasks-stream.mjs` (new) — Node implementation replaces fswatch bash script
  - **Mode A (EPIPE)**: `process.stdout.on('error')` handler exits on EPIPE/EBADF; 30s heartbeat (`HEARTBEAT_INTERVAL_MS` override) forces EPIPE detection when tasks dir is quiet
  - **Mode B (PPID reparent)**: polls `process.ppid` every 10s, exits when parent gone; also handles SIGHUP
  - Inline `bumpAttemptsCounter()` (no external Python dep, graceful degradation)
  - Workspace resolution: SUTANDO_WORKSPACE → ~/.sutando/workspace (no fswatch dep)
- `src/watch-tasks-stream.sh` — updated to thin wrapper (`exec node watch-tasks-stream.mjs`)
- 15 tests in `tests/watch-tasks-stream-orphan.test.py` — all passing (T14 validates EPIPE exits in ≤5s)
- Issue filed by Susan Xueqing Liu; reproduces 2026-05-23 4h task-loss incident (3 owner DMs unprocessed)
- Note: cherry-pick to merge/stando-sync conflicted (stando-sync has divergent .mjs); will resolve at merge time

**Pending push go-ahead** (new this pass):
- `fix/watch-tasks-orphan-1088` (15 tests, `8751618`)
