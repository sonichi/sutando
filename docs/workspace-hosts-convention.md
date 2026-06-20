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
| crons | `crons/<hostname>.json` (#1716) | already hostname-qualified; may fold to hosts/<hostname>/crons.json later — both pass the lint |

## What does NOT live there

- **Secrets / tokens** — `.env`, `*.env`, keychain material. Hard-denied
  (`.env*`) regardless of carrier. They never sync, here or anywhere.
- **Shared, mergeable data** — core memory (`projects/*/memory/`), `notes/`,
  `pending-questions.md`. These are *meant* to merge across hosts; they keep
  their shared paths.
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

## Enforcement

- `vault.sync.include` carries `hosts/*/` (this PR).
- `scripts/lint-vault-sync-paths.sh` (#1716) already blesses the `*` glob and
  fails bare per-host paths — so a future config that tries to carry a per-host
  file *outside* `hosts/<hostname>/` (or another hostname-qualified path) fails CI.
