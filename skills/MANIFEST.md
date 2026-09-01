# Manifest-loaded skills

Sutando's skill system has two shapes. Most skills are invoked through Claude Code's slash-command surface (`/skill-name`) or as standalone scripts. A subset are **manifest-loaded skills** that contribute *tools* directly into the agent's runtime tool table — so they appear alongside the built-in inline tools and Gemini can call them like any other function.

This doc covers the manifest-loaded path: what it is, how it works, who consumes the tools, how to add one.

## What it is

A manifest-loaded skill is a directory containing:
- `manifest.json` — declares the skill, its access tier, and (optionally) a tools entry point and config block
- `tools.ts` (if `manifest.tools` is set) — exports `tools: ToolDefinition[]`, picked up at agent startup
- optional `server.py` / `start.sh` / other runtime infrastructure the tools rely on

At voice-agent startup, `loadSkillManifestTools()` in `src/inline-tools.ts` scans the public `skills/` directory **and** the optional `$SUTANDO_MEMORY_DIR/skills/` directory (legacy `$SUTANDO_PRIVATE_DIR` honored for one release per #870), dynamically imports each tools entry point, and merges the exported tool definitions into `inlineTools`.

The same `inlineTools` list is also pushed into the phone agent's tool table (see `skills/phone-conversation/scripts/conversation-server.ts:587`), so any tool a manifest-loaded skill contributes is automatically available to:

- The web voice agent (Gemini Live ↔ bodhi ↔ web-client)
- The phone agent (Twilio ↔ bodhi ↔ Gemini Live), for owner callers

Tools that need an instant response (sub-second round-trip) live in `src/inline-tools.ts` directly; everything else should live in a manifest-loaded skill.

## Manifest schema

```json
{
  "name": "skill-name",
  "enabled": true,
  "access_tier": "owner",
  "description": "Short human-readable summary; surfaced in code-review when changes land.",
  "tools": "./tools.ts",
  "server": "./server.py",
  "startup": "./start.sh",
  "config": {
    "SUTANDO_SOMETHING_URL": "http://localhost:7877"
  }
}
```

| Field | Required | Behavior |
|---|---|---|
| `name` | yes | Logged at load time; used for diagnostics |
| `enabled` | yes | `false` (or missing) → skill is skipped at startup |
| `access_tier` | yes | Currently informational; tier enforcement still happens at the call-site (work tool dispatch, Discord bridge, etc.) |
| `description` | recommended | Human summary for code review and docs |
| `tools` | optional | Relative path to a TS file exporting `tools: ToolDefinition[]`. Only present if the skill contributes runtime tools. |
| `server` | optional | Relative path to a long-running server script. Not auto-started — referenced for ops |
| `startup` | optional | Relative path to a script that boots the server (manual or via an orchestrator) |
| `config` | optional | Each entry is exported into `process.env` at agent startup, but only if the env var is not already set (so a user override wins over a manifest default) |

## Loader behavior

```text
1. Build dirsToScan = [
     <repo>/skills,
     $SUTANDO_MEMORY_DIR/skills (if set; legacy $SUTANDO_PRIVATE_DIR also honored)
   ]
2. For each dir, read each subdirectory:
   - If no manifest.json, skip.
   - If manifest.enabled is false/missing, skip.
   - Apply manifest.config -> process.env (setdefault semantics).
   - If manifest.tools is unset, skip (config-only skill).
   - Dynamic-import the tools file. If it exports an array `tools`, append.
3. The merged array is appended into `inlineTools` at module load.
4. `assertUniqueToolNames(inlineTools)` enforces no name collisions.
```

The two-directory scan lets a user keep personal tools (per-talk highlight maps, per-deck deictic targets, scratch tools) in their private memory-sync repo with real git history, without forking the public repo.

Order: public first, then private. If a private skill shares a tool name with a public one, the unique-name assertion fails at startup — by design, the loader does not silently shadow.

## Config-only manifests (non-tools skills)

A skill that contributes **no** runtime tools may still ship a `manifest.json` purely to **declare config** — omit `tools` and the loader applies `config → process.env` (setdefault) then skips the tools import (step 2 above). This is how a pipeline skill keeps its channel ids / feature flags / toggles out of ad-hoc `os.environ[...]` literals and in one declared place (the `config` block is the source of truth + default).

Precedent: `skills/wire-monitor/manifest.json` (`WIRE_REPORT_CHANNEL`) and `skills/wire-newsroom/manifest.json` (`WIRE_PUBLISH_CHANNEL`, `WIRE_AUTO_PACKAGE`). Their `description` notes "manifest present to DECLARE config per this convention."

How a **pipeline script** (a Python/bash stage that runs *outside* the voice-agent process, so it doesn't inherit the loader's `process.env`) reads a declared value, most specific first:

```
CLI arg  >  env override  >  manifest.json config[key]  >  another config file (e.g. sources.yaml)  >  a per-host state/<key> file
```

Read the manifest directly when needed — e.g. `publish-wire-episode.py:manifest_config()` reads `skills/wire-newsroom/manifest.json` `config[key]`; `wire-monitor` uses `${ENV:-$(cat state/wire-report-channel)}`. Never wire a bare invented `os.environ[...]` as the *primary* source.

## Currently active manifest skills

Run `grep -l '"enabled": true' skills/*/manifest.json "$SUTANDO_MEMORY_DIR/skills"/*/manifest.json` for the live list (legacy users may need `$SUTANDO_PRIVATE_DIR` in place of the new var).

As of 2026-05-03, the public repo has no manifest-loaded tools shipping by default; the four currently-active manifest skills all live in the private dir:

| Skill | Tools contributed | Notes |
|---|---|---|
| `voice-context` | `set_voice_context`, `list_voice_contexts` | Switches the active per-talk voice script via `$SUTANDO_MEMORY_DIR/voice-contexts/active` (legacy `$SUTANDO_PRIVATE_DIR` honored). Restarts voice-agent on switch so the new context loads. |
| `talk-highlight` | `highlight_slide`, `presenter_mode`, `set_active_slides` | Drives on-stage slide highlights during live talks via the local highlight server (`localhost:7877`). `highlight_slide` glows a topic key and dims siblings; `presenter_mode` toggles the session-level talk flag; `set_active_slides` swaps the deck pointer (`talk-slides/active`) so the same server can drive different decks across a session. (A fourth tool, `fullscreen_presenter`, is defined in the skill but deregistered since 2026-04-25 — core's generic `fullscreen` now auto-branches deck-vs-video, and the overlap confused tool routing; the function is kept for easy re-enable but is not loaded.) |
| `personal-deictic` | `read_selection` | Reads the macOS selected text + cursor via the `ax-read` Swift binary; foundation for "this/that" deictic edits. |
| `personal-talk-prep` | (none — script-only skill, invoked via `/personal-talk-prep`) | Listed for completeness; no manifest tools. |

When a Sutando user enables a new manifest skill, the tool name appears in `[skill-loader] loaded N tool(s) from <name>` in the voice-agent log, and the tool becomes immediately callable from Gemini after the next voice-agent restart.

## How to add a new manifest-loaded skill

1. Create `skills/<name>/manifest.json` (or `$SUTANDO_MEMORY_DIR/skills/<name>/manifest.json` for personal tools; legacy `$SUTANDO_PRIVATE_DIR` honored for one release per #870).
2. Set `"enabled": true`, `"access_tier": "owner"`, `"tools": "./tools.ts"`.
3. Write `tools.ts` that exports `tools: ToolDefinition[]`. Each tool needs `name`, `description`, `parameters` (a Zod schema), `execution: 'inline'`, and an `execute()` function. Reuse the shape from existing skills.
4. Restart voice-agent: `launchctl kickstart -k gui/$(id -u)/com.sutando.voice-agent`.
5. Confirm the skill loaded in `logs/voice-agent.log`.

The phone agent picks up the same tools automatically — no separate registration step.

## Phone-agent tool access

For owner callers, `conversation-server.ts` deduplicates by name and pushes `inlineTools` into the call session's tool table (`conversation-server.ts:587`). This means the phone agent has the **same inline-tool surface** as the web voice agent for owner calls, including all manifest-loaded skills. (System prompt and conversation lifecycle differ between phone and web — what's identical is the inline-tool table.)

For non-owner callers, only `anyCallerTools` and (for verified callers) `configurableTools` are exposed — manifest-loaded tools are NOT exposed to non-owners.

This implies, for the original questions:

- **Voice context switch (`set_voice_context` / `list_voice_contexts`)** — owner phone callers can switch context mid-call.
- **Presenter mode (`presenter_mode`)** — same; owner phone callers can toggle it.
- **`highlight_slide`** — same; useful if Chi is on stage with a phone call routing through Sutando.

Treat that as a feature, not a quirk: a call from a phone is conceptually the same agent the web client talks to, so the tools should match for the owner. Non-owners stay on the restricted surface.

---

## Package identity (v1) — the skill-package model, Phase 1

As of the skill-package work, a `manifest.json` is a **package manifest**, not just a
tool-loader config. It carries identity + a contract so we can build versioning,
trust, dependency resolution, and promotion around skills. Schema:
[`schemas/skill-manifest.schema.json`](../schemas/skill-manifest.schema.json);
validator: [`scripts/lint-skill.py`](../scripts/lint-skill.py).

**Design principle — transport-agnostic.** The manifest + a checksum + a lockfile
are the format; where a package is *resolved from* (a git repo today, a hosted
registry later, e.g. AG2 Space) is a separate concern. git→hosted is a resolver
backend swap, never a re-format.

### Fields (superset of the tool-loader fields above)

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | skill id / slug; must match the directory name |
| `scope` | opt | publish namespace for the SkillPack registry; `name` + `scope` → canonical `@scope/name` (e.g. `@sutando/zoom`). In-repo `name` stays flat, so the loader is unaffected |
| `version` | yes | SemVer. MAJOR=breaking, MINOR=compat feature, PATCH=fix/eval |
| `owner` | yes | who maintains it (handle / team / org) |
| `stability` | yes | `stable` \| `experimental` \| `deprecated` |
| `license` | rec | SPDX id (default: repo license) |
| `description` | rec | one-line summary (code review + registry) |
| `agent_compatibility` | opt | SemVer range of the agent runtime, e.g. `">=0.9 <2.0"` |
| `dependencies` | opt | `{skill-id: semver-range}` |
| `permissions` | rec | `{network, filesystem: none\|read-only\|read-write, secrets: none\|[keys]}` |
| `contract` | opt | `{inputs, outputs, guarantees}` — what downstream depends on across versions |
| `provenance` | opt | `{source_repo, forked_from, upstream_intent}` — keeps forks trackable |
| `enabled`, `access_tier`, `tools`, `server`, `startup`, `config` | — | manifest-loaded skills only (see above) |

`permissions` is a **declaration the linter cross-checks**: e.g. `network: false`
on a skill whose code calls `fetch`/`urllib` is flagged, because a permission that
lies is worse than none.

### Skill maturity (`stability`)

`stability` is a *signal that must be earned*, not a vibe — and it's meant to be
driven by **objective signals** so the agent can (eventually) self-maintain it:

- **`stable`** — earned when a skill has (a) been in the tree a while (rule of
  thumb: **age > ~3 months**), **and** (b) meaningful real usage with net-positive
  feedback (positive > negative), **and** (c) a committed interface — breaking
  changes require a MAJOR version bump.
- **`experimental`** (default) — new or unproven: limited usage, or an interface
  that may still shift in MINOR/PATCH. Every skill starts here.
- **`deprecated`** — superseded; don't adopt.

**Signals that drive promotion:**
- *Age* — computable today from git history (a skill's first-added date).
- *Usage + feedback* — net-positive real usage. Today a maintainer judgment; with
  the hosted registry (Phase 3) it becomes measurable (download counts, ratings).

**Intent — self-maintaining maturity.** As these signals accrue, a periodic job
re-derives `stability` from them, so the registry (and the agent) maintains the
field rather than relying on manual guesses. The initial manifest backfill seeded
`stability` from **age alone** (added on/before 2026-04-30 → `stable`, since we
have no usage data yet and the repo is young); the signal-driven process refines
that seed over time.

### Lint

```bash
python3 scripts/lint-skill.py skills/<name>     # one skill
python3 scripts/lint-skill.py --all             # every skills/*/manifest.json (CI)
python3 scripts/lint-skill.py --all --strict    # warnings are errors
```

### Migration status

Phase 1 migrates the manifest-loaded skills (`zoom`, `screen-companion`,
`gws-gmail-voice`, `obsidian-vault`) to v1. Backfilling the remaining
slash-command skills (adding a minimal `manifest.json` with `version`/`owner`/
`stability`) is the mechanical follow-up. Later phases add the registry index,
`skill.lock` + precedence resolution, and per-skill evals in CI.

### Trusted GitHub resolver

`skills/trusted-capabilities/` provides the first allowlisted GitHub resolver
for the package model. It discovers `SKILL.md` packages in the repositories
declared by its manifest, statically surfaces risk signals, resolves an exact
upstream commit, and installs file-by-file into the runtime skills directory.
Managed installs carry `.sutando-source.json`, which makes later update checks
repeatable and preserves source, path, and commit provenance.

Only sources marked `installable` may write files, and only below each source's
declared root. Tool repositories can be discovered and inspected but, like
awesome-list indexes, remain install-disabled: their install procedures and
runtime permissions are too source-specific for the generic skill installer.
Writes require `--yes` and replace the destination atomically; omitting it
performs an inspection-only dry run.
