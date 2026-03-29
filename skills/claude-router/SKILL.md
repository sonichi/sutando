---
name: claude-router
description: "Choose between the local Codex CLI and Gemini CLI from Claude Code. Use for automatic model selection when the user wants the best local delegate for code review, repo-wide analysis, planning, or implementation."
user-invocable: true
---

# Claude Router

Route a task from Claude Code to either local `codex` or local `gemini` using simple, explicit rules. This skill assumes the dedicated `claude-codex` and `claude-gemini` skills are installed from this repo.

**Usage**: `/claude-router [prompt]`

ARGUMENTS: $ARGUMENTS

## Routing Rules

- Route to `codex` for:
  - code review
  - bug hunting in a small or medium code slice
  - implementation requests
  - patch validation on current changes
- Route to `gemini` for:
  - repo-wide scans
  - architecture and dependency tracing
  - large-context summarization
  - multimodal or structured JSON output requests

If the request explicitly names `codex` or `gemini`, honor that directly.

## Default Behavior

- Default review-oriented prompts to Codex.
- Default broad analysis prompts to Gemini in `plan` approval mode.
- Keep execution in the current workspace unless the user requested a different directory.

## Quick Checks

```bash
bash "$SKILL_DIR/scripts/route-ai.sh" --check
```

## Common Commands

```bash
# Auto-route based on prompt
bash "$SKILL_DIR/scripts/route-ai.sh" -- "Review the current diff for regressions"

# Force Codex
bash "$SKILL_DIR/scripts/route-ai.sh" --engine codex -- "Inspect src/task-bridge.ts for races"

# Force Gemini
bash "$SKILL_DIR/scripts/route-ai.sh" --engine gemini -- "Trace task flow across the entire repo"

# Dry-run the route decision without executing
bash "$SKILL_DIR/scripts/route-ai.sh" --dry-run -- "Summarize architecture risks in this repo"
```

## If Invoked As A Slash Command

- If ARGUMENTS is empty, explain the route rules and suggest a plain prompt.
- If ARGUMENTS is present, run:

```bash
bash "$SKILL_DIR/scripts/route-ai.sh" -- "$ARGUMENTS"
```

