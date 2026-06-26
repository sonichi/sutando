# The `hosts/<hostname>/` per-host convention

**Status:** spec (greenlit by Chi 2026-06-20). Establishes the carrier + naming
convention now; per-component relocation lands as follow-on PRs (see "Migration").

## Problem it solves

The workspace-revamp sync (`scripts/sync-workspace.sh`) is **branch-per-host**:
each host pushes to `host/<hostname>/<wsId>` and pulls peers via 3-way merge.
That isolates on *push* but **re-collides same-path files on *pull***. So the
old `sync-memory.sh` model — which namespaced *everything* per host under
`machine-<hostname>/` — had a property the revamp lost: a safe home for
**per-host config** that must be backed up but must never merge across hosts.

Result (from the gap analysis, #bot2bot 2026-06-20): the revamp carries memory /
notes / build_log / crons but **drops per-host config backup** —
PERSONAL_CLAUDE.md, stand-identity.json, tab-aliases.json, channel access.json,
settings.json, personal skills, avatar. On a rebuild they're gone.

This convention restores the per-host namespace, the revamp way.

## Data model: shared vs per-host

The workspace holds two data classes, and the whole design falls out of keeping
them separate:

- **Shared data** — core memory (`projects/*/memory/`), `notes/`. Every host
  reads *and* writes it; all hosts converge to the same content. Synced by the
  `vault.sync.include` whitelist and **converged via the 3-way merge**
  `sync-workspace.sh` already does. Two hosts touching the same shared file is an
  ordinary git merge (a true conflict only if the same lines change
  concurrently). (`build_log.md` and `pending-questions.md` were reclassified
  **per-host** per the F1 design decision — owner override of an earlier "shared"
  call — and now live under `hosts/<hostname>/`; see the table below.)
- **Per-host data** — everything under `hosts/<hostname>/` (see table below).
  **Single-writer:** only the owning host writes its own subtree. Disjoint paths
  → merging host branches never collides.

## The layered design

Five orthogonal layers, **no shared branch** (each host keeps its own
`host/<hostname>/<wsId>` ref — push isolation, zero contention):

1. **branch-per-host** — the sync/isolation ref. Each host pushes only its own
   branch; peers arrive via fetch + 3-way merge.
2. **`hosts/<hostname>/`** — the per-host namespace. Hostname-qualified, so
   single-writer and collision-free on pull.
3. **shared paths, merged** — shared data stays at its shared paths and converges
   through the existing 3-way merge. Not relocated under `hosts/`.
4. **sparse-checkout (working-tree view)** — each host *cones* its checkout to
   shared paths + its own `hosts/<hostname>/`, so peers' subtrees don't clutter
   the working tree or get edited by accident. **Sparse is a view, not a
   write-guard** — it controls what materializes, not what a buggy writer can
   commit. It's ergonomics; the actual guarantee is layers 2 + 5. Optional: a
   host that never needs peers' subtrees locally barely needs it.
5. **lint** (`scripts/lint-vault-sync-paths.sh`, #1716) — the enforcement layer.
   Fails CI if a per-host file is carried outside a hostname-qualified path, so
   the single-writer invariant can't silently regress.

## The convention

A single per-host subtree under the workspace:

```
<workspace>/hosts/<hostname>/...
```

- `<hostname>` = `hostname | sed 's/\..*//'` — the **same host slug** the sync
  layer uses (so it lines up with the `host/<hostname>/<wsId>` branch).
- **Single-writer:** a host writes ONLY its own `hosts/<hostname>/` subtree.
  It never writes a peer's. This is the load-bearing property — it makes
  cross-host merge conflicts **impossible by construction** (the failure the
  revamp otherwise reintroduces), restoring the `machine-<host>/` safety.
- **Carried** as `hosts/*/` in `vault.sync.include` — the `*` glob is
  hostname-qualified, so it passes `scripts/lint-vault-sync-paths.sh` and never
  collapses. Each host's subtree syncs + backs up; peers' subtrees arrive
  side-by-side after pull, never merged.

## What lives under `hosts/<hostname>/`

Per-host **config that should survive a rebuild** (the backup hole):

| File | Was (main `sync-memory.sh`) | New |
| --- | --- | --- |
| PERSONAL_CLAUDE.md | machine-<host>/ | hosts/<hostname>/PERSONAL_CLAUDE.md |
| stand-identity.json | machine-<host>/ | hosts/<hostname>/stand-identity.json |
| tab-aliases.json | machine-<host>/ | hosts/<hostname>/tab-aliases.json |
| channel access.json (allowlist/tierMap/TOFU) | machine-<host>/channels/ (Mini's #1715) | hosts/<hostname>/channels/<ch>/access.json |
| settings.json snapshot | (unbacked) | hosts/<hostname>/settings.json |
| crons | `crons/<hostname>.json` (#1716) | `hosts/<hostname>/crons.json` — **wired** in `schedule-crons/SKILL.md` (self-heals from the interim/legacy path) |
| build_log.md | machine-<host>/ (per-host) | `hosts/<hostname>/build_log.md` — F1 per-host decision; migrator (#1721) emits it; *loop write-side still emits workspace-root, relocation deferred* |
| pending-questions.md | machine-local (per-host) | `hosts/<hostname>/pending-questions.md` — reader wired via `personal_path` (#1718) + migrator (#1721) |

### Wiring status (implemented vs deferred)

The `hosts/<hostname>/` paths above are the **target** layout. The table is the
intent; not every component writes there yet. As of #1716–#1721:

- **Wired going-forward:** `crons.json` (read+write, `schedule-crons/SKILL.md`),
  `pending-questions.md` (read via `personal_path`, #1718).
- **Migrator one-time copy only:** `PERSONAL_CLAUDE.md`, `stand-identity.json`,
  `tab-aliases.json`, channel `access.json`, `settings.json`, and `build_log.md`'s
  loop-writer. `--migrate-from-legacy` (#1721) copies these into
  `hosts/<hostname>/` **once**, but the owning components do **not yet write
  there going forward** — that per-component write-wiring lands as separate
  single-concern PRs. Until then, edits to these files revert to their old paths
  and aren't backed up under the `hosts/*/` carrier.
- **Reader caveat:** `PERSONAL_CLAUDE.md` is currently read from the **workspace
  root** (per `CLAUDE.md`), not via `personal_path` — so relocating it to
  `hosts/<hostname>/` also requires updating the reader, else the move isn't seen.
- **Fresh-adopter caveat:** the migrator hard-requires a legacy
  `~/.sutando/memory-sync` clone and exits if absent — so a host set up *fresh*
  from this branch (no legacy clone) gets no per-host config established by it.
  Establishing per-host config without a legacy clone is a separate follow-on.

## What does NOT live there

- **Secrets / tokens** — `.env`, `*.env`, keychain material. Hard-denied
  (`.env*`) regardless of carrier. They never sync, here or anywhere.
- **Shared, mergeable data** — core memory (`projects/*/memory/`), `notes/`.
  These are *meant* to merge across hosts; they keep their shared paths.
  (`pending-questions.md` and `build_log.md` are **per-host**, not shared — see
  the table above — per the F1 decision.)
- **Transient runtime state** — `*.alive`, `*.sentinel`, `*.pid`. Hard-denied.
- **Per-host identity that must NOT propagate at all** — `state/auth/`
  (`device.json`, `cloud-auth.json`). These stay excluded (a device's identity
  is meaningless on another host). `hosts/<hostname>/` is for config you'd want
  to *restore on the same host after a rebuild*, not identity you'd clone.

## Conflict model

Because each host writes only its own subtree, `hosts/<hostname>/` files have a
**single writer** → no 3-way merge, no conflict markers, ever. This is the
direct fix for the revamp's conflict regression (git markers landing in
files). Shared files (memory/notes) keep their existing merge strategy.

## Stale-host surfacing

`health-check.py` should read `hosts/*/` mtimes (or a `hosts/<hostname>/.last-sync`
marker) and flag any host subtree not updated in N days — so a host that stopped
syncing is visible rather than silently stale (a gap in both the old and new
models today).

## Migration (follow-on)

1. **From main's `machine-<hostname>/`:** one-time copy
   `machine-<host>/* → hosts/<host>/*` for this host at cutover (parameterized
   script; the `machine-<host>/` source path is environment-specific — owned
   alongside the `sync-memory.sh` retirement).
2. **Mini's #1715** (channel access.json under `machine-<host>/`) is the
   old-model stopgap for the same data. Per Chi's call: merge it as a bridge,
   then this convention subsumes it (channel access → `hosts/<hostname>/channels/`),
   OR close it and fold straight in here. Either way #1715 and this convention
   target the same per-host data — they must not both own it long-term.
3. **Per-component wiring** (each component writes its config under
   `hosts/<hostname>/`) lands as separate single-concern PRs, one per file class,
   so each is testable in isolation.

## `$CLAUDE_CONFIG_DIR` breakdown

`$CLAUDE_CONFIG_DIR` resolves to `<workspace>/.claude-sutando/` — **inside the
workspace**, so the same sync machinery reaches it. Sync granularity is
**per-path via the `vault.sync.include` whitelist**, not all-or-nothing, so the
three classes coexist under it:

| Path under `$CLAUDE_CONFIG_DIR` | Class | How it syncs |
| --- | --- | --- |
| `projects/*/memory/` | **shared** | whitelisted, synced **in place**, merges across hosts ✅ today |
| `channels/<svc>/access.json`, `settings.json` | **per-host** | per-host by **omission** today (not whitelisted) → kept local, **no backup**. Fix: back up to `hosts/<hostname>/channels/.../access.json` |
| `channels/<svc>/.env`, tokens | **secret** | hard-denied (`.env*`) — never synced, anywhere |

**The fixed-path constraint (why per-host config is backup-copy, not move):**
per-host files under `$CLAUDE_CONFIG_DIR` are read by running code from a **fixed
path** (`$CLAUDE_CONFIG_DIR/channels/discord/access.json`). They cannot sync
*in place* — every host's copy would collide on the pull-merge. So per-host
config is **mirrored** into `hosts/<hostname>/…` (carried by `hosts/*/`,
collision-free), the **live file stays** at its fixed `$CLAUDE_CONFIG_DIR` path,
and a fresh host **restores from the backup**. Shared files (`memory/`) sync in
place as before. This is exactly what the per-component relocation PRs (Migration
step 3) implement.

## Enforcement

- `vault.sync.include` carries `hosts/*/` (this PR).
- `scripts/lint-vault-sync-paths.sh` (#1716) already blesses the `*` glob and
  fails bare per-host paths — so a future config that tries to carry a per-host
  file *outside* `hosts/<hostname>/` (or another hostname-qualified path) fails CI.
