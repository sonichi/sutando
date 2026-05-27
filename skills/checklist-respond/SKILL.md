---
name: checklist-respond
description: Discord-native interactive checklist UI — renders bot replies that contain [checklist] as Discord buttons. Owner/team clicks update the message in-place; no reply needed.
---

# Checklist Respond

Renders bot replies as Discord interactive buttons when the reply contains a `[checklist]` marker. Eliminates the typing-a-letter / yes-no cycle for structured picks.

## MVP scope (Phase 1)

**Trigger**: explicit `[checklist]` marker in the result body. Auto-detect heuristic is Phase 2.

**Three patterns detected automatically:**

1. **Lettered options** — lines starting with `a)`, `b)`, `c)` etc.
2. **Markdown list** — `- [ ]` items (3+ items triggers checklist rendering)
3. **Yes/no** — single question with no list items

## Usage (agent side)

Include `[checklist]` anywhere in the result body:

```
[checklist]
Which episode should we post next?

a) Ep4 — park walk (filming not needed, text-only)
b) Ep5 — barber chair
c) Ep6 — park demo
```

Or for a review checklist:
```
[checklist]
PR review items for #1032:

- [ ] Tests cover all 4 steps
- [ ] No dead imports
- [ ] SKILL.md updated
```

Or yes/no:
```
[checklist]
Push feat/event-log-ts-1032 and open a PR?
```

## Architecture

- **`scripts/detector.py`** — parses the checklist kind + items from raw result text.
- **Bridge-side integration** (`src/discord-bridge.py`) — detects `[checklist]` in `poll_results()`, builds a `discord.ui.View`, posts with the view. State saved to `$SUTANDO_WORKSPACE/state/checklists/<msg_id>.json`. `on_interaction` handler updates the message on click.

## State schema

```json
{
  "task_id": "task-1234",
  "kind": "lettered|checklist|yesno",
  "items": [{"id": "a", "label": "Option A", "state": "pending"}],
  "voters": {},
  "original_text": "full original text including preamble",
  "channel_id": 123456
}
```

## Button custom_id format

`cl_{msg_id}_{item_id}` — max 100 chars. `msg_id` is the Discord message snowflake (18-19 digits); `item_id` is `a`/`b`/`c` for lettered, 0-indexed integer for list items, `yes`/`no` for yes-no.

## Click behavior

- Lettered: select one — picks the chosen item (check mark prefix), grays others
- Checklist: toggle ✅/⬜ per user per item; shows per-voter state (per Chi preference)
- Yes/no: records vote, updates labels to show tally

## Allowlist scope

Only users in the personal-bot allowlist can click. Clicks from outside are silently ignored (HTTP 200 + ephemeral "not authorized" message).
