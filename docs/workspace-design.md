# Workspace design — the 2-space model

**Status:** Ratified, in effect (operational since PR #1440 merge 2026-06-04; refined 2026-06-04 per owner directive replacing the prior 3-space framing).

**Operational contract:** see [`docs/workspace-contract.md`](workspace-contract.md). **Runtime config:** see [`docs/workspace-config.md`](workspace-config.md). **Sync across machines:** see [`docs/workspace-sync.md`](workspace-sync.md). This doc explains the mental model — what fundamental categories of state exist in a Sutando install, and where they belong.

## Why this doc exists

Through 2026-05-18 a workspace-vs-repo bug class surfaced ~28 sites across Python, TypeScript, and Swift where writers and readers disagreed about whether a path resolved against the git checkout or against per-user runtime state. 16 PRs closed the divergence; the workspace contract (M0 → v0.3.0) ratified the boundary. The mental model that drives the contract is the 2-space model below.

> **Historical note** (2026-06-04): The previous version of this doc described a **3-space model** (Code / State / Memory), with Memory subdivided into Shared vs Per-machine. Per owner directive 2026-06-04 03:11Z (after PR #1444 closure): **Memory is no longer a top-level space — it's a sub-path under `<workspace>/.claude-sutando/projects/<slug>/memory/` that gets synced alongside notes/, build_log.md, etc.** Sync is a property of sub-paths within Workspace, not a distinguishing top-level container. The doc has been rewritten around the 2-space model accordingly.

## The 2 spaces

| Space | Purpose | Lives at | Sync model | Lifecycle |
|---|---|---|---|---|
| **Code** | Source of truth for behavior | The git checkout (resolved per-language via the standard resolvers) | Public git (`sonichi/sutando`) | Persistent; updated by `git pull` |
| **Workspace** | All per-user runtime + content | `<repo>/workspace/` by default; configurable via `sutando.config.local.json` (per-clone gitignored) | Private git per fleet (`vault.remote_url` via `sync-workspace.sh`), with `vault.sync.include`/`exclude` controlling what flows | Workspace tree IS the unit; sub-paths have per-flow lifecycle (tasks/results are ephemeral, memory/notes are persistent) |

That's it. Two top-level spaces. Sync is configured per-sub-path inside Workspace, not by having a separate "synced" container.

## Quick decision: which space?

When adding code that reads or writes a file, ask: **does it ship with every install (same on every clone)?**

- **Yes** → **Code**. Examples: `.py` script, `.yaml` config in `skills/`, markdown doc, anything that should be identical across machines.
- **No** → **Workspace**. Then within Workspace, decide whether it should sync via `vault.sync.*` (memory, notes, durable user content) or stay per-host (state/cores/, .env, conversation.sqlite, runtime tasks/results).

If you can't place a path on this flowchart in 5 seconds, the path probably wants splitting — e.g. a config file with both shared defaults + per-machine overrides should split into a tracked `sutando.config.json` (Code) and an untracked `sutando.config.local.json` (Workspace, per-host).

## Resolvers

### Code

The git repo. Source of truth for behavior. Owned by version control.

**Resolvers:**
- Python: `Path(__file__).resolve().parent.parent` for src-adjacent paths
- TypeScript: `dirname(dirname(fileURLToPath(import.meta.url)))` (matches `src/web-client.ts` post-#821)
- Swift: `AppDelegate.repoRoot` (post-#837)

**Lives at:** wherever the user cloned. `~/Desktop/sutando`, `~/Documents/github/sutando`, etc.

**Sync model:** standard public git. `git pull` keeps in sync.

**What goes here:** `src/`, `skills/`, `scripts/`, `tests/`, `docs/`, `CLAUDE.md`, `README.md`, `package.json`, `.env.example`. Anything that should be the same across every install.

**What does NOT go here:** anything per-user. `tasks/`, `results/`, `state/`, `logs/`, `.env`, `logs/conversation.log`, the actual `notes/` and `build_log.md`, the agent's memory — none of these are code.

### Workspace

Per-user runtime + content. Lives at `<repo>/workspace/` by default (post-M0). Synced across the fleet via `sync-workspace.sh` for the sub-paths the user opts in to.

**Resolvers:**
- Python: `from workspace_default import resolve_workspace` (post-M0)
- TypeScript: `import { resolveWorkspace } from './workspace_default.js'`
- Swift: `AppDelegate.workspace` (post-#837)
- Shell: `bash scripts/sutando-config.sh workspace`

**Lives at:** `<repo>/workspace/` by default. Configured via `sutando.config.local.json` → `workspace.path`. The env var `$SUTANDO_WORKSPACE` is **no longer honored** as of #1440 (deprecation warning + auto-migrate at startup if set).

**Sync model:** `sync-workspace.sh` (workspace IS the git working tree, per-host branch `host/<host>/<wsId>`). What flows controlled by `vault.sync.include`/`vault.sync.exclude` in config.

**What goes here, by sub-path purpose:**

| Sub-path | Synced? | Lifecycle | Examples |
|---|---|---|---|
| `notes/` | ✅ yes (default) | Persistent, user-authored | Long-form notes, research, daily logs |
| `.claude-sutando/projects/<slug>/memory/` | ✅ yes (default) | Persistent, agent-authored | Auto-memory files written by `/remember` etc. |
| `build_log.md` | ✅ yes (default) | Append-only | Session changelogs from proactive-loop |
| `pending-questions.md` | ✅ yes (default) | Persistent | User-deferred questions |
| `tasks/`, `results/` | ❌ no | Ephemeral | Per-tick task queue + replies (per-host runtime) |
| `state/cores/<host>.alive` | ❌ no | Per-host heartbeat | Per-host structural state, not synced |
| `state/auth/`, `state/cloud-auth.json`, `state/device.json` | ❌ no | Per-host durable | Install/identity state, per-host |
| `logs/conversation.log` | ❌ no | Per-host runtime | Voice/phone/discord log; large + per-host |
| `data/conversation.sqlite` | ❌ no | Per-host runtime | SQLite mirror of conversation log |
| `.env` | ❌ no | Per-host secret | Tokens, API keys — must NOT sync |

The sync default is "synced unless excluded"; per-host runtime sub-paths are excluded via the carrier-set gitignore (per PR #1447) + `vault.sync.exclude` user overrides.

## Decisions (preserved as historical record)

The earlier version of this doc (pre-2026-06-04) framed the model as **3 spaces** with Memory as a top-level peer of Code + State. The shift to **2 spaces** happened when owner observed that the "Memory vs State" distinction is really a sync-policy distinction *within* Workspace, not a separate-container distinction:

- The agent's memory files live at `<workspace>/.claude-sutando/projects/.../memory/`, structurally inside the workspace tree.
- The user's notes live at `<workspace>/notes/`, also inside.
- Per-host state lives at `<workspace>/state/`, also inside.

What distinguishes them is not WHERE they live but WHETHER they flow through `sync-workspace.sh`. That's a property of sub-paths, configurable via `vault.sync.*`.

The 3-space model conflated **container identity** with **sync policy**. The 2-space model separates them: containers are Code vs Workspace; sync is per-sub-path.

The original 3-space rationale + the bug-class history that motivated it are preserved in the git history of this file (pre-PR #1472 docs sweep).
