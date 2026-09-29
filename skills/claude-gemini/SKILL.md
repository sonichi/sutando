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
   - Gemini API key: set `"modelProvider": "gemini"` in `~/.gemini/antigravity-cli/settings.json`,
     merging it into any keys already there rather than overwriting the file:

     ```bash
     F=~/.gemini/antigravity-cli/settings.json; mkdir -p "${F%/*}"; [[ -s "$F" ]] || echo '{}' > "$F"
     "$(bash "$SKILL_DIR/../../scripts/sutando-config.sh" python-bin)" -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["modelProvider"]="gemini"; json.dump(d,open(p,"w"),indent=2)' "$F"
     ```

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

`start` is safe to re-run and does nothing already done. It uses a port only when the process
listening on it (found with `lsof`) runs on the agy profile, so it never adopts the user's Chrome;
it reads each process's argv with the repo's `python3` (`$SUTANDO_PY`, the bundled runtime, then a
PATH `python3` that is not the macOS developer-tools stub), so a profile path containing a space
cannot match a neighbour. It refuses a profile already running on another port, and fails (stopping
only the Chrome process group it launched) when `agy mcp list` fails or the chrome-devtools entry
changes while it registers. Runs take `~/.gemini/agy-browser.lock` around the registry read and add,
since `agy mcp add` overwrites; a hand-run `agy mcp add` in that moment is not covered.
`stop` waits for that profile's processes to exit and fails if they do not. The profile defaults to
`~/.gemini/antigravity-browser-profile`; `--port`, `--profile` and `--chrome` override. Then ask `agy`
to use the MCP tools by name:

```bash
agy -p "Using the chrome-devtools MCP tools, open https://example.com and save a screenshot to /tmp/shot.png" \
  --print-timeout 240s
```

Headless `agy` cannot ask for permission, so it denies any tool not allowed in `settings.json`, and a
denial ends the run with no output. Allow the browser tools and each site the run may open, in
`permissions.allow` (merged into the file as in Setup):

```json
"permissions": { "allow": ["mcp(chrome-devtools/*)", "execute_url(example.com)"] }
```

Those two rules are enough to open a page and save a screenshot; shell commands stay denied.
`--dangerously-skip-permissions` also works, but it lets `agy` run any shell command, write any file
and use the network as you, while it reads untrusted web pages, so one hostile page can make it run
a command. Use it only for a site you trust, and only when the narrow rules cannot do the job.

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

