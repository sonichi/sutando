---
name: commons-app-builder
description: Build a working Room Commons app (an HTML page on room databases, registered as an app) when someone asks "Build a Commons app: <description>" or asks for a game, poll, sign-up sheet, tracker or similar interactive page in an s.space room. Designs the databases first, builds the page against the window.artifact bridge, holds it to a phone-width-first UX bar, verifies every control's data path, and replies with the link. The portable guide is GUIDE.md.
---

# commons-app-builder

**Read [`GUIDE.md`](GUIDE.md) and follow it, step by step.** It is the whole method. It uses
only the `room.commons.*` Actions and the page's `window.artifact` API, so agents outside
Sutando follow the same file. Keep it portable: Sutando-only material goes here, not there.

## When to use

- Use it for "Build a Commons app: …", "make us a … app in this room", or a game, poll,
  sign-up, RSVP, tracker or watch-party page that people use together.
- Don't use it for a static page, write-up or deck. Those are a Doc or a plain HTML page
  (the `room-commons` skill).

## Sutando specifics

- **Actions.** Call them through the s.space MCP tools (`room.actions.describe`,
  `room.action.read`, `room.action.execute`). The `ag2-space-mcp` skill maps them and
  registers the connector when none is present. The `room-commons` skill's CLI offers the same
  operations where it is installed (`--kind db create`, `page-add`, `--kind html-<id> append`,
  `app create`, `app scaffold`). Use one route per build, not both.
- **Static pre-flight.** Before you publish, run:

  ```bash
  python3 skills/commons-app-builder/scripts/check_page.py page.html
  ```

  It fails on sandbox-blocked APIs (network, modals, `<form>`, history), a missing viewport or
  dark mode, writes without the consent retry, or a hard-coded database id. It warns on missing
  `onChange`, storage, touch targets under 44px, hover-only controls and buttons that claim to
  send. It does not replace GUIDE.md step 6: still render the page at about 390px and about
  1280px.
- **Room rules still apply.** The reply goes where the request came from. Durable work stays
  in Room Commons. Summon the requester with `room.commons.summon` instead of pasting the link
  twice.
