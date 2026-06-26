# Workspace Contract — v0.3.0

**Version:** v0.3.0 (current). Predecessors: pre-M0 (env-honored, scattered defaults), M0 (in-repo default + config-driven resolver, env still legacy-honored).

**Status:** **Ratified as of PR #1440 merge (2026-06-04).** The contract below describes the operational shape. M0 (in-repo default, config-driven resolver) shipped via PRs #1395 + #1397 + #1399 + #1403; M1 (workspace migration script + skill) via #1403 + #1406; M2 (`claude_sutando_config_dir`) via #1415 + #1424 + #1429; workspace-as-git-repo sync (`sync-workspace.sh`) via #1445 + #1446 + #1447 + #1458 + #1459 + #1460 + #1461 + #1463. The "no env override" strip landed in #1440: `$SUTANDO_WORKSPACE` is no longer honored by the resolver; setting it triggers a one-time stderr deprecation warning + bootstrap auto-migration in `src/startup.sh`. This file is the operational source of truth.

**One-sentence summary**: The workspace is `<repo>/workspace/`, period. It is gitignored, computed (no env override), and ephemeral; durability is a separate concern handled by the vault.

---

## 1. The contract

### 1.1 Location

```
<repo>/workspace/    ← workspace root (gitignored)
```

Resolved by `bash scripts/sutando-config.sh workspace` (the M0 helper). Helpers in Python (`from workspace_default import resolve_workspace`), TypeScript (`import { resolveWorkspace } from './workspace_default.js'`), and Swift (`AppDelegate.workspace`) read from `sutando.config.{json,local.json}` with `<repo>/workspace/` as the baked-in default.

