---
name: claude-pi
description: "Use the local Pi CLI (pi.dev) from Claude Code with the user's existing Pi authentication (Kimi, Claude Pro/Max, or ChatGPT OAuth). Use for second-opinion analysis, implementation, or reviews from a Kimi-family model in the current workspace."
user-invocable: true
---

# Claude Pi

Delegate work from Claude Code to the local `pi` CLI (pi.dev). This skill uses whatever
authentication Pi is already logged into on this machine (`~/.pi/agent/auth.json` — OAuth
for Kimi, Claude Pro/Max, or ChatGPT). It does not copy or export secrets.

**Usage**: `/claude-pi [prompt]`

ARGUMENTS: $ARGUMENTS

## When to Use

- "Use pi on this repo" / "ask kimi"
- Need a second opinion from a Kimi-family model (default: `kimi-coding/kimi-for-coding`)
- Need a non-Anthropic, non-OpenAI, non-Google perspective on a design or diff
- Need a scripted, non-interactive agent pass (`pi -p`) in the current workspace

## Guardrails

- Default provider/model is `kimi-coding/kimi-for-coding`; override with `--provider`/`--model`.
- Use `--read-only` for analysis passes — it restricts Pi to its `read` tool (no bash/edit/write).
- Without `--read-only`, Pi has full tools (read, bash, edit, write) in the working directory —
  same trust level as a default Codex `workspace-write` run.
- Keep Pi in the target repo via `--cd`; prefer `--mode json` when another tool consumes the output.

## Quick Checks

```bash
bash "$SKILL_DIR/scripts/pi-run.sh" --check
```

## Common Commands

```bash
# Read-only analysis
bash "$SKILL_DIR/scripts/pi-run.sh" --read-only -- "Trace how tasks flow from voice input to execution"

# Different provider/model (any Pi is logged into)
bash "$SKILL_DIR/scripts/pi-run.sh" --provider kimi-coding --model k3 -- "Review the repo structure"

# Machine-readable output
bash "$SKILL_DIR/scripts/pi-run.sh" --mode json -- "Summarize risks in src/startup.sh"

# Allow edits when the user asked for implementation help
bash "$SKILL_DIR/scripts/pi-run.sh" -- "Implement a safer startup preflight for missing services"
```

## If Invoked As A Slash Command

- If ARGUMENTS is empty, explain the available modes and suggest `--read-only` for analysis.
- If ARGUMENTS is present, run:

```bash
bash "$SKILL_DIR/scripts/pi-run.sh" -- "$ARGUMENTS"
```
