---
name: claude-codex
description: "Use the local Codex CLI from Claude Code with the user's existing Codex login or API key. Use for Codex reviews, second-opinion analysis, implementation delegation, or non-interactive Codex runs in the current workspace."
user-invocable: true
---

# Claude Codex

Delegate work from Claude Code to the local `codex` CLI. This skill assumes Codex is already authenticated on this machine. It does not mint, extract, or transfer credentials.

**Usage**: `/claude-codex [prompt]`

ARGUMENTS: $ARGUMENTS

## When to Use

- "Ask Codex to review this change"
- "Use my Codex subscription from Claude Code"
- Need a second model to inspect a bug, review a diff, or propose an implementation
- Need a Codex result saved or streamed from the current repo

## Guardrails

- Prefer `codex review --uncommitted` for code review.
- Prefer `codex exec` for analysis, planning, or implementation prompts.
- Keep Codex pointed at the same repo with `-C "$PWD"` unless the user asked for another directory.
- Default to `workspace-write` sandbox or stricter. Do not use bypass flags unless the user explicitly asks.

## Quick Checks

```bash
codex login status
bash "$SKILL_DIR/scripts/codex-run.sh" --check
```

## Common Commands

```bash
# General delegation
bash "$SKILL_DIR/scripts/codex-run.sh" -- "Inspect src/task-bridge.ts for race conditions"

# Safer review of current uncommitted changes
bash "$SKILL_DIR/scripts/codex-run.sh" --review --uncommitted -- "Prioritize bugs and missing tests"

# Review against a base branch
bash "$SKILL_DIR/scripts/codex-run.sh" --review --base main -- "Focus on regressions and security"

# Save the last Codex message to a file
bash "$SKILL_DIR/scripts/codex-run.sh" --output-last-message results/codex-review.txt -- "Review the current workspace"
```

## If Invoked As A Slash Command

- If ARGUMENTS is empty, explain the available modes and suggest `--review --uncommitted` for diffs or a plain prompt for general delegation.
- If ARGUMENTS is present, run:

```bash
bash "$SKILL_DIR/scripts/codex-run.sh" -- "$ARGUMENTS"
```

