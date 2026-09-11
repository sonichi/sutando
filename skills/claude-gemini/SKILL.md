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

## Delegating Work That Touches `workspace/`

Gemini's own file tools (`read_file`, `write_file`, `glob`, `search_file_content`) honor
`.gitignore`. This repo ignores `workspace/*` (`.gitignore`), and the workspace is where every
piece of per-user runtime state lives — `tasks/`, `results/`, `state/`, `logs/`, memory. So those
tools cannot see any of it, and a run told to read or write there fails with
`File path ... is ignored by configured ignore patterns`, often retrying the same call instead of
falling back.

- **A delegation that touches the workspace must be told to use shell commands only** — `cat`,
  `ls`, `printf`, `mv`, heredocs. Say so in the prompt; the run will otherwise reach for a file
  tool first and burn turns on the error.
- **`--approval-mode plan` has no shell tool at all**, so read-only mode has no route to workspace
  state: file tools are filtered out and `run_shell_command` is unavailable. Read-only questions
  about runtime state need a mode that can run commands, or the caller must pass the content in.
- **Paths outside the repo are rejected** as resolving outside the allowed directories (a
  `~/Library/LaunchAgents` read, for example). Pass `--include-directories <dir>` for those.

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

