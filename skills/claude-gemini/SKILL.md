---
name: claude-gemini
description: "Renamed to agy. Compatibility alias for one release: /claude-gemini [prompt] behaves as /agy [prompt]. Use the agy skill directly."
user-invocable: true
---

# claude-gemini (renamed to agy)

This skill was renamed to `agy`, since it drives the Antigravity CLI `agy` and the old name
described the standalone `gemini` CLI Google folded into Antigravity. The skill now lives at
`skills/agy/`; see its `SKILL.md` for setup, the browser helper, guardrails and commands.

`/claude-gemini [prompt]` behaves as `/agy [prompt]`, and `scripts/gemini-run.sh` and
`scripts/agy-browser.sh` here forward to the `agy` scripts, so a saved command using the old path keeps
working. This alias is removed in the next release: switch to `/agy` and `skills/agy/scripts/`.

**Usage**: `/claude-gemini [prompt]` (prefer `/agy [prompt]`)

ARGUMENTS: $ARGUMENTS

## If Invoked As A Slash Command

- If ARGUMENTS is empty, say the skill is now `/agy` and point at `skills/agy/SKILL.md`.
- If ARGUMENTS is present, run the same script the `agy` skill runs:

```bash
bash "$SKILL_DIR/scripts/gemini-run.sh" -- "$ARGUMENTS"
```
