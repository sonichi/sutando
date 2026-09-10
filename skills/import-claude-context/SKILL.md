---
name: import-claude-context
description: "Bring the owner's Claude Code history into Sutando: index, extract and haiku-summarise the stock ~/.claude/projects transcripts, stage the result, show the owner a digest and — only on their 'bring it in' — land it in core memory, notes/claude-import/ and People payloads. Triggers: 'import my Claude history', 'read my Claude Code sessions', 'bring my Claude context along', 'what did I work on in Claude Code', and the review replies 'bring it in' / 'bring in <slug>' / 'forget <slug>'. Read-only on ~/.claude; conversation text only; transcripts are processed by the model providers the Sutando already uses, nothing is stored there and nothing is uploaded to AG2 Space."
user-invocable: true
---

# Import Claude context

Sutando starts out knowing the owner's projects, decisions, open threads and people by reading the conversations they already had with Claude Code. The transcripts under the **stock** `~/.claude/projects/` (`claude_home_path("projects", vanilla=True)` — never Sutando's relocated `.claude-sutando`) are indexed without a model, the dialog is cleaned and redacted on disk, cheap-model subagents summarise it, the result is **staged** and shown to the owner as a digest, and only their "bring it in" moves it into the Sutando-owned sinks.

**Usage**: `/import-claude-context [--projects a,b] [--session uuid] [--since 30d] [--dry-run] [--counts-only] [--json] [--new] [--cloud] [--stage] [--commit] [--discard] [--forget slug] [--purge-dumps]`

## When it runs

