# Memory + notes sync across machines

Sutando supports running the same agent identity across multiple machines (e.g. Mac mini + MacBook + Mac Studio). Each machine runs its own Claude Code session; a shared private git repo keeps the agent's memory and long-form notes consistent between them.

The mechanism is intentionally minimal: a single shell script (`scripts/sync-memory.sh`) that's invoked on a cron tick. It pulls everyone's latest writes, copies this machine's local edits into the shared repo, commits with the hostname, and pushes.

## When you want this

- You run Sutando on more than one machine and want the agent to share memory across them.
- You want a private, owner-controlled audit log of what the agent has learned over time (every memory write is a git commit).
- You want fleet coordination without standing up a database or message broker — just a private GitHub repo.

If you only run Sutando on one machine, you don't need this.

## Setup (one-time, per machine)

1. **Create a private GitHub repo** for your memory + notes (any name, e.g. `your-org/your-memory`). It will hold `memory/` and `notes/` directories, plus any per-host scratch you opt to track.

2. **Add the repo URL to `.env`** in your sutando workspace:
   ```
   SUTANDO_MEMORY_REPO=https://github.com/your-org/your-memory.git
   ```

3. **Run once** from the sutando working tree:
   ```bash
   bash scripts/sync-memory.sh
   ```
   First run auto-clones to `~/.sutando/memory-sync/` and pushes whatever's locally in `memory/` + `notes/` as the initial commit.

4. **Add a cron entry** to run every 10–30 minutes:
   ```cron
   */15 * * * * cd ~/Desktop/sutando && bash scripts/sync-memory.sh
   ```

Repeat the same steps on every machine in the fleet. All clone the same `SUTANDO_MEMORY_REPO`. Each machine's commits are signed with `Sync <hostname> <ISO timestamp>` so you can tell who wrote what.

## What gets synced

| Source | Synced to | Notes |
|---|---|---|
| `~/.claude/projects/<workspace-hash>/memory/*.md` | `~/.sutando/memory-sync/memory/` | Claude Code auto-memory files. Append-only is safest. |
| `<sutando workspace>/notes/` | `~/.sutando/memory-sync/notes/` | Long-form notes. Symlink pattern recommended (see below). |

## What does NOT get synced

- **Per-host runtime state** — `core-status.json`, `contextual-chips.json`, anything in `state/`, `.env` files. These are local to each machine.
- **Build artifacts** — generated videos, screenshots, derived caches.
- **Anything in `.gitignore`** of your memory repo.

## Conflict model

Concurrent edits from two machines land via `rsync --update --checksum` (mtime-wins). If both machines edit the same file in the same minute, the later push wins. To avoid losses:

- **Prefer append-only files** for shared state (`build_log.md`, `MEMORY.md` index entries).
- **Avoid simultaneous edits** to the same memory file from two machines.
- If you hit a conflict, the loser's edit is in the previous commit on `~/.sutando/memory-sync/` — recover via `git log -p memory/the-file.md`.

The script self-heals if the local sync clone gets stuck on a non-`main` branch.

## Env vars

| Variable | Default | Notes |
|---|---|---|
| `SUTANDO_MEMORY_REPO` | (required) | git URL of your private memory repo |
| `SUTANDO_WORKSPACE` | `~/Desktop/sutando` | path to your sutando working tree |
| `SUTANDO_MEMORY_SYNC_DIR` | `~/.sutando/memory-sync` | local clone path |

## Optional: notes/ as a symlink

If you want `<workspace>/notes/` and `~/.sutando/memory-sync/notes/` to be the same directory (so editing a note instantly syncs without a round-trip through `cp`), symlink one to the other on every machine:

```bash
mv ~/Desktop/sutando/notes ~/Desktop/sutando/notes.legacy-backup
ln -s ~/.sutando/memory-sync/notes ~/Desktop/sutando/notes
```

Memory files (`~/.claude/projects/.../memory/`) can't be symlinked the same way because Claude Code creates new files in that directory at runtime; let the script's `cp_if_newer` handle them.

## Troubleshooting

- **`sync-memory: SUTANDO_MEMORY_REPO not set in .env, skipping sync.`** — Add the variable to your `.env` per step 2 above.
- **`Another sync already in progress, exiting.`** — A previous cron tick is still running. The script self-clears stale locks after 10 minutes; if you see this repeatedly, check `/tmp/sync-memory.log` for the previous tick's error.
- **`sync repo on non-main branch '...'`** — Someone manually `git checkout`-ed a feature branch in `~/.sutando/memory-sync/`. The script auto-recovers by switching back to `main`.
- **Push fails** — Check that your machine has push access to the memory repo (`gh auth status` if you use the GitHub CLI). Read-only clones won't push.
