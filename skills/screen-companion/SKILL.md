---
name: screen-companion
description: "Sutando watches your screen and helps in real time, with the interaction pattern pre-configured per use case. Reads papers with you, pair-debugs, reviews PRs — without you having to narrate intent every session."
---

# Screen Companion

Sutando is already capable of watching your screen + voice-chatting in real time (vision push-mode from PR #735 + bodhi `VoiceSession`). The gap this skill closes: **Sutando doesn't know what you're trying to do.** Every session you have to re-narrate intent ("I'm reading a paper, ask about figures"; "I'm debugging, suggest hypotheses").

This skill ships pre-baked **interaction-pattern configs** — each one encodes a purpose, the right system-prompt overlay, the right tool subset, and the right vision cadence. Owner activates a config by name; the skill handles the rest.

## When to Use

- *"Read this paper with me"* — paper-reading mode (PDF / arXiv / blog).
- *"Debug this with me"* — stack-trace + IDE pair-debug.
- *"Review this PR with me"* — GitHub PR diff walk-through.
- Or any other use-case that has a config in `configs/`.

NOT for: silent screen-watching (use the voice agent directly), one-shot questions about a screenshot (use the `look_at_screen` inline tool).

## Architecture

```
configs/<name>.yaml             # the interaction pattern, declarative
        ↓
scripts/activate.ts             # entry: --config <name> [--goal "..."]
        ↓ loads config, builds:
        ↓
voice-agent's VoiceSession      # gets a system-prompt overlay + tool subset
        ↓                       # + vision_mode + cadence_ms config
Push-mode vision frames flow    # at the configured cadence
        ↓
Owner asks questions in voice   # answers grounded in what's on screen
```

The skill itself is small — most of the value lives in the configs. New use case = drop a YAML into `configs/`. No code change required.

## Configs ship with v0

| Config | Activation | Vision mode | Shape |
|---|---|---|---|
| `guided-setup` | `--config guided-setup --goal "..."` or *"guide me through this"* | push, 700ms | **proactive + goal-directed** (Sutando narrates next steps for a configuration task) |

**v0 demo angle:** "Sutando helps you set up something you've never done before" (e.g., a Discord dev portal bot config). User shares screen, names the goal at activation, Sutando narrates the next concrete step in real time. More visceral demo than paper-reading; matches a near-universal user pain (everybody fights some dev portal once a quarter).

### Coming in follow-up PRs (sketched in `screen_companion.md` at repo root)

- `pair-debug` — reactive + exploratory mode for stack-trace + IDE pair-debug.
- `pair-review-code` — reactive + exploratory mode for GitHub PR diff walk-through.

These don't ship in this PR — the schema gets validated against `guided-setup` first; siblings land once the integration shape is locked in.

## Adding a new use case

Drop a file at `configs/<your-use-case>.yaml`. Required fields:

```yaml
name: your-use-case-name
activation:
  voice_phrases: ["phrase one", "phrase two"]    # spoken triggers
  button_label: "Button label"                    # for Sutando.app UI
  cli_alias: "your-alias"                         # for the CLI
vision_mode: push          # or "pull"
vision_cadence_ms: 1000    # only for push
system_prompt_overlay: |
  Free-form description of: what the user is doing,
  what kinds of questions they'll ask, what NOT to do.
tools_allow:
  - tool-name-1
  - tool-name-2
goal_template: "Optional one-liner with {goal} placeholder"
```

After adding, run `npx tsx scripts/activate.ts --list` to confirm the loader picks it up. No skill rebuild required — configs are loaded fresh on each activation.

## Run

**Via voice (preferred):** say one of the activation phrases for the mode you want. For `guided-setup`: *"help me set this up"*, *"guide me through this"*, *"walk me through this"*, or *"I don't know what to click"*. Sutando picks up the phrase, asks for the goal if it isn't already clear from context, then activates the mode via the `activate_screen_companion` tool.

**Via CLI (preview the config without activating):**

```bash
npx tsx skills/screen-companion/scripts/activate.ts --config guided-setup --goal "find the bot token in the Discord developer portal"
```

This prints the activation card — mode prompt, tools allowed, vision cadence — without entering the mode. Useful for tuning a new config before wiring it to voice.

## How activation wires through

The voice agent loads `activate_screen_companion` via the skill manifest (`manifest.json` → `tools.ts`). When Gemini calls it with `mode` + `goal`:

1. The tool reads `configs/<mode>.yaml` and returns a structured payload (`instructions`, `tools_allow`, `vision_mode`, `vision_cadence_ms`, `activation_message`).
2. Gemini speaks `activation_message`, then treats `instructions` as its operating prompt for the rest of the session.
3. If `vision_mode` is `push` and screen-share isn't already streaming, Gemini asks the user to start it. Vision is owner-driven (the user starts screen-share); the tool does not toggle it directly.

Restart the voice-agent for the manifest to pick up newly-added configs / SKILL.md changes (configs themselves are loaded fresh on each activation, so they don't require a restart).

## Open questions still being worked

See `screen_companion.md` at the repo root. Owner is iterating on the model (M1 picked: master skill + configs).

## Trust + scope

Configs are non-executable YAML — they only declare the interaction shape. No path to arbitrary code execution from a config alone.

**`tools_allow` is advisory in v0.** The activation tool returns the allow-list in its payload and instructs Gemini to self-restrict via the system-prompt note. There is no hard-enforcement path that scopes the live VoiceSession's tool surface to the allow-list — Gemini sees the field as guidance, not a hard constraint. A future PR will add hard enforcement at the bodhi-realtime-agent layer; until then, treat configs as the documented intent, not the security boundary. Configs still cannot grant tools the active VoiceSession doesn't already expose (the field is a hint, not a grant), so the worst case is "Gemini calls a tool you didn't intend to allow" — not privilege escalation.

## Privacy + data handling

**By default, this skill DOES NOT take screenshots, record video, or persist frames to disk.** Frames are sent to Gemini Live for real-time understanding (the API call itself), and discarded — nothing lands on the local filesystem.

What IS persisted by default:
- **Text transcript** of the spoken conversation, in `<workspace>/data/screen-companion-<sessionId>.jsonl`. Same convention as `phone-conversation/conversation-server.ts` + `discord-voice/discord-voice-server.ts`.
- **Notes the user explicitly asks Sutando to take** via the `take_note` tool, in `<workspace>/notes/`.

What is NOT persisted by default:
- Vision frames (the screenshots themselves) — sent to Gemini Live and discarded.
- Audio recordings — same.
- Video recordings of the session — never.

If the user explicitly asks ("save this screenshot", "record this session"), Sutando will explain what gets saved and where, then either do it or ask for confirmation depending on the action's reversibility. Saving is opt-in per request, not per session.

The transmission boundary: frames cross the wire to Google's Gemini Live API (that's where the understanding happens). Per `phone-conversation/SKILL.md` precedent, that's documented as the trust boundary — anyone who's comfortable with screen-sharing to a Gemini-Live-backed voice agent is the audience for this skill. If you wouldn't share the screen to a colleague's video call, don't share it here.
