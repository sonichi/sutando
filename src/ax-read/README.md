# ax-read

Minimal CLI that returns frontmost-app accessibility data as JSON.

```
$ ./ax-read
{"app":"Cursor","cursor":[1421.0,398.0],"selected":"the previous paragraph"}
```

Used by the voice agent's deictic edit-mode (see
`notes/deictic-edit-mode-design-2026-04-30.md`): when the interim
transcript hits a deictic word ("this", "that", "here"), the voice
agent shells out to this binary and stamps the snapshot into the
turn's deictic-refs context for the LLM to resolve.

## Build

```
swiftc -O -o ax-read ax-read.swift -framework Cocoa -framework ApplicationServices
```

## Output schema

```json
{
  "app": "<localized frontmost app name, or '' if none>",
  "selected": "<AXSelectedText of the focused element, or '' if no selection>",
  "cursor": [x, y]   // NSEvent.mouseLocation, bottom-left Cocoa coords; null on failure
}
```

Errors keep the schema (empty strings / null cursor) so callers can use it
uniformly without branching on stderr.

## Why a separate binary

AX read via `osascript`/AppleScript has gaps across apps — e.g. `tell
process P to value of attribute "AXSelectedText" of focused element`
returns "Can't get attribute AXFocused" on Cursor. The C-level AX API
(`AXUIElementCopyAttributeValue`) reads cleanly and consistently.

A Node/TS binding would also work but requires native deps; a 60KB
Swift binary is simpler to ship.

## Permissions

The first call needs Accessibility permission for whatever process is
invoking it (`tsx`/`node` for voice-agent). macOS prompts on first run.
