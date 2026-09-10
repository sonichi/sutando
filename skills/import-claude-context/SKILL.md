---
name: import-claude-context
description: "Bring the owner's Claude Code history into Sutando: index, extract and haiku-summarise the stock ~/.claude/projects transcripts into core memory, notes/claude-import/ and People payloads. Triggers: 'import my Claude history', 'read my Claude Code sessions', 'bring my Claude context along', 'what did I work on in Claude Code'. Read-only on ~/.claude; conversation text only; nothing leaves the Mac."
user-invocable: true
---

# Import Claude context

Sutando starts out knowing the owner's projects, decisions, open threads and people by reading the conversations they already had with Claude Code. The transcripts under the **stock** `~/.claude/projects/` (`claude_home_path("projects", vanilla=True)` — never Sutando's relocated `.claude-sutando`) are indexed without a model, the dialog is cleaned and redacted on disk, cheap-model subagents summarise it, and the results land only in Sutando-owned sinks.

**Usage**: `/import-claude-context [--projects a,b] [--session uuid] [--since 30d] [--dry-run] [--counts-only] [--json] [--new] [--cloud] [--forget slug] [--purge-dumps]`

## When it runs

1. **Onboarding run** — a task arrives with `channel_id: onboarding-wizard` (written by the desktop's Import button; `priority: low`, `access_tier: owner`). Its text carries the consent timestamp. Run the full procedure below with `--run-kind onboarding`; that task's own result body starts with `[no-send]` (the wizard channel has no delivery path) and the completion message goes to the owner DM as a proactive message (`results/proactive-<ts>.txt`).
2. **User ask** — the owner says "import my Claude history", "read my Claude Code sessions", or invokes `/import-claude-context`. Reply in the asking channel.

## Consent contract

- Run only after the onboarding card's **Import** button or an **explicit** owner request. A mention of Claude Code in passing is not consent; the health check, proactive loop and crons never start this on their own.
- An incremental re-run (`--new`: only sessions changed since the last import) needs no new consent — the first consent covers keeping the import current. Say what you are doing when you do it.
- `--cloud` (uploading the summaries to cloud-recall, corpus `claude-import`) is a **separate, explicit yes** every time. Never in the onboarding run; never inferred from the first consent.
- Before the first full run, when the owner is present, state the counts (`index.py --counts-only --json`: "N conversations across M projects") and what will be written, then proceed. The onboarding run has that consent already; do not ask again in the DM.

## Procedure

Set `DATA=<workspace>/data/claude-import` (per host, not vault-synced) and `S=skills/import-claude-context/scripts`. Every script resolves the workspace through `bash scripts/sutando-config.sh workspace`.

1. **Index** (seconds, no model): `python3 $S/index.py --json` → `$DATA/index.json` + `$DATA/claude-import-index.md` (per project: recovered `cwd`, session count, date range; one `[ ]` row per session with title + date). Write `status.json` phase `indexed`. `--dry-run` prints the counts and writes nothing; `--counts-only` is readdir+stat only. The reported `new` is the number of sessions changed since the last index.
2. **Extract**: `python3 $S/extract.py --json` (add `--new` on re-runs, `--projects`/`--session` to narrow, `--max-chars-total` to cap; default 8,000,000 chars, newest sessions first). Produces `$DATA/dumps/<slug>/<uuid>.<n>.txt` — dialog only, harness noise stripped, secrets redacted, ≤120k chars per chunk, 0600 files in a 0700 dir. Phase `extracted`.
3. **Session summaries** — one Agent-tool subagent per session with **`model: haiku`** (owner rule, session-recap: never spend core-model quota on transcripts), prompt = `prompts/session.md` with the slug/uuid/chunk paths filled in. The subagent Reads the chunk files and Writes `$DATA/summaries/<slug>/<uuid>.json` itself; do not paste dump text through the core. Run several in parallel; skip sessions whose `summaries/<slug>/<uuid>.json` is newer than their `extracted_at` in `state.json` (resumable). Tick the session's `[ ]` in `claude-import-index.md` as each lands and keep `status.json` at phase `summarizing` with `summarized`/`sessions` counts. A subagent that answers `STOP` marks a missing or unreadable input — fix or skip, never retry blindly.
4. **Project roll-ups** — one haiku subagent per project, prompt `prompts/project.md`; reads that project's session JSONs, writes `$DATA/projects/<slug>.json` (roll-up + `note_markdown`). Incremental runs pass the previous roll-up so facts carry forward.
5. **Entities** — one haiku subagent over all summaries, prompt `prompts/entities.md`, writes `$DATA/entities.json` (people, companies, deals, decisions, open threads, every entry cited).
6. **Finalize**: `python3 $S/finalize.py --run-kind onboarding|user --json` writes `memory_dir()/claude_import.md` (≤ 2,000 B), the guarded `MEMORY.md` row, `<workspace>/notes/claude-import/<slug>.md` + `overview.md`, `status.json` phase `done`, `summarized_at` per session. Then `python3 $S/finalize.py --people-json` → up to 25 payloads (people with ≥ 2 citations); upsert each with the station's `people__upsert_person` tool when it is connected (`source: claude-import`), otherwise note "People sync pending" and leave it for the next run. Finally `python3 $S/finalize.py --purge-dumps` unless the owner asked to keep the dumps.
7. **Completion message** (counts only, plus the People names so they can be undone):

   > Imported your Claude Code history: N sessions across M projects (K new). Wrote `notes/claude-import/overview.md` + M project notes and a 2 KB memory summary (MEMORY.md row: added | skipped — index full). People added to your store: A, B, C (say "forget <name>" to undo). Nothing left your Mac. Say "import my Claude history" any time to pick up new sessions; `/import-claude-context --forget <slug>` removes one project.

   Onboarding run: this goes to the owner DM via `results/proactive-<ts>.txt`; the task's own result is `[no-send]`. Never quote transcript text, titles of private sessions, or paths under `~/.claude` in the message.

## Never do

- Write, move, rename or lock anything under `~/.claude` — the tree is opened read-only; `index.py`/`extract.py` refuse an out-dir inside it and `finalize.py` refuses a memory dir under it. Do not run `finalize.py` outside the core without `--memory-dir`.
- Read `subagents/**` or any nested transcript, or sidechain sessions (first message `isSidechain: true`) — index.py skips them; do not go around it.
- Let tool I/O, attachments, images or thinking blocks into a dump, a prompt or a note. The extractor takes user + assistant text only; keep it that way.
- Put transcript text on disk before it has been noise-stripped **and** run through `secret_scanner.scan_and_redact`. Keep `[STORED-IN-KEYCHAIN-…]` placeholders verbatim; never reconstruct a secret.
- Log content. `status.json`, stdout of the scripts (`--json`), the app log and the completion message carry counts only — no titles, prompts, quotes or paths.
- Send anything off the Mac without `--cloud` and a fresh explicit yes. No cloud-recall upload, no telemetry with content, no sharing of notes.
- Summarise with anything but haiku; never paste dumps into the core session.
- Touch `user_profile.md` (read by literal name in `src/voice-context.ts`), or write the `MEMORY.md` row when `memory-index-budget.py` refuses — the file stays, the row is logged as skipped.
- Re-run on a schedule, auto-import for a non-owner, or import for a different user's home (`$SOURCE_CLAUDE_CONFIG_DIR` is honoured only because migration scripts already use it).

## CLI reference

| flag | script | effect |
|---|---|---|
| `--projects a,b` | index, extract | only these slugs (exact or case-insensitive substring of the opaque slug, e.g. `gtm`) |
| `--session uuid` | extract | one session (prefix allowed) |
| `--since 30d` | index | only transcripts modified in the window (`12h`, `2w`, `YYYY-MM-DD` also) |
| `--dry-run` | index | full pass, writes nothing, prints counts |
| `--counts-only` | index | readdir + stat only; opens no transcript; the "found N conversations" number |
| `--json` | all | counts as JSON on stdout (counts only) |
| `--new` | index, extract | index: list only sessions changed since the last index; extract: only sessions changed since their last extraction |
| `--cloud` | (skill) | upload the finished summaries to cloud-recall corpus `claude-import` — separate explicit consent; not implemented in the scripts yet, do it by hand only when asked |
| `--forget slug` | finalize | remove exactly that project's note, summaries, dumps, roll-up, memory line, state and entity citations |
| `--purge-dumps` | finalize | delete `$DATA/dumps/` |
| `--max-chars-total N` | extract | soft cap for one run (default 8,000,000 chars, newest first) |
| `--run-kind onboarding\|user` | finalize | the last field of the note header line |

Every script also takes `--root DIR` (another projects dir), `--out-dir`/`--data-dir`, `--workspace` and finalize `--memory-dir` for tests and unusual installs. Slugs start with `-` (`-Users-o-Projects-x`); `--projects`/`--forget` accept them as typed (`--forget -Users-o-Projects-x`) as well as `--forget=-Users-o-Projects-x`.

## Files it writes

| what | where |
|---|---|
| index, state, status, dumps, summaries, roll-ups, entities | `<workspace>/data/claude-import/…` (dumps 0600 in 0700) |
| narrative | `<workspace>/notes/claude-import/<slug>.md`, `overview.md` — header `*[imported, claude-code] — import-claude-context \| <first> → <last> \| onboarding\|user*` |
| facts the core loads at start | `memory_dir()/claude_import.md` (≤ 2,000 B) + one `MEMORY.md` row when the budget allows |
| people | `people__upsert_person` payloads, `source: claude-import` |

Tests: `tests/import-claude-context-{index,extract,finalize}.test.py`. Parsing lives in `skills/session-recap/scripts/extract.py` (`--root`) and `src/context_resume.py`; this skill adds none of its own.
