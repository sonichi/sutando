---
name: claude-gemini
description: "Use the local Antigravity CLI (agy, Gemini-backed) from Claude Code with the user's existing Gemini authentication or API configuration. Use for large-context repo scans, multimodal analysis, second-opinion planning, or structured Gemini runs in the current workspace."
user-invocable: true
---

# Claude Gemini

Delegate work from Claude Code to the local `agy` (Antigravity CLI) — Google folded the standalone
`gemini` CLI this skill used to wrap into Antigravity CLI in 2026, so `agy` is what this skill drives
now. This skill uses whatever authentication `agy` is already configured to use on this machine
(Gemini API key or signed-in Google auth). It does not copy or export secrets. `agy`'s Gemini-API-key
path needs BOTH `modelProvider: "gemini"` set in `~/.gemini/antigravity-cli/settings.json` AND the
`GEMINI_API_KEY` env var — the env var alone does nothing; `gemini-run.sh --check` reports both.
If `agy` isn't installed but the legacy `gemini` CLI still is, `gemini-run.sh` falls back to it
automatically, so a gemini-only host keeps working.

**Usage**: `/claude-gemini [prompt]`

ARGUMENTS: $ARGUMENTS

## Setup

1. Install the Antigravity app and CLI:

   ```bash
   brew install --cask antigravity
   curl -fsSL https://antigravity.google/cli/install.sh | bash   # installs ~/.local/bin/agy
   ```

2. Pick one way to authenticate:
   - Google sign-in: run `agy` once interactively and follow the prompt.
   - Gemini API key: write `{"modelProvider": "gemini"}` to `~/.gemini/antigravity-cli/settings.json`
     and have `GEMINI_API_KEY` in the environment `agy` runs in. Keep the key in the vault
     (`secret-vault.py env GEMINI_API_KEY -- ...`), never in a file.

3. Confirm: `bash "$SKILL_DIR/scripts/gemini-run.sh" --check`.

## Browser

`agy`'s browser tools need a Chrome that exposes the DevTools protocol. Its built-in `/browser`
attaches to the user's running Chrome and offers to restart it — never accept that, since it holds
the user's own tabs and sessions. Give `agy` a Chrome of its own instead:

```bash
bash "$SKILL_DIR/scripts/agy-browser.sh" start    # headless Chrome on its own profile, 127.0.0.1:9222,
                                                  # and registers the chrome-devtools MCP server with agy
bash "$SKILL_DIR/scripts/agy-browser.sh" status
bash "$SKILL_DIR/scripts/agy-browser.sh" stop     # stops only that profile's Chrome
```

`start` is safe to re-run and does nothing already done. The profile defaults to
`~/.gemini/antigravity-browser-profile`; `--port`, `--profile` and `--chrome` override. Then ask `agy`
to use the MCP tools by name:

```bash
agy -p "Using the chrome-devtools MCP tools, open https://example.com and save a screenshot to /tmp/shot.png" \
  --dangerously-skip-permissions --print-timeout 240s
```

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

# Explicit model selection (see `agy models` for the current list)
bash "$SKILL_DIR/scripts/gemini-run.sh" --model gemini-3.1-pro-high -- "Review the repo structure and identify weak points"

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

