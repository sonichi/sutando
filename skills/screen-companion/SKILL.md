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

| Config | Activation | Vision mode |
|---|---|---|
| `pair-read-paper` | `--config pair-read-paper` or *"read this with me"* | push, 1000ms |
| (more to come — `pair-debug`, `pair-review-code`) | | |

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

After adding, run `bash scripts/activate.ts --list` to confirm the loader picks it up. No skill rebuild required — configs are loaded fresh on each activation.

## Run (v0 skeleton — load + print only)

```bash
npx tsx skills/screen-companion/scripts/activate.ts --config pair-read-paper
```

This v0 ships the **loader + printer**: it reads the config and prints what *would* happen (mode prompt, tools allowed, vision cadence). No wiring to the voice agent yet. Followups: real wiring as a separate PR.

## Open questions still being worked

See `screen_companion.md` at the repo root. Owner is iterating on the model (M1 picked: master skill + configs).

## Trust + scope

Configs are non-executable YAML — they only declare the interaction shape. No path to arbitrary code execution from a config alone. The tool allow-list is enforced at activation time; configs cannot grant tools the active VoiceSession doesn't already expose.
