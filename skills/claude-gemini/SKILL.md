---
name: claude-gemini
description: "Use the local Gemini CLI from Claude Code with the user's existing Gemini authentication or API configuration. Use for large-context repo scans, multimodal analysis, second-opinion planning, or structured Gemini runs in the current workspace."
user-invocable: true
---

# Claude Gemini

Delegate work from Claude Code to the local `gemini` CLI. This skill uses whatever authentication the Gemini CLI is already configured to use on this machine, including API key or signed-in CLI flows. It does not copy or export secrets.

**Usage**: `/claude-gemini [prompt]`

ARGUMENTS: $ARGUMENTS

## When to Use

- "Use Gemini on this repo"
- Need a large-context scan across many files
- Need multimodal or cross-module analysis from a second model
- Need a read-only or JSON-formatted Gemini pass from the current workspace

## Guardrails

- Default to `--approval-mode plan` for read-only analysis.
- Switch to `--approval-mode auto_edit` only when the user wants Gemini to make edits.
- Keep Gemini in the same repo by changing into the target workspace before running it.
- Prefer `--output-format json` or `stream-json` when another tool will consume the output.

## Quick Checks

```bash
bash "$SKILL_DIR/scripts/gemini-run.sh" --check
```

## Common Commands

```bash
# Read-only analysis
bash "$SKILL_DIR/scripts/gemini-run.sh" -- "Trace how tasks flow from voice input to execution"

# Explicit model selection
bash "$SKILL_DIR/scripts/gemini-run.sh" --model gemini-2.5-pro -- "Review the repo structure and identify weak points"

# Machine-readable output
bash "$SKILL_DIR/scripts/gemini-run.sh" --output-format json -- "Summarize risks in src/startup.sh"

# Allow edits when the user asked for implementation help
bash "$SKILL_DIR/scripts/gemini-run.sh" --approval-mode auto_edit -- "Implement a safer startup preflight for missing services"
```

## If Invoked As A Slash Command

- If ARGUMENTS is empty, explain the available modes and suggest `--approval-mode plan` for analysis.
- If ARGUMENTS is present, run:

```bash
bash "$SKILL_DIR/scripts/gemini-run.sh" -- "$ARGUMENTS"
```

