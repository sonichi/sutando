# Manifest-loaded skills

Sutando's skill system has two shapes. Most skills are invoked through Claude Code's slash-command surface (`/skill-name`) or as standalone scripts. A subset are **manifest-loaded skills** that contribute *tools* directly into the agent's runtime tool table — so they appear alongside the built-in inline tools and Gemini can call them like any other function.

This doc covers the manifest-loaded path: what it is, how it works, who consumes the tools, how to add one.

## What it is

A manifest-loaded skill is a directory containing:
- `manifest.json` — declares the skill, its access tier, and (optionally) a tools entry point and config block
- `tools.ts` (if `manifest.tools` is set) — exports `tools: ToolDefinition[]`, picked up at agent startup
- optional `server.py` / `start.sh` / other runtime infrastructure the tools rely on

At voice-agent startup, `loadSkillManifestTools()` in `src/inline-tools.ts` scans the public `skills/` directory **and** the optional `$SUTANDO_PRIVATE_DIR/skills/` directory, dynamically imports each tools entry point, and merges the exported tool definitions into `inlineTools`.

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
     $SUTANDO_PRIVATE_DIR/skills (if set)
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

## Currently active manifest skills

Run `grep -l '"enabled": true' skills/*/manifest.json $SUTANDO_PRIVATE_DIR/skills/*/manifest.json` for the live list.

As of 2026-05-03, the public repo has no manifest-loaded tools shipping by default; the four currently-active manifest skills all live in the private dir:

| Skill | Tools contributed | Notes |
|---|---|---|
| `voice-context` | `set_voice_context`, `list_voice_contexts` | Switches the active per-talk voice script via `$SUTANDO_PRIVATE_DIR/voice-contexts/active`. Restarts voice-agent on switch so the new context loads. |
| `talk-highlight` | `highlight_slide`, `presenter_mode`, `fullscreen_presenter`, `set_active_slides` | Drives on-stage slide highlights during live talks via the local highlight server (`localhost:7877`). `highlight_slide` glows a topic key and dims siblings; `presenter_mode` toggles the session-level talk flag; `fullscreen_presenter` switches the slide window into fullscreen; `set_active_slides` swaps the deck pointer (`talk-slides/active`) so the same server can drive different decks across a session. |
| `personal-deictic` | `read_selection` | Reads the macOS selected text + cursor via the `ax-read` Swift binary; foundation for "this/that" deictic edits. |
| `personal-talk-prep` | (none — script-only skill, invoked via `/personal-talk-prep`) | Listed for completeness; no manifest tools. |

When a Sutando user enables a new manifest skill, the tool name appears in `[skill-loader] loaded N tool(s) from <name>` in the voice-agent log, and the tool becomes immediately callable from Gemini after the next voice-agent restart.

## How to add a new manifest-loaded skill

1. Create `skills/<name>/manifest.json` (or `$SUTANDO_PRIVATE_DIR/skills/<name>/manifest.json` for personal tools).
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
