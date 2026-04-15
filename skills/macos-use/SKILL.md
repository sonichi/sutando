---
name: macos-use
description: "GUI control for macOS apps via mediar-ai's mcp-server-macos-use. Click, type, scroll, key-press, open apps — driven by accessibility tree, works in non-interactive Claude Code mode. Use this for any Sutando task that needs to drive another macOS application (Safari, Zoom, Mail, Finder, etc.)."
user-invocable: false
---

# macos-use

Drive macOS applications from Claude Code via mediar-ai's [mcp-server-macos-use](https://github.com/mediar-ai/mcp-server-macos-use). A Swift MCP server that wraps the macOS Accessibility API. Unlike Claude's built-in `computer-use`, this works in non-interactive mode (which is how Sutando's proactive loop and task bridge run), does not hold a machine-wide lock, and does not require a Pro/Max subscription.

## When to use

- "Open Safari and navigate to github.com" — anything requiring real GUI interaction with an app
- "Click the Join button on the Zoom invite dialog"
- "Type this into the Discord message box"
- "Scroll the frontmost window to the bottom"
- Any task that currently falls back to AppleScript + Quartz mouse events in `src/inline-tools.ts`

Prefer this skill over:
- `bash src/screen-capture.sh` — that captures screenshots; `macos-use` actually *interacts*
- AppleScript `tell application` blocks — more reliable, better error handling
- `cliclick` — lower-level, no accessibility context
- Claude's built-in `computer-use` — that mode requires interactive sessions and holds a lock that contends with Sutando's own loop

## Tools exposed

After install, these appear as `mcp__macos-use__*` in Claude Code:

| Tool | Parameters | Purpose |
|------|------------|---------|
| `open_application_and_traverse` | `identifier` (name/bundle ID/path) | Launch or activate an app, return its a11y tree |
| `click_and_traverse` | `pid`, `x`, `y` | Click at coordinates in a target app, return updated tree |
| `type_and_traverse` | `pid`, `text` | Type into the frontmost element |
| `press_key_and_traverse` | `pid`, `key` | Press a named key (Return, Tab, Escape, arrows, ...) |
| `scroll_and_traverse` | `pid`, `direction`, `amount` | Scroll in a direction |
| `refresh_traversal` | `pid` | Re-read the a11y tree without acting |

Every tool returns an accessibility-tree snapshot of the target app — structured UI elements with roles, titles, positions, and identifiers. No pixels. Model reasons over the tree, not over screenshots.

## Install

Two steps, one-time:

```bash
# 1. Build the Swift binary (~35s)
bash skills/macos-use/scripts/build.sh

# 2. Register with Claude Code's MCP config (writes ~/.claude.json)
bash skills/macos-use/scripts/install-mcp.sh

# 3. Grant Accessibility permission
#    System Settings → Privacy & Security → Accessibility
#    Click +, navigate to ~/.macos-use-mcp/.build/release/mcp-server-macos-use, enable.
```

Restart Claude Code after install for the MCP tools to appear.

## Gotchas

- **Swift 6 build fragility**: the `swift-sdk` transitive dep has data-race errors that Swift 6.3+ strict-concurrency trips on. `build.sh` uses `-Xswiftc -swift-version -Xswiftc 5` as a workaround. When upstream fixes this, remove the flag.
- **Accessibility permission**: the binary must be added to System Settings → Privacy & Security → Accessibility, or every tool call will return "not authorized". First-run error is obvious; owner must click through once.
- **Apps without good a11y trees**: Canvas / Electron / games degrade badly. For those, fall back to `screen-capture.sh` + Claude vision.
- **Build dep pulled from GitHub**: air-gapped Macs won't work. No prebuilt releases yet.
- **Multi-node**: each node builds its own binary. Not synced via `sutando-memory.git` (binaries are machine-specific). Run `build.sh` + `install-mcp.sh` on Mac Mini and MacBook separately.

## Quick self-test

After install + restart:

```
Sutando, open Safari and navigate to https://github.com/sonichi/sutando
```

You should see Claude invoke `mcp__macos-use__open_application_and_traverse` with `identifier: "Safari"`, then `type_and_traverse` into the URL bar, then `press_key_and_traverse` with `Return`.

## Related
- Research + decision memo: `notes/issue-65-computer-use-research.md`
- Issue: [#65 Add Claude Computer Use support](https://github.com/sonichi/sutando/issues/65)
- Upstream: https://github.com/mediar-ai/mcp-server-macos-use
