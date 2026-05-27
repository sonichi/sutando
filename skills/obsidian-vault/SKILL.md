# Obsidian Vault

Mirror Sutando workspace state into an Obsidian vault for human-readable browsing and
cross-linking. Gated by `SUTANDO_OBSIDIAN_MIRROR=1` (opt-in) so it never runs silently.

## Vault layout

```
$SUTANDO_WORKSPACE/obsidian-vault/
├── .obsidian/           # Obsidian config (created on first run, left alone after)
└── Sutando/
    ├── Notes/           # notes/ mirror
    ├── Tasks.md         # live task queue (appended, not replaced)
    └── Thoughts/        # ephemeral thoughts appended by the voice agent
```

Open this folder as an Obsidian vault to get full graph view, search, and backlinks.

## Triggers

| Voice command | Action |
|---|---|
| "add a note about X" / "save this to Obsidian" | `add_to_vault` with `kind=note` |
| "add to my task list" | `add_to_vault` with `kind=task` |
| "save that thought" | `add_to_vault` with `kind=thought` |
| "dream" / "connect my notes" | `run_dream` — cross-links similar notes via LLM |

## Opt-in gate

Set `SUTANDO_OBSIDIAN_MIRROR=1` in `.env` (or the shell environment) to enable mirroring.
Without it, `obsidian-mirror.py` and `dream.py` exit immediately with an informational message.

To bypass the gate for one-off runs:
```bash
python3 src/obsidian-mirror.py --force
python3 skills/obsidian-vault/scripts/dream.py --force
```

## obsidian-mirror.py (src/)

One-shot sweep — safe to run repeatedly:

```bash
python3 src/obsidian-mirror.py           # opt-in gate applies
python3 src/obsidian-mirror.py --force   # bypass gate
python3 src/obsidian-mirror.py --since 1h  # only items newer than 1h
```

What it mirrors:
- `tasks/task-*.txt` → `Sutando/Agent/Tasks/<id>.md`
- `results/task-*.txt` → appended as Result block in matching task note
- `notes/*.md` → `Sutando/Notes/<slug>.md`
- `pending-questions.md` → `Sutando/Agent/Asks.md`

## dream.py (skills/obsidian-vault/scripts/)

Nightly LLM pass that discovers cross-links between vault notes:

```bash
python3 skills/obsidian-vault/scripts/dream.py           # opt-in gate applies
python3 skills/obsidian-vault/scripts/dream.py --force   # bypass gate
python3 skills/obsidian-vault/scripts/dream.py --dry-run # show proposed links, no writes
```

Uses `SUTANDO_DREAM_MODEL` (default: `claude-opus-4-7`) via the Anthropic API.
Appends `(cf. [[note-stem]])` at paragraph end for strong connections (tier ≥ strong).
Idempotent: skips pairs already cross-linked.

Schedule (nightly at 03:37 — see `skills/schedule-crons/crons.example.json`):
```json
{"name":"obsidian-dream","cron":"37 3 * * *","prompt":"Run python3 skills/obsidian-vault/scripts/dream.py..."}
```

## tools.ts

Exposes two voice-callable tools to `src/voice-agent.ts`:
- `add_to_vault` — write note/task/thought directly to vault (no mirror step needed)
- `run_dream` — fire dream.py in background (detached, returns immediately)

## Dependencies

- Python 3.9+, `anthropic` package (for dream.py)
- Obsidian installed locally (optional — vault is plain markdown files)
- `ANTHROPIC_API_KEY` in environment (for dream.py)
