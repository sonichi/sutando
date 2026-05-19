# Context-Drop AX-Read

Tiny Swift CLI that reads the focused app's text selection via the macOS Accessibility API. Sutando.app's ⌃C "drop context" hotkey shells out to this binary to capture what you have highlighted.

## What it does

Runs once per invocation and emits a single JSON line on stdout:

```json
{"app":"Discord","window_title":"@sutando — Discord","url":"","selected":"the highlighted text","path":"ax"}
```

`path` values:
- `"ax"` — `AXSelectedText` returned non-empty (native NSTextView path)
- `"clipboard"` — AX returned empty, Cmd+C fallback wrote new clipboard content
- `"none"` — both paths returned empty (or AX was denied)

The Cmd+C fallback uses `NSPasteboard.changeCount` to distinguish "Cmd+C actually wrote something" from "clipboard already had content" — otherwise the previous clipboard contents would be silently reported as the new selection. The prior pasteboard string is restored after the probe so a failed-copy attempt doesn't corrupt the clipboard.

## Build

```bash
bash skills/context-drop/build.sh
```

Produces `skills/context-drop/ax-read`. `src/startup.sh` runs this automatically when the binary is missing or older than the source.

## Why a separate binary instead of inlining in Sutando.app

LSUIElement=YES menu-bar agents (which Sutando.app is) have a TCC attribution chain that can misroute AX queries to the parent bundle even after a re-grant. A separately-signed CLI launched as a child gets its own clean trust binding, and the resulting AX queries succeed consistently. The binary is also reusable by any other tool that needs a one-shot selection read — voice agents, other hotkey handlers, automation scripts.

## Permissions

The first time `ax-read` runs it'll trigger the macOS Accessibility prompt for **Sutando.app** (its parent, who macOS attributes the AX queries to). Approve the prompt; the binding persists until the next codesign identity change. See PR #902 for the full TCC story.

## Relationship to the private `personal-deictic` skill

The private `personal-deictic` skill ships a richer AX-read with screenshot + cursor capture for deictic phrases ("this", "here"). The skill here is the text-only subset Sutando.app's ⌃C handler needs. Public-repo users get the full ⌃C experience without needing the private skill installed.

`Sutando.app::resolveAxReadPath()` tries the private path first (when available) and falls back to this public binary, so users with the private skill keep their richer deictic captures while public users get the text-only path.
