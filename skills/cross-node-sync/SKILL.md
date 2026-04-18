---
name: cross-node-sync
description: "Rsync-over-ssh sync between Sutando nodes (Mac Studio and MacBook) for shared memory + notes. Optional — core runs fine without it; enables automatic cross-bot learning and note propagation by running from the proactive-loop cron on each pass."
user-invocable: false
---

# cross-node-sync

Rsync-over-ssh cross-node sync for Sutando-Studio (Mac Studio) and Sutando-Mini (MacBook). Shares bot memory and user notes so both nodes converge automatically on each proactive loop pass.

## Why rsync (not Syncthing)

Initial design used Syncthing (peer-to-peer daemon, continuous sync). Pivoted to rsync-over-ssh after manifest comparison showed the sync scope is narrow (17 memory files, 34 notes) — a daemon + web UI is overkill. Rsync wins on:

- **No new binary** — rsync is macOS-native, no brew install needed.
- **No daemon** — fires from the existing proactive-loop cron, no process to babysit.
- **Auditable** — each sync pass logs exactly what moved; `--dry-run` preview is first-class.
- **SSH-keyed auth** — reuses the ssh trust relationship you already have for git, no Device-ID pairing dance.
- **Lighter on disk** — no `.stversions/` per-file versioning, no config.xml, no index database.

Syncthing would still be the right call if: scope grew past a few hundred files, we hit conflict frequency > daily, or we wanted sub-second propagation. Neither is true today.

## Scope

**Syncs (both directions, union semantics via `rsync --update`):**
- `~/.claude/projects/-Users-xueqingliu-Documents-sutando-sutando/memory/` — cross-session bot memory
- `<repo>/notes/` — user's second-brain notes

**Excluded (per-node state):**
- `state/`, `tasks/`, `results/`, `logs/` — per-bot queues + histories
- `core-status.json`, `build_log.md`, `contextual-chips.json` — per-bot proactive state
- `.env`, `~/.claude/channels/*/.env` — different tokens per node
- `data/voice-metrics.jsonl` — per-node
- `src/.discord-pending-replies.json` (legacy location), `src/Sutando/SutandoApp` (Mac binary build artifact)
- `~/.claude/projects/` (other projects), `~/.claude/skills/` (installed per-node)
- `.DS_Store`, `*.swp`, `*.swo`, `.stversions`, `.stfolder` (OS/editor/Syncthing-legacy noise)

## Setup

Two steps, once per node:

```bash
# 1. Generate / authorize SSH key on the peer (prints instructions)
bash skills/cross-node-sync/scripts/setup-rsync-sync.sh --setup

# 2. Set the peer host — the ONLY required env var
echo 'export SUTANDO_SYNC_PEER="susan@MacBook-Pro.local"' >> .env   # on Studio
echo 'export SUTANDO_SYNC_PEER="susan@Mac-Studio.local"'  >> .env   # on Mini
```

`SUTANDO_PEER_MEM_DIR` / `SUTANDO_PEER_NOTES_DIR` default to the **same literal paths as local**, so the defaults only work when both nodes share the same OS username AND the same repo location. In practice that's rare (e.g. Studio's `/Users/xueqingliu/...` vs MacBook's `/Users/xliu/...`), so most setups will want to set them explicitly:

```bash
# Example: Studio talking to a MacBook with a different username + repo path
export SUTANDO_PEER_MEM_DIR="/Users/xliu/.claude/projects/-Users-xliu-.../memory/"
export SUTANDO_PEER_NOTES_DIR="/Users/xliu/path/to/sutando/notes/"
```

Get the peer's values with `ssh $SUTANDO_SYNC_PEER 'echo $HOME; ls -d ~/.claude/projects/-*sutando*'`.

**Cron wiring is automatic:** the `cross-node-sync` entry already lives in `skills/schedule-crons/crons.example.json`, so the usual first-time `cp skills/schedule-crons/crons.example.json skills/schedule-crons/crons.json` wires the 7-minute sync into the proactive-loop crons with no manual JSON editing. (7 min chosen to avoid `:00/:30` collision with other crons.)

**Manual sync (optional):**
```bash
bash skills/cross-node-sync/scripts/setup-rsync-sync.sh --dry-run   # preview
bash skills/cross-node-sync/scripts/setup-rsync-sync.sh             # actual run
```

## Conflict handling

Two-direction rsync with `--update` flag: files are copied to the receiver only if newer than the receiver's copy. If both nodes edited the same file since last sync, the node with the later mtime wins. For our scope (mostly append-only memory + notes), conflicts are rare.

If conflicts become a problem, add `--backup --backup-dir=../.sutando-sync-conflicts/` to preserve losers for manual merge. Not needed day one.

## Diagnostics

```bash
# Show what would sync without doing it
bash skills/cross-node-sync/scripts/setup-rsync-sync.sh --dry-run

# Show SSH keypair state + setup instructions
bash skills/cross-node-sync/scripts/setup-rsync-sync.sh --setup

# Run the smoke tests
bash skills/cross-node-sync/scripts/test-setup-rsync-sync.sh
```

## Status

- 2026-04-17: Design approved in #susan. Syncthing prototype replaced with rsync after manifest comparison. `setup-rsync-sync.sh` + `test-setup-rsync-sync.sh` (10/10 pass) committed locally, NOT pushed. Still awaiting Mini's actual file inventory to validate the sync scope closes the gap.
