# Per-host carried paths must be hostname-qualified

**Rule:** any file synced through the workspace vault (`vault.sync.include`) that
holds **per-host** data MUST live at a **hostname-qualified path** —
`<dir>/<hostname>.json`, `build_log/<hostname>.md`, etc. A bare, non-qualified
path that carries per-host data will silently cross-contaminate hosts.

## Why

`scripts/sync-workspace.sh` uses a **branch-per-host** topology: each host
pushes only to its own branch `host/<hostname>/<wsId>`, and pulls peers via
`fetch` + **3-way merge** into its local branch.

That merge isolates by branch on **push**, but on **pull** it re-collides
**same-path** files: if host A and host B both have `crons.json` at the same
path with different content, the pull-merge tries to merge the two — conflict or
blended content. So branch-per-host does **not**, by itself, keep per-host data
separate. The separation only holds when the path differs per host:

- **distinct by construction** — `build_log/<hostname>.md` (each host writes its
  own file; peers' files coexist after pull, never merge), or
- **distinct by accident** — `.claude-sutando/projects/<local_slug>/memory/`,
  where `<local_slug>` is the repo path with `/`→`-`. Safe only while hosts use
  *different* repo paths; if two hosts share a path the slug "collapses" and the
  memory dirs merge. (That's luck, not design.)

The sharpest failure is a path with **no per-host component at all** —
`channels/<svc>/access.json` (allowlists / TOFU / tier-maps), `state/auth/device.json`
and `cloud-auth.json` (per-host identity/credentials). If any of these were ever
added to the carrier set, *every* host's copy would collide at the identical
path on the first pull — blending allowlists or device identities across
machines. Today they are safe **only** because the carrier whitelist does not
include them.

## The two safe options for per-host data

1. **Don't carry it.** Leave it out of `vault.sync.include` (whitelist mode
   ignores everything not listed). This is how channel configs stay per-host
   today.
2. **Hostname-qualify it.** If it should be synced/backed-up per host, give it a
   path that expands to one file per host. The canonical home is the
   `hosts/<hostname>/` per-host subtree (see
   [`workspace-hosts-convention.md`](workspace-hosts-convention.md)) — e.g.
   `hosts/<hostname>/crons.json`, carried by the `hosts/*/` glob — but any
   qualified pattern works (`build_log/<hostname>.md`). Each host owns its file;
   pulls bring peers' files in side-by-side, never merging.

Use the host slug the sync layer uses: `hostname | sed 's/\..*//'`.

## Enforcement

`scripts/lint-vault-sync-paths.sh` fails CI if `vault.sync.include` carries a
known per-host-prone path (`channels/`, `state/auth`, `device.json`,
`cloud-auth.json`, `state/cores`) without a hostname qualifier (an explicit
`<hostname>`/`$(hostname)` token or a `*` glob segment). This stops a future
carrier addition from silently reintroducing the collapse.

## Examples

| Path in `vault.sync.include` | Verdict |
| --- | --- |
| `hosts/*/` (carries `hosts/<hostname>/crons.json`) | ✅ glob expands per host |
| `build_log/<hostname>.md` | ✅ hostname-qualified |
| `.claude-sutando/projects/*/memory/` | ✅ glob expands per slug |
| `crons.json` | ❌ bare per-host file → collapses on pull-merge |
| `channels/discord/access.json` | ❌ per-host, zero qualifier |
| `state/auth/` | ❌ per-host identity, must not propagate |
