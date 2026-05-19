# Host CLI Bindings

Sutando is built on a host CLI tool (today: Claude Code). A handful of agent-state surfaces — agent memory, bridge auth tokens, skill discovery, slash-command config — live inside the host CLI's user-home directory rather than under `$SUTANDO_WORKSPACE` or `$SUTANDO_MEMORY_DIR`. These surfaces are not Sutando-owned; we read and write them via the host CLI's conventions, the way a Node app uses `~/.npm/` or a Python tool uses `~/.config/`.

This doc names the surfaces, what binds each, what re-binding costs if Sutando ever moves to a different host CLI (Codex, gemini-cli, a future tool), and the canonical helper to call when new code needs to touch the host CLI's home.

It does **not** list specific paths in the current host CLI's home directory. Those are implementation detail — they bit-rot when the host CLI's conventions change. Contributors who need to make a code change that touches a specific path should ask the maintainer for the workspace-private engineering note.

## The surfaces

| Surface | What Sutando does there | What binds it | Re-bind cost on a host-CLI swap |
|---|---|---|---|
| **Agent memory** | Reads on session start (auto-loaded as context); writes on memory updates (`/remember`, auto-save). Persists across sessions. | The host CLI's session-startup contract: it loads memory files from a known per-user directory and surfaces their content into the model's context window. Sutando relies on this loading behavior, not just on filesystem layout. | **High.** A new host CLI would need an equivalent session-startup-loads-memory contract, or Sutando would have to re-implement memory injection on its own. |
| **Skill discovery** | Reads on session start (manifest + tools.ts dynamic-import); the host CLI also exposes skills as user-facing slash commands. | The host CLI's skill loader: it scans a known per-user directory, surfaces skills as slash commands, and gates them by skill metadata. Sutando rides on this for both runtime tool tables and user-typed `/skill-name` invocations. | **High.** A new host CLI would need an equivalent skill-loader contract — both the discovery surface AND the slash-command UI binding. |
| **Bridge tokens + access state** | Reads + writes for owner allowlists, OAuth tokens, channel-access JSON. | The host CLI doesn't actually need these — Sutando picked the host CLI's per-user directory as a stable, gitignored home for cross-bridge config. It's the closest thing to "user-config-dir" on macOS without bringing in another dependency. | **Low.** Pure data movement: define a new per-user directory (or `~/.config/sutando/`) and migrate. No protocol changes. |
| **Slash commands** | The host CLI registers slash commands the user can invoke (`/morning-briefing`, `/proactive-loop`, `/help`). Some are Sutando-defined; some are host-CLI built-ins. | The host CLI's command dispatcher + the slash-command file convention. Sutando-defined commands live as files in the skills/host-CLI's command dir. | **Interop loss.** The host CLI's built-in commands (`/help`, `/clear`, etc.) disappear on a swap. Sutando-defined commands would need re-registration in the new host's convention. Not a re-bind so much as a partial re-implementation. |

## The greppable surface

Any code that needs to read or write inside the host CLI's home directory should go through one helper:

- **Python:** `claude_home_path(*subpath)` in `src/util_paths.py`.
- **TypeScript:** `claudeHomePath(...subpath)` exported from `src/util_paths.ts`.

Both honor `$CLAUDE_HOME` for tests + alt-host installs, else default to the current host CLI's known per-user home.

The point of the helper is *grep-counting*: one canonical resolver means a future host-CLI swap is a 1-day grep+replace rather than a re-architecture. Don't reach for `~/.claude/`-relative paths directly — always go through `claude_home_path()` / `claudeHomePath()` so the dependency surface stays countable.

If you're adding code that touches a new surface (not currently in the table above), update this doc with the new surface + its re-bind cost. The taxonomy is the value here, not the row count.

## Today's policy on specific paths

Specific path strings (e.g. `<host-CLI-home>/skills/`, `<host-CLI-home>/projects/<slug>/memory/MEMORY.md`) live in the workspace-private engineering note, not in this public doc. The rationale: those paths are implementation detail of the current host CLI's conventions; publishing them invites bit-rot when the host CLI's directory layout changes between versions, and a public doc that goes stale is worse than no public doc.

The taxonomy in this doc + the `claudeHomePath()` helper give contributors enough surface to:

1. Recognize when a new piece of code is touching the host-CLI dependency surface (it'd be calling `claudeHomePath()`).
2. Estimate the portability cost of that touch (high / low / interop loss, per the table).

Contributors who need the actual paths to make a change should ask the maintainer for the workspace-private engineering note. The helper is the bridge: as long as your code calls `claudeHomePath()`, the implementation can move without breaking your call site.

## Relationship to other docs

- [`docs/workspace-design.md`](workspace-design.md) — 3-space mental model (Code / State / Memory) RFC. This doc is the longer form of that RFC's "Host CLI dependency surface" section.
- [`docs/workspace-contract.md`](workspace-contract.md) — implementation reference for the 3-space split. Tells you how to use `REPO_DIR` vs `WORKSPACE_DIR` vs `personal_path()`. The host-CLI surface is *outside* the 3-space split — it's a fourth zone owned by the host CLI, not Sutando.

## Why this matters

Sutando picked a host CLI on purpose: a third-party tool that already handles auth, model swapping, MCP servers, slash commands, and session lifecycle is cheaper to ride on than to rebuild. The trade is the dependency surface this doc inventories.

Keeping the surface small and countable is the only thing standing between "swap host CLIs in a sprint" and "swap host CLIs in a quarter." The greppable helper is the structural insurance; this taxonomy is the inventory you'd reach for when planning the swap.
