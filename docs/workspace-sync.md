# Workspace sync across machines

Sutando supports running the same agent identity across multiple machines (e.g. Mac mini + MacBook + Mac Studio). Each machine runs its own Claude Code session; a shared private git repo keeps the workspace consistent between them — notes, memory, state, and any custom directories the workspace contains.

The mechanism is intentionally minimal: a single shell script (`scripts/sync-workspace.sh`) invoked on a cron tick. The workspace itself IS the git working tree; each machine pushes its writes to a per-host branch and pulls from the others to merge.

## When you want this

- You run Sutando on more than one machine and want the agent to share workspace state across them.
- You want a private, owner-controlled audit log of what the agent has learned over time (every workspace write is a git commit).
- You want fleet coordination without standing up a database or message broker — just a private GitHub repo.

If you only run Sutando on one machine, you don't need this.

## Setup (one-time, per machine)

1. **Create a private GitHub repo** for your workspace (any name, e.g. `your-org/your-workspace`). The workspace tree will be pushed there as branches; you don't need to scaffold any contents.

2. **Configure the vault URL.** Either:
   - **Preferred:** `sutando.config.local.json` under `vault.remote_url` (per-clone, gitignored):
     ```json
     {
       "vault": {
         "remote_url": "https://github.com/your-org/your-workspace.git"
       }
     }
     ```
   - **OR:** pass `--vault-url <url>` per invocation
   - **Legacy:** `.env` with `SUTANDO_MEMORY_REPO=https://github.com/your-org/your-workspace.git` (honored for the deprecation window; see [Migration from `sync-memory.sh`](#migration-from-sync-memorysh) below)

3. **Initialize the workspace as a git repo:**
   ```bash
   bash scripts/sync-workspace.sh --init
   ```
   First run sets the workspace tree up as a git working copy, points at the configured remote, and pushes whatever's locally on disk as the initial commit on the per-host branch.

4. **Schedule the recurring sync.** Use the installer — it picks the right
   scheduler per OS (a per-user **LaunchAgent** on macOS, a **crontab** line on
   Linux) and defaults to every 15 min:
   ```bash
   bash scripts/install-workspace-sync.sh                 # install (idempotent)
   bash scripts/install-workspace-sync.sh --interval 600  # custom seconds
   bash scripts/install-workspace-sync.sh --status        # check it's loaded
   bash scripts/install-workspace-sync.sh --uninstall
   ```

   > **macOS: prefer launchd, not a hand-written crontab.** The cron spool
   > (`/var/at/tabs`) is TCC-protected — editing a crontab requires the
   > controlling **terminal app** to have **Full Disk Access**, or `crontab`
   > fails with `Operation not permitted` (the spool is root-owned and gated on
   > the *responsible app*, not euid, so even setuid-root `crontab` is blocked).
   > The installer's LaunchAgent needs no FDA, and — running in the `gui/<uid>`
   > domain — also unlocks the login Keychain, sidestepping the `-25308` error
   > (see Troubleshooting). To wire it by hand instead, a plain cron line works
   > on Linux (and on macOS *if* your terminal has FDA):
   > ```cron
   > */15 * * * * cd /path/to/sutando && bash scripts/sync-workspace.sh
   > ```

Repeat the same steps on every machine in the fleet. All clone the same `vault.remote_url`. Each machine pushes to its own branch named `host/<hostname>/<wsId>` (per [#1459](https://github.com/sonichi/sutando/pull/1459)); the merge across branches happens on next pull.

## What gets synced

The workspace tree is the unit of sync. Every file under `<workspace>/` is potentially trackable; the per-clone `.gitignore` and the workspace's own gitignore determine what actually flows.

| Source | Synced to | Notes |
|---|---|---|
| `<sutando workspace>/` (everything not excluded) | Single private repo (`vault.remote_url`), branch `host/<hostname>/<wsId>` per workspace | Workspace IS the working tree. Per-host branch + automatic merge across hosts. |
| `<workspace>/.claude-sutando/projects/<slug>/memory/` | Same repo, same branch | Claude Code auto-memory files synced alongside notes + state. |
| `<workspace>/notes/` | Same repo, same branch | Long-form notes. No symlink needed — they're already in the workspace tree. |

## What does NOT get synced

**Defaults — never tracked:**

- **Per-host runtime state** — `state/auth/` (per-host install / device identity — never tracked, never overwritten by sync), `core-status.json`, `contextual-chips.json`, `.env` files. These are local to each machine.
- **Build artifacts** — generated videos, screenshots, derived caches.
- **Anything in `<workspace>/.git/info/exclude`** — this is where the carrier writes its sync rules as of [#1460](https://github.com/sonichi/sutando/pull/1460), so the workspace's user-tracked `.gitignore` stays clean of system-level excludes.

**Customize per workspace** — `sync-workspace.sh` reads `vault.sync.include` and `vault.sync.exclude` from `sutando.config.local.json` to extend or contract what's tracked. The carrier writes these patterns into `<workspace>/.git/info/exclude` on each sync tick (mechanism: [#1447](https://github.com/sonichi/sutando/pull/1447) + [#1460](https://github.com/sonichi/sutando/pull/1460) — not the workspace's `.gitignore`, to avoid leaking the carrier-set rules into operator-tracked state).

```json
{
  "vault": {
    "remote_url": "https://github.com/your-org/your-workspace.git",
    "sync": {
      "include": ["custom-research/", "drafts/"],
      "exclude": ["state/cache/", "data/large-snapshots/"]
    }
  }
}
```

Patterns are standard gitignore syntax.

## Conflict model

Concurrent writes from two machines land on different branches (`host/<host-A>/<wsId>` and `host/<host-B>/<wsId>`); the merge happens locally on each host's next sync tick via git's standard 3-way merge. Same-file edits within the same minute on two hosts produce a normal git merge conflict that gets surfaced via stderr — `sync-workspace.sh` doesn't silently pick a winner.

To avoid conflicts in the first place:

- **Prefer append-only files** for shared state (`build_log.md`, `MEMORY.md` index entries).
- **Avoid simultaneous edits** to the same memory file from two machines.
- If you hit a conflict, the conflict markers appear in the file; resolve manually or `git reset --hard origin/host/<host>/<wsId>` to take the remote's version.

First-cross-host pull is handled specially — [#1458](https://github.com/sonichi/sutando/pull/1458) catches the "unrelated histories" git error from the initial bootstrap and lets the pull proceed via `--allow-unrelated-histories`.

## Config keys

| Key | Default | Notes |
|---|---|---|
| `vault.remote_url` | (required) | git URL of your private workspace repo |
| `vault.sync.include` | `[]` | extra gitignore-negation patterns to add to the carrier |
| `vault.sync.exclude` | `[]` | extra gitignore patterns to add to the carrier |
| `--vault-url` (CLI flag) | (overrides config) | per-invocation override |

The workspace path is resolved via the standard helper (`bash scripts/sutando-config.sh workspace`); no separate sync-side configuration needed.

## Migration from `sync-memory.sh`

The legacy `scripts/sync-memory.sh` (rsync-to-`~/.sutando/memory-sync/`) is **deprecated** as of v0.3.0 and will be **removed in v0.4.0**. The script still works during the deprecation window but emits a stderr banner on each invocation.

To migrate:

1. **One-time per machine:**
   ```bash
   bash scripts/sync-workspace.sh --init
   ```
   This converts the workspace into a git repo + sets the remote.

2. **Move vault URL out of `.env`:** delete `SUTANDO_MEMORY_REPO` from `.env`; add the URL to `sutando.config.local.json` under `vault.remote_url` (per the Setup section above). Per-invocation `--vault-url` also works.

3. **Update cron:** replace any cron entry calling `sync-memory.sh` with one calling `sync-workspace.sh`. See `skills/schedule-crons/crons.example.json` for the new entry.

4. **Verify** by checking that the per-host branch appears on the remote after the next cron tick.

After the migration, you can safely remove `~/.sutando/memory-sync/` (the legacy clone) — the new flow doesn't use it.

## Migration risks + pre-flight checklist

Adopting sync-workspace.sh on an existing Sutando install is a structural migration: the workspace gains a `.git` directory, sync rules are written, and bridges restart. The risks below come from real first-hand migration runs (#1467 thread + Lucy's Maddy v0.8 report 2026-06-06).

### Pre-migration (every host, before `--init`)

| Check | Why |
|---|---|
| `vault.remote_url` set in `sutando.config.local.json` | First-run `--init` reads it; missing → error halt |
| `SUTANDO_WORKSPACE` removed from shell rc (`~/.zshrc` / `~/.bash_profile`) AND from workspace `.env` | Stale env causes startup re-migrate-every-boot loop |
| Workspace size scoped — exclude large media (`notes/asset-library/`, video, `data/large-snapshots/`) via `vault.sync.exclude` BEFORE `--init` | Pre-migration backup tars the whole workspace; uncompressible mp4 freezes `tar -czf` for 30+ min on a 30+ GB workspace |
| `node_modules/` dirs excluded via `vault.sync.exclude` (also nested under surface dirs like `notes/asset-library/`) | Regenerable build artifact (`npm install`) — large, no-value in vault, and uncompressible → slow backup |
| Active voice/phone/discord-voice sessions ended | They drop on bridge restart anyway; explicit shutdown is cleaner |
| `.env` is in `vault.sync.exclude` | Contains secrets; must NEVER sync |
| `tasks/`, `results/`, `state/cores/<host>.alive`, `conversation.sqlite`, `.DS_Store` are in `vault.sync.exclude` (or default gitignore) | Per-host runtime / per-host data — must not sync |

### Migration order (across multiple hosts)

**Serial, not parallel.** One host at a time:

1. Pre-migration checklist (above) on host N.
2. `bash scripts/sutando-migrate.sh --dry-run` → review the plan.
3. `bash scripts/sutando-migrate.sh --commit` → real migration.
4. `bash scripts/sync-workspace.sh --init` → workspace becomes git repo.
5. Restart bridges + voice-agent + Sutando.app.
6. Verify: `python3 src/health-check.py` reports all paths via M0 helper cleanly.
7. **Verify sync hygiene:** immediately after `core_heartbeat.py` writes its first `.alive`, run `cd <workspace> && git status` — should show no changes (heartbeat files must be excluded). If not, fix exclusion rules before pushing.
8. Next host.

Why serial: branch D/F collisions are possible on simultaneous first-pushes pre-#1463; serial avoids the race entirely.

### What used to break (resolved in v0.3.0 — historical reference)

The three migration symptoms below were observed pre-v0.3.0 and all shipped fixes in this release. Listed here so anyone debugging an older migration recognizes the pattern; if you're running v0.3.0 or later, none of these apply:

- **Claude memory not auto-moved** — Fixed by [#1475](https://github.com/sonichi/sutando/pull/1475): `sutando-migrate --commit` now auto-invokes `--import` to copy `~/.claude/projects/<slug>/memory/` into `<ws>/.claude-sutando/projects/<slug>/memory/`, with a slug-rename bridge for the `<repo-slug>` → `<repo-slug>-workspace` variant.
- **`sync-workspace.sh` plain run silently commits the entire workspace** — Fixed by [#1483](https://github.com/sonichi/sutando/pull/1483): the script now refuses push/pull when `.git` is present but sync was never initialized, with a clear error directing operators to run `--init` first.
- **First migration with `SUTANDO_WORKSPACE` set re-migrates every boot** — Fixed by [#1478](https://github.com/sonichi/sutando/pull/1478): `src/startup.sh` honors `sutando-migrate`'s per-source sentinels (`state/.migrated-from-<tag>-<id>`), suppressing the re-migrate loop when the env var is still set in shell rc after a manual migration. The residual deprecation banner keeps firing until the env line is removed — see step 2 of the [pre-migration operator checklist](../KNOWN_ISSUES.md).

## Troubleshooting

- **`sync-workspace: vault.remote_url not set` / `--vault-url missing`** — configure per the Setup section above.
- **`Another sync already in progress, exiting.`** — A previous cron tick is still running. The script self-clears stale locks after 10 minutes; if you see this repeatedly, check `/tmp/sync-workspace.log` for the previous tick's error.
- **`refusing to push to non-host branch '...'`** — Someone manually `git checkout`-ed a feature branch in the workspace clone. Switch back to your `host/<host>/<wsId>` branch.
- **Push fails with auth error** — Check that your machine has push access to the vault repo (`gh auth status` if you use the GitHub CLI). Read-only clones won't push.
- **Push fails with macOS Keychain error `-25308` over SSH or plain cron** — `gh auth` stores the GitHub token in macOS Keychain, which is bound to a GUI session by default. Plain SSH sessions / system-level crontabs can't unlock it. Three fixes (pick one):
  1. **SSH remote URL** *(recommended for headless/SSH)*: configure the vault with `git@github.com:user/vault.git` instead of HTTPS — SSH uses key-based auth, no Keychain involved. Switch with `git -C "$(bash scripts/sutando-config.sh workspace)" remote set-url origin git@github.com:user/vault.git`.
  2. **`GH_TOKEN` in `.env`**: add `GH_TOKEN=<personal-access-token>` to `<workspace>/.env` (resolve `<workspace>` via `bash scripts/sutando-config.sh workspace`). git and `gh` respect this env var without touching Keychain. *Note:* the M2 sync engine carries the workspace tree cross-host, so a `GH_TOKEN` in `<workspace>/.env` propagates to every fleet machine; that's typically desired for headless sync hosts, but worth being aware of for multi-host operators.
  3. **launchd plist** *(recommended on macOS)*: run sync from a launchd plist scoped to your user GUI session (not system-level crontab) — `bash scripts/install-workspace-sync.sh` does exactly this for you. The `gui/<uid>` domain unlocks the login Keychain, and it also avoids the separate Full-Disk-Access wall that blocks `crontab` edits (see step 4 of Setup). Hand-rolled alternative: unlock the keychain in a cron wrapper (`security unlock-keychain` — requires interactive password setup, not recommended for automation).
- **Merge conflicts on every tick** — Two hosts editing the same file in tight loops. Use append-only patterns or coordinate edits.

## Related

- [`docs/workspace-config.md`](workspace-config.md) — how the workspace path itself is resolved (different concern from sync)
- [`docs/workspace-contract.md`](workspace-contract.md) — the workspace data contract
- [`docs/release-process.md`](release-process.md) — release timeline for `sync-memory.sh` removal (deprecated v0.3.0, removed v0.4.0)
