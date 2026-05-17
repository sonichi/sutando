# Workspace Contract

Every file Sutando reads or writes lives in one of two locations. Knowing which is which prevents an entire class of split-brain bugs.

## The rule

**`REPO_DIR` is source-tree-only. All user / runtime state goes through `WORKSPACE_DIR`. Default to `WORKSPACE_DIR` for any new path; reach for `REPO_DIR` only when you're reading code or git-tree metadata.**

This is the strong form of the split. If you find yourself typing `REPO_DIR / "<filename>"` for any file that isn't checked into git and read by code, that's almost certainly the bug pattern this contract prevents.

## What goes where

| Concept | Path on this machine | What lives here | Use it when |
|---|---|---|---|
| **Source tree (`REPO_DIR`)** | wherever you cloned the repo (e.g. `~/Documents/github/sutando`) | `src/`, `skills/`, `scripts/`, `docs/`, `tests/`, `CLAUDE.md`, `package.json` — anything that's in git and is part of the codebase. | Exec'ing a source script. Running `git -C` against the tree. Reading a checked-in file (docs, sample config, source). |
| **Workspace (`WORKSPACE_DIR`)** | `$SUTANDO_WORKSPACE` (if set) or `~/.sutando/workspace/` (canonical default) | Per-user mutable runtime state: `tasks/`, `results/`, `state/`, `data/`, `logs/`, `notes/`, `build_log.md`, `pending-questions.md`, `contextual-chips.json`, `core-status.json`, `voice-state.json`, `quota-state.json`, anything else the running agent generates or accumulates. | Always, unless one of the three "use REPO_DIR" cases above applies. |

The two paths **can** be the same (default for fresh installs without `SUTANDO_WORKSPACE`), but designing for them as separate is the right structural shape — multiple Sutando nodes on one machine, separate-from-code workspace, OSS readers running the engine without polluting their own git tree.

## Code review test

For every `REPO_DIR / "..."` (or equivalent `Path(__file__).parent.parent / "..."`) in a PR diff, the reviewer should ask: *"Is this path a source-code file, a script being exec'd, or a `git -C` cwd?"* If the answer is no, the path belongs under `WORKSPACE_DIR`.

## Resolution rules — Python

```python
# Source tree — for reading source files, exec'ing scripts, git cwd
REPO_DIR = Path(__file__).resolve().parent.parent

# Runtime state — for tasks/, results/, state/, data/, logs/, notes/,
# build_log.md, pending-questions.md, core-status.json, etc.
from workspace_default import resolve_workspace
WORKSPACE_DIR = resolve_workspace()
```

For files that live under a *user-shared* private dir (the memory-sync repo), use the helpers in `src/util_paths.py`:

```python
from util_paths import personal_path, shared_personal_path

# Per-machine personal asset (stand-identity.json, stand-avatar.png)
si = personal_path("stand-identity.json")

# Fleet-shared file (notes/, build_log if you want it shared)
notes = shared_personal_path("notes")
```

These honor `SUTANDO_WORKSPACE` by default — don't pass `REPO_DIR` explicitly.

## Resolution rules — TypeScript

```typescript
// Runtime state — match the Python helper's contract
const WORKSPACE_DIR =
  process.env.SUTANDO_WORKSPACE ||
  join(homedir(), '.sutando', 'workspace');

// Source tree
const REPO_DIR = new URL('..', import.meta.url).pathname;  // adjust depth
```

## Resolution rules — Shell

```bash
# Workspace state (tasks/, results/, etc.)
TASKS_DIR="${SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}/tasks"

# Source tree — derive from script location, not from $SUTANDO_WORKSPACE
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_PARENT="$(cd "$SCRIPT_DIR/.." && pwd)"
```

## Common mistakes (the bug class this contract prevents)

1. **`Path(__file__).parent.parent / "<runtime-file>"`.** This gives the source tree; on an env-pinned host it diverges from where bridges read. Use `WORKSPACE_DIR` for runtime state. The strong rule: if you're typing `REPO_DIR / "..."` for anything that isn't source code, you're probably in this bug class.
2. **`$SUTANDO_WORKSPACE` as input to a Claude-project-key sed-map.** Claude's project memory dir keys on the launch CWD, not on the workspace. Use `SCRIPT_PARENT` for that lookup. (Bug fixed in `scripts/sync-memory.sh` by this PR; was silently skipping 8+ memory files for 5+ weeks.)
3. **Hard-coding a startup gate around `NGROK_AUTHTOKEN` when a `TWILIO_WEBHOOK_URL` tunnel is the alternative.** Different shape but same family — code assumed one path when the design supports two. (Fixed for `conversation-server.ts` by this PR.)
4. **Passing `REPO_DIR` explicitly to `personal_path()` / `shared_personal_path()`.** The helpers default to the right thing now; the explicit arg forces source-tree resolution. Drop the arg unless you genuinely need a non-workspace path. (Cleaned up in `dashboard.py` by this PR.)

## How to decide for new code

Walk the question top-to-bottom and stop at the first match:

1. **Is the file checked into git?** → `REPO_DIR`. (Source code, scripts, docs, tests, package.json.)
2. **Is the file a stable cross-machine reference Claude can read on session start?** (e.g., `CLAUDE.md`, README) → `REPO_DIR`.
3. **Otherwise — does it mutate during runtime?** → `WORKSPACE_DIR`. (Tasks, results, state, logs, notes, build_log, pending-questions, status JSON.)
4. **Is it per-machine personal state?** (Stand identity, avatar) → `personal_path()` (honors `SUTANDO_PRIVATE_DIR` first).
5. **Is it fleet-shared user state?** (Notes, shared memory) → `shared_personal_path()` (honors `SUTANDO_PRIVATE_DIR` first, falls back to workspace).

## Related PRs

- **#762** — established the workspace default (`~/.sutando/workspace/`) and migration from legacy repo-root fallback.
- **#775** — applied the two-variable split (`REPO_DIR` + `WORKSPACE_DIR`) to `agent-api.py`, `github-webhook.py`, `task-bridge.ts`.
- **This PR** — applies the same split to `health-check.py`, `conversation-server.ts`, `sync-memory.sh`, `util_paths.py`, `dashboard.py`. Documents the contract.

Any code in the OSS tree that still uses the old pattern is a fix candidate — file an issue or open a PR following the same shape.
