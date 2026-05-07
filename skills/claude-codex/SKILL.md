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
- Spec-driven one-shot build of a self-contained artifact (HTML/CSS/JS prototype, single-file
  demo) — use `--goal` to invoke Codex's `/goal` mode

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

# Spec-driven one-shot build (Codex /goal mode)
bash "$SKILL_DIR/scripts/codex-run.sh" --goal -- "$(cat path/to/spec.md)"
```

## `/goal` mode (`--goal`)

Wraps Codex's interactive `/goal` slash-command for non-interactive use. The flag prepends
`/goal ` to the prompt and forces `--full-auto` so Codex can write files unattended.

Reach for it when:

- The task is a self-contained artifact (single HTML file, one-off demo, isolated script).
- You have a tight written spec and want a one-shot build with a known cost ceiling.
- A second-model take on a prototype is useful (run `--goal` in parallel with Claude's own
  build for cross-check).

Skip it when:

- The task touches in-repo code or needs memory/context Claude already has.
- You expect to iterate — `/goal` is one-shot and does not self-correct on failure.

## If Invoked As A Slash Command

- If ARGUMENTS is empty, explain the available modes and suggest `--review --uncommitted` for diffs or a plain prompt for general delegation.
- If ARGUMENTS starts with `--goal ` (e.g., `/claude-codex --goal Build a self-playing demo`), strip the prefix and route the remaining text through `--goal` mode:

```bash
PROMPT="${ARGUMENTS#--goal }"
bash "$SKILL_DIR/scripts/codex-run.sh" --goal -- "$PROMPT"
```

- Otherwise (plain prompt), run:

```bash
bash "$SKILL_DIR/scripts/codex-run.sh" -- "$ARGUMENTS"
```