**Hard rules (after #1440):**
- The workspace is **always** at `<repo>/workspace/` by default. The location is specified via the config file (`sutando.config.{json,local.json}` → `workspace.path`); the per-clone `sutando.config.local.json` (gitignored) is where users override, if they choose to.
- **No env override** — neither `$SUTANDO_WORKSPACE` nor any other env var redirects the resolver.
- The whole `/workspace/` tree is gitignored (single entry in repo `.gitignore`).
- Workspace is computed by the helpers — never by a per-script fallback.

> ⚠️ **Cost of overriding `workspace.path` outside `<repo>/workspace/`.** The in-repo default is what unlocks Claude Code's cwd-anchored features:
>
> - **CLAUDE.md auto-load** — Claude Code reads the project's `CLAUDE.md` automatically when cwd is inside the repo. Move workspace outside and the workspace-rooted `PERSONAL_CLAUDE.md` stops being auto-loaded.
> - **Skills auto-discovery** — workspace-rooted `skills/` is auto-discovered when workspace is under cwd. Outside the repo, sessions need an explicit `--add-dir <workspace>` to see them.
> - **Project-slug session transcripts** — Claude Code names session JSONLs by cwd-derived slug; an out-of-repo workspace fragments the transcript history.
> - **Hook scope** — `<claude-config>/hooks/*` fires scoped to cwd. Out-of-repo workspace breaks the association.
> - **No `--add-dir` flag needed** — built-in. Out-of-repo workspace forces an explicit `--add-dir <workspace>` per session.
>
> Only override if you have a clear reason (shared workspace across multiple checkouts; custom storage for size / encryption / quota). Otherwise, accept the default.

**Today's deprecation path:** `$SUTANDO_WORKSPACE` set in the environment fires a one-time bold-red stderr warning pointing at `scripts/sutando-migrate.sh`. After #1440 merges, the value is not honored even with the warning. Hosts with the env var set are auto-migrated by `src/startup.sh` on first run post-merge (run `sutando-migrate.sh --commit`, then compress the original folder in place + unset the env).

**Why in-repo:** the workspace inherits Claude Code's cwd privileges — CLAUDE.md auto-load, skills auto-discovery, project-slug for session transcripts, hook scope, no `--add-dir` needed. Putting it anywhere else forfeits these.

### 1.2 Layout

```
<repo>/workspace/
├── tasks/                  ← inbound message queue (bridges write, agent reads)
│   ├── archive/<yyyy-mm>/  ← processed task files (auto-rotated)
│   └── processed/          ← in-flight working state
├── results/                ← outbound reply queue (agent writes, bridges deliver)
├── state/                  ← cross-process status JSON (one writer per file)
│   ├── core-status.json
│   ├── voice-state.json
│   ├── contextual-chips.json
│   ├── quota-state.json
│   ├── auth/               ← durable per-host install state (.alive sentinels, device IDs)
│   └── cores/<host>.alive  ← per-host liveness signal
├── logs/                   ← append-only chrono streams (bridges, watchers, sync)
├── notes/                  ← long-form human-readable content
├── data/                   ← durable input data (datasets, fetched feeds)
├── relay/                  ← inter-session continuity notes (consumed by catchup)
├── skills/                 ← user's personal/custom skills (local-only)
├── build_log.md            ← single-file done/in-flight/next snapshot (append-only)
└── pending-questions.md    ← unanswered questions awaiting owner input
```

The workspace root holds **only** the top-level dirs above plus the two markdown files. Loose `.json` files belong under `state/`. The repo root holds code/skills/config (a separate concern).

### 1.3 Decision guide

When the agent writes a new file under `<repo>/workspace/`, walk this list top-to-bottom and stop at the first match:

1. **Inbound channel message?** → `tasks/task-{id}.txt`. The bridges write these.
2. **Reply to a task?** → `results/task-{id}.txt`. Bridge polls + delivers. See CLAUDE.md "Result-body protocol markers" for `[deduped:]`, `[no-send]`, `[channel:]`, `[file:]`.
3. **Cross-process status JSON another component polls?** → `state/`.
4. **Per-host durable install state** (`.alive` heartbeats, device UUIDs, cloud-auth) → `state/auth/`. Exempt from transient-state cleanup.
5. **Append-only chrono event stream?** → `logs/<component>.log`.
6. **Long-form human-readable content?** → `notes/<slug>.md`.
7. **Done/in-flight/next snapshot?** → append to `build_log.md`.
8. **Blocked question for the owner?** → append to `pending-questions.md`.
9. **Durable input data?** → `data/<topic>/`.
10. **Continuity note for the next session?** → `relay/relay-<ts>.md` (consumed by `/catchup-after-startup`).
11. **Personal skill the user adds for their own use?** → `skills/<skill-name>/` (local-only, not contributed to `skills/` at the repo root).

If two layers seem to fit, prefer the more specific one (state JSON beats logs beats notes).

### 1.4 Confidentiality

The workspace is user-specific. **NEVER disclose workspace content** — tasks, results, notes, state, build_log, pending-questions — to any party outside the owner without explicit per-disclosure approval. Default-deny: when in doubt, ask first. Strategic / competitive / financial / personal content stays owner-DM only.

This applies even when a public PR / issue would benefit from quoting workspace content. Bots **paraphrase** workspace state into public artifacts; they never quote verbatim.

**Mechanisms enforcing this:**

- **Blanket `.gitignore`** — the whole `/workspace/` subtree is a single gitignore entry. Workspace files cannot be accidentally staged, committed, or pushed; trying to add one prints "ignored by .gitignore." `sutando.config.local.json` is gitignored for the same reason.
- **Pre-commit + CI guard layers** — `scripts/check-workspace-not-tracked.sh` (pre-commit hook) and the equivalent CI check (see `docs/workspace-config.md` "Protection layers") fail the build if anything under `/workspace/` is tracked or if `sutando.config.local.json` is staged. Multi-layer defense: gitignore is the floor, hooks + CI are the ceiling.
- **Per-channel access tiers** — every bridge (Discord, Slack, Telegram) tags incoming tasks with `access_tier: owner | team | other`. Only `owner` tasks get full agent capabilities; `team` and `other` are delegated to `codex exec --sandbox read-only`, which cannot read workspace files outside its sandbox path. The bridge enforces this in-band by injecting a `===SUTANDO SYSTEM INSTRUCTIONS===` block into every non-owner task body — the agent follows those instructions verbatim and never processes the user-supplied content directly. See CLAUDE.md "Discord/Slack/Telegram access control."
- **TOFU + allowlist for owner identity** — first DM to a bridge auto-enrolls the sender as owner and writes `$CLAUDE_CONFIG_DIR/channels/<surface>/access.json`. Subsequent senders are checked against `allowFrom`; absent or non-matching senders fall through to non-owner tiers (no implicit owner promotion).
- **Sandboxed delegation for team/other** — non-owner tasks run under `codex exec --sandbox read-only`. The sandbox blocks filesystem writes and restricts reads to the agent's working directory, so workspace `state/`, `notes/`, `build_log.md`, etc. are unreachable to non-owner tiers even if the user-supplied prompt tries to coax otherwise.
- **Result-body delivery markers** — `[no-send]`, `[deduped: task-X]`, `[REPLIED]`, `[channel: <id>]` route or suppress bot replies *at the bridge layer*, not the agent layer. Even if the agent writes a confidential result, a `[no-send]` marker prevents it from reaching any user-facing surface.
- **Memory split (shared vs private)** — memory and notes live under `$SUTANDO_MEMORY_DIR` (default: `$CLAUDE_CONFIG_DIR/projects/.../memory/`), separate from the public repo. `scripts/sync-workspace.sh` (canonical as of v0.3.0) syncs to a private vault (`vault.remote_url` in `sutando.config.local.json`); never to a public one. The legacy `scripts/sync-memory.sh` flow remains during the deprecation window (v0.3.x) and is removed in v0.4.0. Memory content is loaded into agent context, never into PR bodies or commit messages.
- **Per-host install state under `state/auth/`** — `cloud-auth.json`, `device.json`, and other per-host credentials are scoped to one host's workspace and exempted from transient-state cleanup. They never sync via `sync-workspace.sh` (which excludes `state/auth/` by path) or via the legacy `sync-memory.sh` (which targeted `memory/` + `notes/` only).
- **Channel-confidentiality routing rule** (operational) — codified in the `feedback_channel_confidentiality` memory: strategic / competitive / financial content goes to owner-DM only, never to shared channels. When the owner asks "what's pending?" in a public channel, the agent lists only audience-appropriate items.

### 1.5 Expansion (user-side)

The user can add their own top-level subdirs (e.g. `drafts/`, `research/`, `screenshots/`, `inbox/`). New dirs are automatically gitignored (the whole `/workspace/` tree is) and inherit the §1.4 default-deny posture.

**Agent-facing rules for custom subdirs** — what content goes there, when the agent reads/writes them, retention — belong in `PERSONAL_CLAUDE.md` (per-user overrides). CLAUDE.md (shared) describes the built-in shape; `PERSONAL_CLAUDE.md` describes the per-user extension.

### 1.6 Archive / cleanup

Workspace bloat distracts Claude Code's cwd-anchored discovery (large `git status`, slow project-slug indexing, big tab-completion sets). Mitigation: a nightly cron archives older content.

- `tasks/processed/` and `logs/` older than 30 days → `archive/<yyyy-mm>/`.
- Suggested cron: 03:30 local. Script: `scripts/archive-workspace.sh` *(forthcoming — not yet shipped)*.
- `notes/` and `data/` are user content — never auto-archived. User manages.
- `build_log.md` is append-only — never archived. Owner manually rotates if it gets unwieldy (>500KB).

## 2. Resolution (implementation)

Path computation is centralized; do NOT reinvent the fallback per-script.

- **Python:** `from workspace_default import resolve_workspace` → `Path`
- **TypeScript:** `import { resolveWorkspace } from './workspace_default.js'` → `string`
- **Swift:** `AppDelegate.workspace` (split alongside `repoRoot` for code-adjacent paths)
- **Shell:** `WORKSPACE="$(bash scripts/sutando-config.sh workspace)"` then `"$WORKSPACE/..."`. The shared `src/workspace_resolve.sh` helper wraps this for scripts that source it.

**Resolution order (post-#1440):**
1. `sutando.config.local.json` → `workspace.path` (per-clone override, gitignored).
2. `sutando.config.json` → `workspace.path` (tracked defaults at repo root).
3. `<repo>/workspace/` baked-in default.

**Today (pre-#1440):** `$SUTANDO_WORKSPACE` env var is honored as a legacy escape hatch ahead of step 1, with a one-time deprecation warning. PR #1440 removes that step.

## 3. Migration from earlier versions

Users running with `$SUTANDO_WORKSPACE` pointing outside the repo (pre-M0 layout, or any pre-v0.8 host):

1. **`bash scripts/sutando-migrate.sh --dry-run`** — scan all three potential source locations (repo root, `~/.sutando/workspace/`, `$SUTANDO_WORKSPACE` env) and surface collisions before any move.
2. **`bash scripts/sutando-migrate.sh --commit`** — relocate content into `<repo>/workspace/` with rule-driven class resolution (`structural`, `newest-mtime`, `keep-both`, `append`, etc.). Per M1 Part 2 (#1403) + #1406.
3. **Post-#1440 auto-flow**: `src/startup.sh` detects `$SUTANDO_WORKSPACE` set with data and invokes the migration automatically, then compresses the original folder in place as `<legacy>-pre-v0.8-<ts>.tar.gz` (recoverable via `tar -xzf`) so stale processes hit `ENOENT` instead of writing to a divergent path.
4. **After migration**, unset `$SUTANDO_WORKSPACE` in shell rc + `.env`. The startup script already unsets it for child processes; cleaning the source is a one-time operator step.

**Edge case** — users with multiple repo checkouts pointing at one shared workspace lose that pattern under v0.8. Workaround: pick one canonical checkout, OR filesystem-symlink `<repoB>/workspace → <repoA>/workspace` (no tooling).

## 4. Vault (overview only)

The workspace is intentionally ephemeral: delete the repo and the workspace dies. The **vault** is the separate durability layer that persists content across reclones and syncs across hosts.

- `$SUTANDO_VAULT` — the durability env var. Defaults exist; user can override.
- Default content: memories. The user can extend the sync allowlist.
- Sync mechanism: `scripts/sync-workspace.sh` (canonical as of v0.3.0, cron-driven). Legacy `scripts/sync-memory.sh` remains during the v0.3.x deprecation window and is removed in v0.4.0.
- Cross-host topology, room collaboration, allowlist format, secret push-gate, conflict policy — **all out of scope here**. See `docs/vault-design.md` *(forthcoming)*.

## 5. Locked decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Workspace = `<repo>/workspace/`, **computed**, no env override (post-#1440) | Claude Code cwd-privilege capture; structural simplicity |
| 2 | Whole `/workspace/` tree gitignored | Prevents tracked-pollution anti-pattern (the historic `Path(__file__).parent.parent` regression) |
| 3 | Top-level dirs (tasks/results/state/logs/notes/data/relay/skills) + 2 .md files | Matches the decision guide; agent has a single answer for each write |
| 4 | Confidentiality default-deny | Workspace = owner-private; bots paraphrase, never quote |
| 5 | Custom user dirs allowed, documented in PERSONAL_CLAUDE.md | Per-user expansion without polluting shared CLAUDE.md |
| 6 | `state/auth/` exempt from cleanup | Per-host install state (cloud-auth, device.json, .alive sentinels) must survive transient-state sweeps |
| 7 | Nightly archive of stale `tasks/processed/` + `logs/` | Bounds cwd bloat (script forthcoming — not yet shipped) |
| 8 | Vault is a separate doc / separate concern | Workspace contract = location + structure only; durability is its own design |
| 9 | Auto-migration on bootstrap (post-#1440) | Operators don't have to remember `sutando-migrate.sh`; legacy env-pointed data is preserved as `<legacy>-pre-v0.8-<ts>.tar.gz` |

## 6. What's NOT in this doc

These belong in the vault-design doc, not here:
- Multi-host sync topology
- Room collaboration (multi-user shared content)
- Tier model (LOCAL / MACHINE / AGENT / ROOM)
- Per-tier git remote layout
- Allowlist format + push-gate (secret scanning)
- Conflict resolution policy
- Vault creation / invite flow
- `identity_<scope>.json` and the scopes registry

This is intentional. This doc (the workspace contract) defines location + structure. The vault doc defines durability + sharing. Keeping them separate lets each evolve at its own pace.

## 7. FAQ

**Q: Can I use a workspace dir other than `<repo>/workspace/`?**

A: Yes. Override `workspace.path` in your gitignored `sutando.config.local.json`:

```json
{
  "workspace": {
    "path": "/some/other/absolute/path"
  }
}
```

**But do it mindfully** — the in-repo default is what unlocks Claude Code's cwd-anchored features (CLAUDE.md + PERSONAL_CLAUDE.md auto-load, skills auto-discovery without `--add-dir`, project-slug session transcripts, hook scope). If you override outside the repo, you forfeit these and have to compensate (explicit `--add-dir <workspace>` per launch; fragmented session-transcript history). See §1.1's ⚠️ callout for the full breakdown.

Common legitimate reasons to override: shared workspace across multiple checkouts; custom storage location for size / encryption / quota.

**Q: Why isn't `$SUTANDO_WORKSPACE` honored anymore?**

A: It was the legacy escape hatch (pre-M0). Two problems with env-based overrides:

1. Two processes inheriting the env at different times read different values (cron jobs, launchd jobs, ad-hoc shells) — silent split-brain.
2. The env var doesn't survive a fresh `git clone`. New machines / checkouts don't see your override; the default applies inconsistently.

`sutando.config.local.json` solves both: it lives at a known path (gitignored), every process reads it the same way, and a new clone gets a clean state with the default.

**Q: How do I migrate an existing workspace from a pre-v0.8 layout?**

A: **You shouldn't have to do anything — `src/startup.sh` auto-migrates on first boot post-#1440** when it detects `$SUTANDO_WORKSPACE` set with data at the env-pointed path. It runs `sutando-migrate.sh --commit` for you, then compresses the original folder in place as `<legacy>-pre-v0.8-<ts>.tar.gz` so stale processes hit `ENOENT` instead of writing to a divergent dir.

**If for any reason the auto-migration didn't happen** (e.g. you launched a service directly without going through `startup.sh`, or your `$SUTANDO_WORKSPACE` was unset at startup but set later), just ask Sutando: *"migrate my workspace"* or *"my workspace is in the wrong place — fix it"*. Sutando will run the migration script for you, with diagnostics + collision detection.

**If you'd rather run it manually** (you want full visibility, you're testing the migration on a sandbox checkout, etc.):

```bash
bash scripts/sutando-migrate.sh --dry-run    # preview what would move — sources scanned: repo root, ~/.sutando/workspace/, $SUTANDO_WORKSPACE env
bash scripts/sutando-migrate.sh --commit     # actually relocate
bash scripts/sutando-migrate.sh --explain <path>   # walk the class-resolution rules for a single path
```

Sources scanned (A/B/C):
- **A** — repo root (any loose runtime files at the top of the checkout from pre-M0 installs).
- **B** — `~/.sutando/workspace/` (the pre-M0 default location).
- **C** — `$SUTANDO_WORKSPACE` env (the legacy escape hatch path).

Collisions are surfaced before any move — the script never silently overwrites.

**Q: My workspace ended up in a weird place after I forgot to migrate. What now?**

A: Run `bash scripts/sutando-migrate.sh --dry-run` to confirm what's where, then `--commit` to move it. If the old location has been compressed into a tarball by the auto-migration, `tar -xzf <legacy>-pre-v0.8-<ts>.tar.gz -C <legacy-parent>` to expand for inspection.

**Q: What if I want the workspace on a different filesystem (e.g. an external SSD for size reasons) but still inside the repo's git-tracked path?**

A: Use a filesystem-level symlink: `ln -s /Volumes/MyExtSSD/sutando-workspace <repo>/workspace`. The gitignore covers the link target; the helper resolves through the symlink. This keeps cwd-anchored features intact while letting you control physical storage.

## 8. Shipping history

| Milestone | PR(s) | What landed | Date |
|---|---|---|---|
| **M0** — in-repo default + config-driven resolution | [#1395](https://github.com/sonichi/sutando/pull/1395), [#1397](https://github.com/sonichi/sutando/pull/1397) | `sutando.config.{json,local.json}` loader; helper at `scripts/sutando-config.sh`; default flipped from `~/.sutando/workspace/` to `<repo>/workspace/` | 2026-06-01 |
| **M1 phase 1** — resolver hardening | [#1399](https://github.com/sonichi/sutando/pull/1399) | Helper bootstrap subcommands; resolver fixes | 2026-06-02 |
| **M1 part 2** — migration tool | [#1403](https://github.com/sonichi/sutando/pull/1403), [#1406](https://github.com/sonichi/sutando/pull/1406) | `scripts/sutando-migrate.sh` + `/sutando-migrate` skill; class-rule resolution; `--explain` / `--dry-run` / `--commit` modes; sources A/B/C scan | 2026-06-02 |
| **claude-config M0+M1** | [#1415](https://github.com/sonichi/sutando/pull/1415) | `claude-sutando` shell wrapper; workspace-scoped `CLAUDE_CONFIG_DIR` | 2026-06-02 |
| **catchup PID-stamp sentinel** | [#1431](https://github.com/sonichi/sutando/pull/1431) | Crash-safe restart detection for `/catchup-after-startup` | 2026-06-03 |
| **sync-memory SCRIPT_PARENT** | [#1432](https://github.com/sonichi/sutando/pull/1432) | Anchor helper lookup to `$SCRIPT_PARENT` instead of `$REPO_DIR` (fixes a stale-pin failure) | 2026-06-03 |
| **schedule-crons no-inline-fire + queue gate** | [#1437](https://github.com/sonichi/sutando/pull/1437) | Closes the SKILL.md `invoke it as /skill-name` ambiguity that caused mid-session avalanche; `scripts/cron-gate.sh` defers sub-daily crons when owner work is queued | 2026-06-03 |
| **v0.8 env strip + auto-migration** | [#1440](https://github.com/sonichi/sutando/pull/1440) (draft) | Strip `$SUTANDO_WORKSPACE` from resolver; bootstrap auto-migration + compress-in-place; bold-red deprecation warnings | in flight 2026-06-03 |
| **workspace contract v0.8 spec** | [#1384](https://github.com/sonichi/sutando/pull/1384) (draft) | The design doc that this file makes operationally true | in flight 2026-06-03 |
| **CLAUDE.md §Workspace restructure** | [#1379](https://github.com/sonichi/sutando/pull/1379) (draft) | Agent-facing decision guide + `<repo>/workspace/` paths | in flight 2026-06-03 |
