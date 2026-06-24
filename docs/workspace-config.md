# Workspace configuration

Sutando keeps **per-user runtime state** (tasks, results, notes, state, logs, etc.) under `<repo>/workspace/`, separate from the tracked code in the rest of the repo. This doc covers how the workspace path is resolved and how to override it.

## The default

```
<repo>/workspace/
```

That's it for a fresh clone — no setup, no env var, no config file. The directory is gitignored except for `.gitkeep`, so user data never sneaks into commits.

## Resolution order (highest wins)

1. **`sutando.config.local.json`** → `workspace.path` — per-clone override. Gitignored.
2. **`sutando.config.json`** → `workspace.path` — tracked default. The repo ships `${REPO_DIR}/workspace`.
3. **Baked-in fallback** — `${REPO_DIR}/workspace`, used if neither config file exists.

**Note:** `$SUTANDO_WORKSPACE` is **no longer honored** by the resolver as of PR #1440 (workspace contract v0.3.0). If set, startup emits a one-time stderr deprecation warning + invokes auto-migration via `src/startup.sh`. Migrate to `sutando.config.local.json` to silence the warning.

`${REPO_DIR}` in any config string expands to the directory containing the config file (== git toplevel for a sane checkout).

## The two config files

**`sutando.config.json`** — tracked, shared across all clones. Defines the contract + defaults:

```json
{
  "workspace": {
    "path": "${REPO_DIR}/workspace"
  },
  "vault": { ... }
}
```

**`sutando.config.local.json`** — gitignored, per-clone overrides. Optional — empty file or missing file both mean "use defaults":

```json
{
  "workspace": {
    "path": "/Volumes/MyExternalSSD/sutando-workspace"
  }
}
```

A sample is shipped as `sutando.config.local.json.example`. Copy + edit, or start from scratch — the loader tolerates any subset of fields.

Keys whose name starts with `_` (e.g. `_comment`) are stripped before validation, so the `.example` file can carry inline documentation without affecting runtime.

## Three common overrides

```json
// 1. Move workspace outside the repo (e.g. shared between clones)
{ "workspace": { "path": "/Users/you/.sutando/workspace" } }

// 2. Enable vault sync to a private remote
{ "vault": { "enabled": true, "remote_url": "https://vault.example.com/you/workspace.git" } }

// 3. Both
{
  "workspace": { "path": "/Users/you/.sutando/workspace" },
  "vault": { "enabled": true, "remote_url": "https://vault.example.com/you/workspace.git" }
}
```

## Use the loader, never reinvent the fallback

| Language | API |
|---|---|
| Python | `from sutando_config import resolve_workspace, resolve_vault, load_config` |
| TypeScript | `import { resolveWorkspace, resolveVault, loadConfig } from './sutando_config.js'` |
| Swift | `SutandoConfig.resolveWorkspace()` / `SutandoConfig.loadConfig()` |
| Bash | `WORKSPACE="$(bash scripts/sutando-config.sh workspace)"` |

`src/workspace_default.{py,ts}` (the legacy resolver) now delegates to the loader transparently — existing callers don't need code changes.

## Protection layers

The workspace must never end up in commits. Three layers enforce this:

1. **`.gitignore`** — `workspace/*` with a `!workspace/.gitkeep` exception. Prevents `git add .` from picking up runtime files.
2. **Local pre-commit hook** — `.githooks/pre-commit` refuses any commit whose staged files include `workspace/`-prefixed paths (except `.gitkeep`). Auto-installed by `src/startup.sh`; one-time manual install via `bash scripts/install-git-hooks.sh`.
3. **CI workspace-leak check** — `.github/workflows/workspace-leak-check.yml` mirrors the hook on every PR + push to main. Catches anyone who bypassed the local hook.

Escape hatch for the rare legitimate case (updating `.gitkeep` itself): `git commit --no-verify`.

## CI lint: forbid new direct resolution

`.github/workflows/lint-workspace-resolution.yml` refuses PRs that introduce new code outside the loader reading `$SUTANDO_WORKSPACE` directly, hardcoding `~/.sutando/workspace`, or using the historic `Path(__file__).resolve().parent.parent` walk-up. Existing legacy offenders are not flagged (the CI uses `--diff` mode); they're migrated separately.

## Migrating from the old default

Older installs used `~/.sutando/workspace/` as the default. If you have one:

- **Path-only override:** add `{"workspace":{"path":"/Users/you/.sutando/workspace"}}` to `sutando.config.local.json`. Done.
- **Move into the in-repo default:** copy your old workspace contents into `<repo>/workspace/`. The loader will emit a `.env` drift warning if your `.env` still declares `SUTANDO_WORKSPACE=` — remove that line once you've migrated.

The M1 milestone will ship a dedicated recovery skill (`bash scripts/sutando-migrate.sh`) for users who want a guided audit + move.

## Related

- `CLAUDE.md` § Workspace contract — the project-wide contract this doc operationalizes
- `docs/sutando-config.schema.json` — JSON Schema for the config file (editor autocomplete + validation)
- `sutando.config.local.json.example` — annotated override sample at the repo root