1. **Onboarding run** — a task arrives with `channel_id: onboarding-wizard` (written by the desktop's Import button; `priority: low`, `access_tier: owner`). Its text carries the consent timestamp. Run the procedure below with `--run-kind onboarding`.
2. **User ask** — the owner says "import my Claude history", "read my Claude Code sessions", or invokes `/import-claude-context`. Same procedure with `--run-kind user`.
3. **Review reply** — while `$DATA/staged/review.md` exists, an owner message saying **"bring it in"**, **"bring in <slug>"** or **"forget <slug>"** answers the pending review: run the matching command from step 10 and nothing else. Any other message is not an answer; the set stays staged.

In cases 1 and 2 the import is a **background job**: the task's own result is `[no-send]` (the wizard channel has no delivery path anyway), and everything the owner sees — the immediate acknowledgement, the digest, a failure note — is a proactive message to the owner DM (`results/proactive-<ts>.txt`). The owner's answer (case 3) arrives as a later task.

## Consent contract

- Run only after the onboarding card's **Import** button or an **explicit** owner request. A mention of Claude Code in passing is not consent; the health check, proactive loop and crons never start this on their own.
- That consent covers reading, summarising and **staging**. Landing the result is a second, separate yes: nothing reaches core memory, `notes/` or the People store until the owner has read the digest (`staged/review.md`) and replied "bring it in" (or "bring in <slug>") in the same conversation.
- An incremental re-run (`--new`: only sessions changed since the last import) needs no new import consent — the first consent covers keeping the import current — but its result is staged and reviewed like the first. Say what you are doing when you do it.
- `--cloud` (uploading the summaries to cloud-recall, corpus `claude-import`) is a **separate, explicit yes** every time. Never in the onboarding run; never inferred from the first consent.
- Before the first full run, when the owner is present, state the counts (`index.py --counts-only --json` — a file count, so "about N transcripts across M projects"; the exact number the owner is told is the index's `conversations`, step 2) and that a digest will follow for approval, then proceed. The onboarding run has the import consent already; do not ask again in the DM — the acknowledgement (step 2) and then the digest are the next things the owner sees.

## Procedure

Set `DATA=<workspace>/data/claude-import` (per host, not vault-synced) and `S=skills/import-claude-context/scripts`. Every script resolves the workspace through `bash scripts/sutando-config.sh workspace`.

The slow part (summaries, roll-ups, entities, staging) never runs in the core's task loop: the main task indexes, extracts, acknowledges, **spawns ONE background subagent** and returns `[no-send]` — "at background" means spawn a subagent (CLAUDE.md), and the core stays free for other tasks. `status.json` phases `indexed → extracted → summarizing → rolling-up → staged → done` let the desktop show progress; from step 5 on it is kept current by `progress.py`, never by hand.

**Main task (seconds):**

0. **Greeting first.** If the owner's first-contact message ("Hi — I'm all set up, say hello.") or any other owner message is waiting in the same batch as the import task, answer it first — a short hello, nothing about the import — and only then start the steps below. The greeting is the owner's first impression; the import is a background job and can wait a minute.

1. **Index** (no model): `python3 $S/index.py --json` → `$DATA/index.json` + `$DATA/claude-import-index.md` (per project: recovered `cwd`, session count, date range; one `[ ]` row per session with title + date). Phase `indexed`. `--dry-run` prints the counts and writes nothing; `--counts-only` is readdir+stat only — a **file count** that cannot tell a sidechain or a never-answered session apart, good for a fast estimate only. The full index reports `sessions`, `empty` (sessions with no assistant message: aborted or never answered — nothing gets summarised for them) and `conversations` = `sessions − empty`; **`conversations` is the number the owner is told.** The reported `new` is the number of sessions changed since the last index.
2. **Acknowledge immediately** — write `results/proactive-<ts>.txt` (owner DM) with `conversations` and `projects` from the index (`N` and `M` below) and nothing else; append ` (K empty ones skipped)` after "M projects" only when `empty` is > 0:

   > Importing your Claude Code history: N conversations across M projects. I'll message you here when it's ready to review — usually 10–20 minutes. Your transcripts are processed by the model providers your Sutando already uses; nothing is stored there and nothing is uploaded to AG2 Space.

3. **Extract**: `python3 $S/extract.py --json` (add `--new` on re-runs, `--projects`/`--session` to narrow, `--max-chars-total` to cap; default 8,000,000 chars, newest sessions first). Produces `$DATA/dumps/<slug>/<uuid>.<n>.txt` — dialog only, harness noise stripped, secrets redacted, ≤120k chars per chunk, 0600 files in a 0700 dir. A session whose cleaned dialog has no turn or fewer than 400 chars (`--min-chars`) gets no dump: it is `skipped_empty` in the counts and `skipped_empty: true` in `state.json` (a `--new` re-run leaves it alone until the file changes). `extracted` counts only the sessions that got a dump, so `extracted` + `skipped_empty` + `errors` is what was looked at. Phase `extracted`.
4. **Spawn the import coordinator** — ONE Agent-tool subagent, run in the background, briefed with steps 5–9, `$DATA`, `$S`, the run kind and the slugs to cover. It may run on the default model (it only orchestrates and reads counts) but must never paste transcript text; its per-session workers are `model: haiku`. Then end the task with a result body of `[no-send]`. Do not wait for the subagent; do not summarise inline; do not hand the work back to a later session.

**Import coordinator (background subagent):**

5. **Session summaries** — one Agent-tool subagent per session with **`model: haiku`** (owner rule, session-recap: never spend core-model quota on transcripts), prompt = `prompts/session-summary.md` with the slug/uuid/chunk paths filled in. The subagent Reads the chunk files and Writes `$DATA/summaries/<slug>/<uuid>.json` itself; do not paste dump text through the coordinator. Fan out in parallel batches; skip sessions whose `summaries/<slug>/<uuid>.json` is newer than their `extracted_at` in `state.json` (resumable) and sessions marked `skipped_empty: true` there (they have no chunk files — no worker). After each batch run `python3 $S/progress.py` (no model): it recounts from disk — sessions minus `skipped_empty`, summaries present, roll-ups, entities, staged manifest — and rewrites `status.json` (phase `summarizing` with `summarized`/`sessions`, `rolling-up` once every session is in). Never write `status.json` by hand. A subagent that answers `STOP` marks a missing or unreadable input — fix or skip, never retry blindly.
6. **Project roll-ups** — one haiku subagent per project, prompt `prompts/project.md`; reads that project's session JSONs, writes `$DATA/projects/<slug>.json` (roll-up + `note_markdown`). Incremental runs pass the previous roll-up so facts carry forward. Then `python3 $S/progress.py`.
7. **Entities** — one haiku subagent over all summaries, prompt `prompts/entities.md`, writes `$DATA/entities.json` (people, companies, deals, decisions, open threads, every entry cited). Then `python3 $S/progress.py`.
8. **Stage** (nothing lands yet): `python3 $S/finalize.py --stage --run-kind onboarding|user --json`. It first de-duplicates the entities in memory (people sharing an email are one person; a bare first name folds into its unique full name; companies by name — `people_merged` / `companies_merged` in the JSON; `entities.json` itself is not rewritten), then renders everything under `$DATA/staged/`: `memory/claude_import.md` (≤ 2,000 B), `notes/claude-import/<slug>.md` + `overview.md`, `people.json` (≤ 25 payloads, people with ≥ 2 citations), `review.md` (the digest) and `manifest.json`; `status.json` phase `staged`. It touches neither `memory_dir()`, nor `<workspace>/notes`, nor `MEMORY.md`. `--stage` is the default: a bare `finalize.py` cannot write into a sink. Then `python3 $S/finalize.py --purge-dumps` unless the owner asked to keep the dumps (the review and the commit read the summaries, not the dumps).
9. **Digest** — write `results/proactive-<ts>.txt` for the **owner DM only** containing `$DATA/staged/review.md` verbatim; it ends with the reply instructions ("Reply 'bring it in' to save this to your Sutando, 'bring in <slug>' for one project, or 'forget <slug>' to drop one."). This is the ONE place transcript-derived text is shown to the owner: one paragraph per project, its open threads and decisions, the people it would add (name, why, citation counts) and the size of the memory summary. If any step 5–8 fails part-way, write a short proactive message instead — what was done, as counts ("summarised K of N sessions"), and "say 'import my Claude history' to continue"; the run is resumable and the owner is never left without a message.

**Later task (the owner's reply, case 3 above):**

10. **Review reply** — the commit happens only here, on the owner's explicit yes, never in the run that produced the digest:
    - **"bring it in"** → `python3 $S/finalize.py --commit --json`: moves the staged set into the sinks with the usual guards — memory file ≤ 2,000 B, the `MEMORY.md` row only when `memory-index-budget.py` allows, notes with a dated `## Update` section on re-runs — sets `summarized_at` per session and phase `done`, prints the counts. Then `python3 $S/finalize.py --people-json` and upsert each payload with the station's `people__upsert_person` tool when it is connected (`source: claude-import`), otherwise note "People sync pending" and leave it for the next run. People are upserted **only after the commit**, never from the staged list.
    - **"bring in <slug>"** → `python3 $S/finalize.py --commit --projects <slug> --json` (an exact slug or a unique part of one; ambiguous is refused). The rest stays staged and `review.md` is re-rendered for it. Then `python3 $S/finalize.py --people-json --projects <slug>` and upsert those — only people cited at least twice inside the approved project.
    - **"forget <slug>"** → `python3 $S/finalize.py --discard --projects <slug>` while it is still staged; `python3 $S/finalize.py --forget <slug>` once it has been committed. A plain `--discard` drops the whole pending set.
    - Anything else, or no reply, leaves the set staged. Never commit on silence, on a re-worded question, or on the original import consent. `--commit` is refused when nothing is staged or when the summaries changed since staging (stale): stage again and re-post the digest.
11. **Completion message** (after the commit; counts only, plus the People names so they can be undone), as the reply to the owner's message:

    > Saved your Claude Code history: N sessions across M projects (K new). Wrote `notes/claude-import/overview.md` + M project notes and a 2 KB memory summary (MEMORY.md row: added | skipped — index full). People added to your store: A, B, C (say "forget <name>" to undo). Your transcripts are processed by the model providers your Sutando already uses; nothing is stored there and nothing is uploaded to AG2 Space. Say "import my Claude history" any time to pick up new sessions; `/import-claude-context --forget <slug>` removes one project.

    Never quote transcript text, titles of private sessions, or paths under `~/.claude` in the acknowledgement, the completion message or a failure note; the digest is the only message that carries derived text, and only in the owner DM.

## Never do

- Commit (`finalize.py --commit`) without the owner's explicit yes — "bring it in" or "bring in <slug>" — in the same conversation as the digest. Silence, the original import consent, a yes from anyone but the owner, or a stale staged set is not a yes. Upsert People only after that commit.
- Block the task loop on summarisation. The main task returns `[no-send]` right after spawning the background coordinator; summaries, roll-ups, entities and staging run in that subagent, never inline in the core task and never "later".
- Post the digest (`staged/review.md`) anywhere but the owner DM (`results/proactive-<ts>.txt`) — not in a channel, a task result, a log or a note.
- Write, move, rename or lock anything under `~/.claude` — the tree is opened read-only; `index.py`/`extract.py` refuse an out-dir inside it and `finalize.py` refuses a memory dir under it. Do not run `finalize.py` outside the core without `--memory-dir`.
- Read `subagents/**` or any nested transcript, or sidechain sessions (first message `isSidechain: true`) — index.py skips them; do not go around it.
- Let tool I/O, attachments, images or thinking blocks into a dump, a prompt or a note. The extractor takes user + assistant text only; keep it that way.
- Put transcript text on disk before it has been noise-stripped **and** run through `secret_scanner.scan_and_redact`. Keep `[STORED-IN-KEYCHAIN-…]` placeholders verbatim; never reconstruct a secret.
- Log content. `status.json`, stdout of the scripts (`--json`), the app log and the completion message carry counts only — no titles, prompts, quotes or paths. The digest (`staged/review.md`) is the one exception and goes to the owner DM only.
- Send content anywhere but the summarising haiku subagents (the model providers this Sutando already uses; nothing is stored there) without `--cloud` and a fresh explicit yes. No cloud-recall upload, no telemetry with content, no sharing of notes, nothing to AG2 Space.
- Summarise with anything but haiku; never paste dumps into the core session.
- Install anything to run this skill — no `pip install`, no `--break-system-packages`, no venv, no brew — and never offer to in the DM. When `secret_scanner` prints `mode: DEGRADED` (the `detect-secrets` package is absent on this Mac), the repo-local rules still run and the run proceeds: keep the acknowledgement exactly as in step 2 and add one line at the end of the digest — "Note: secret redaction used the built-in rules only on this Mac." — nothing more.
- Touch `user_profile.md` (read by literal name in `src/voice-context.ts`), or write the `MEMORY.md` row when `memory-index-budget.py` refuses — the file stays, the row is logged as skipped.
- Re-run on a schedule, auto-import for a non-owner, or import for a different user's home (`$SOURCE_CLAUDE_CONFIG_DIR` is honoured only because migration scripts already use it).

## CLI reference

| flag | script | effect |
|---|---|---|
| `--projects a,b` | index, extract, finalize | index/extract/`--stage`: slugs matching exactly or as a case-insensitive substring of the opaque slug (e.g. `gtm`); `--commit`/`--discard`/`--people-json`: an exact slug or a **unique** part of one — no match or an ambiguous one is refused |
| `--session uuid` | extract | one session (prefix allowed) |
| `--since 30d` | index | only transcripts modified in the window (`12h`, `2w`, `YYYY-MM-DD` also) |
| `--dry-run` | index | full pass, writes nothing, prints counts |
| `--counts-only` | index | readdir + stat only; opens no transcript; a **file count** for the pre-run estimate — the acknowledgement uses the full index's `conversations` (= `sessions − empty`) |
| `--json` | all | counts as JSON on stdout (counts only) |
| `--new` | index, extract | index: list only sessions changed since the last index; extract: only sessions changed since their last extraction |
| `--cloud` | (skill) | upload the finished summaries to cloud-recall corpus `claude-import` — separate explicit consent; not implemented in the scripts yet, do it by hand only when asked |
| `--stage` | finalize | **the default**: de-duplicate the entities and render the review set under `$DATA/staged/` (memory file, notes, `people.json`, `review.md`, `manifest.json`); writes no sink, phase `staged` |
| `--commit` | finalize | move the staged set into the sinks — only on the owner's yes; refused (exit 1) when nothing is staged or the staged set is stale; with `--projects` lands that subset and re-stages the rest (phase `done`, or `staged` while something is pending) |
| `--discard` | finalize | drop the staged set (or the `--projects` subset) without touching the sinks |
| `--people-json` | finalize | print the ≤ 25 People payloads (≥ 2 citations) — the staged copy while a review is pending, else computed; `--projects` restricts to people cited inside those projects. Approved only by `--commit` |
| `--forget slug` | finalize | remove exactly that project's note, summaries, dumps, roll-up, memory line, state, entity citations and staged copy (a pending review is re-rendered without it) |
| `--purge-dumps` | finalize | delete `$DATA/dumps/` (alone, or together with `--stage`/`--forget`) |
| `--max-chars-total N` | extract | soft cap for one run (default 8,000,000 chars, newest first) |
| `--min-chars N` | extract | a session with less cleaned dialog than this (default 400 chars), or none at all, is skipped: no dump, `skipped_empty` in the counts and in `state.json`, never counted as `extracted` |
| *(none)* | progress | recount from disk and rewrite `status.json` — phase `summarizing` / `rolling-up` / `staged` with `sessions` (minus `skipped_empty`), `summarized`, `rolled_up`, `entities`, `staged`; run after every coordinator batch |
| `--run-kind onboarding\|user` | finalize `--stage` | the last field of the note header line; `--commit` reuses the staged run kind |

Every script also takes `--root DIR` (another projects dir), `--out-dir`/`--data-dir`, `--workspace` and finalize `--memory-dir` for tests and unusual installs. Slugs start with `-` (`-Users-o-Projects-x`); `--projects`/`--forget` accept them as typed (`--forget -Users-o-Projects-x`) as well as `--forget=-Users-o-Projects-x`.

## Files it writes

| what | where |
|---|---|
| index, state, status, dumps, summaries, roll-ups, entities | `<workspace>/data/claude-import/…` (dumps 0600 in 0700) |
| staged review set (nothing landed yet) | `<workspace>/data/claude-import/staged/` (0700): `review.md` — the digest —, `memory/claude_import.md`, `notes/claude-import/<slug>.md` + `overview.md`, `people.json`, `manifest.json` (staged_at, run kind, pending slugs, input fingerprint); removed by `--commit`/`--discard` |
| narrative (only by `--commit`) | `<workspace>/notes/claude-import/<slug>.md`, `overview.md` — header `*[imported, claude-code] — import-claude-context \| <first> → <last> \| onboarding\|user*` |
| facts the core loads at start (only by `--commit`) | `memory_dir()/claude_import.md` (≤ 2,000 B) + one `MEMORY.md` row when the budget allows |
| people (only after `--commit`) | `people__upsert_person` payloads, `source: claude-import` |

Tests: `tests/import-claude-context-{common,index,extract,progress,finalize,stage}.test.py`. Parsing lives in `skills/session-recap/scripts/extract.py` (`--root`) and `src/context_resume.py`; this skill adds none of its own.
