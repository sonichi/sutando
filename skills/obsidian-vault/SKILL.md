# obsidian-vault

Voice-inline capture into a Sutando-owned Obsidian vault. The voice agent calls `add_to_vault(kind, body, title?)` directly — no core round-trip, no Obsidian plugin required. Filesystem-direct: Obsidian's watcher picks up the change instantly when the vault is open.

## Vault

Lives at `$SUTANDO_WORKSPACE/obsidian-vault/` (default `~/.sutando/workspace/obsidian-vault`). Auto-created on first capture, with a `.obsidian/` marker dir so Obsidian recognizes the folder as a vault.

## Layout

Everything Sutando writes lives under the `Sutando/` subfolder, by kind:

```
$SUTANDO_WORKSPACE/obsidian-vault/
  .obsidian/                                 ← marker; Obsidian populates on first open
  Sutando/
    Notes/<slug>-<YYYY-MM-DDTHHMMSS>.md      kind="note"   → standalone file w/ frontmatter
    Tasks.md                                 kind="task"   → appended checkbox
    Thoughts/<YYYY-MM-DD>.md                 kind="thought" → appended timestamped block
```

This subfolder convention keeps Sutando's writes out of the way of anything else you put in the vault later.

## Triggers (what the voice agent listens for)

- "save this as a note" / "note that X" → `kind="note"`
- "add to my tasks" / "todo: X" / "remind me to X" → `kind="task"`
- "remember this thought" / "log this idea" → `kind="thought"`
- Ambiguous capture intents → the tool description picks `thought` for stream-of-consciousness, `task` for action-shaped, `note` otherwise.

## One-time setup in Obsidian

Open Obsidian → **File → Open vault → Open folder as vault** → pick `$SUTANDO_WORKSPACE/obsidian-vault`. Obsidian will remember it. The vault appears empty until you trigger your first capture.

## Opt-in: agent-state mirror + nightly dream

`add_to_vault` (the voice-inline capture tool) is always available — it only writes when you explicitly say "save this as a note" / "todo: ..." / "thought: ...".

Two automatic features are **opt-in via env var** and OFF by default:

- `src/obsidian-mirror.py` — one-way watchdog mirror of `tasks/` + `results/` + `notes/` + `pending-questions.md` into `Sutando/Agent/` inside the vault.
- Nightly `dream.py` cron — Opus-4.7-judged cross-linking (inline `(cf. [[X]])` citations + tiered `## Strongly Related` / `## Related` / `## See also` footer block).

Both are gated by `SUTANDO_OBSIDIAN_MIRROR`. To enable, add to `.env`:

```
SUTANDO_OBSIDIAN_MIRROR=1
```

Then `bash src/startup.sh` will boot the mirror. The cron-driven nightly dream will respect the same flag.

The on-demand voice tool `run_dream` *bypasses* the gate (it passes `--force` to `dream.py`) — explicit user invocation always wins.

## What's not in this skill (yet)

- **Search / weekly roundup / multi-file edits** — those are the "core" half of this integration. Plan: add `scripts/search.py`, `scripts/daily-roundup.py` driven via `work()` so heavier ops don't block the voice turn.
- **Obsidian Local REST API** — community plugin that exposes HTTP endpoints. We don't use it. Filesystem-direct is simpler and has no plugin dependency. Could be a future opt-in for read-side flows.

## Loader

Loaded by `src/inline-tools.ts:loadSkillManifestTools()` at voice-agent startup. To pick up changes: restart voice-agent and reconnect the web client (Gemini caches the tool list at session start).
