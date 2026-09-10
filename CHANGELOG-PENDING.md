# CHANGELOG-PENDING

Unreleased changes to be curated into the next `CHANGELOG.md` entry.

**Instructions for contributors:** add a one-line entry under the appropriate section when your PR introduces a user-visible change. PR number in brackets at the end. Entries are curated (not auto-generated) before each release.

Format: `- Brief description of what changed. ([#NNN])`

---

## Added

<!-- feat() PRs go here -->
- import-claude-context: new in-box, user-invocable skill that brings the owner's Claude Code history into Sutando — an LLM-free index of the stock `~/.claude/projects` transcripts, noise-stripped + secret-redacted 0600 dialog dumps, haiku summaries per session/project/entities, and sinks in core memory (≤2 KB file + budget-guarded `MEMORY.md` row), `notes/claude-import/` and People payloads; read-only on `~/.claude`, `--new` incremental, `--forget <slug>` undo. Review gate: `finalize.py` stages by default under `data/claude-import/staged/` (with a `review.md` digest and duplicate people/companies merged first) and writes the sinks only on the owner's "bring it in" (`--commit`, `--projects` for one project, `--discard` to drop). `session-recap/scripts/extract.py` gains `--root`; `context_resume` exports `message_text`/`clean_text`/`NOISE_*_RE`.
  - Counting: `extract.py` counts a session as `extracted` only when it wrote a dump — a session with no cleaned dialog or under `--min-chars` (400) is `skipped_empty`, remembered in `state.json` so `--new` leaves it alone (the fresh-install run said 47 extracted with 43 dumps on disk); `index.py` reports `empty` (no assistant message) and `conversations` = sessions − empty, the number the acknowledgement uses (`--counts-only` stays a file count); `finalize.py` leaves skipped sessions out of every session count and they never block `--commit`; new `progress.py` recounts summaries / roll-ups / entities / staging from disk and rewrites `status.json` (phase `summarizing` → `rolling-up` → `staged`) after each coordinator batch, so the desktop's progress no longer sits at `summarized 0`.
  - People store: an existing person is never overwritten or duplicated — `finalize.py --stage --known-people <file>` takes the station's `people__list_people` listing and matches every staged person by email, then by normalised name (diacritics folded); matches become UPDATE payloads (same slug/id, merged `identifiers.emails`, a `doc_append` section and no `doc`) that the coordinator merges into the fetched dossier with `--people-doc-merge` (same-heading section replaced, idempotent); a name two store entries share is "Needs your call" and is never upserted; the review lists "Already in your People store" and "New" separately (the 25 cap applies to new people only) or says "Store not checked" when the People tool was unavailable.
  - Review-gate hardening (PR #4127 review): the memory file and `overview.md` are rebuilt from per-project **approved snapshots** (`data/claude-import/approved/<slug>.json`, written by `--commit`), so committing one project never lands another's unreviewed roll-up re-run — the digest names it "changed since approval — bring in <slug> to refresh"; `--forget` takes a known slug (or a unique part of one) and refuses paths, `..`, `~`, unknown or ambiguous values, symlinks and any target resolving outside `notes/claude-import/`, `data/claude-import/` or the memory dir before deleting anything; `index.py` redacts every metadata string (titles, prompts, summary, agent name) with the importer's policy (`_common.redact_text`, the same object `extract.py` uses) before cutting it to length; `--forget`, `--forget-session` and `--hold` shrink the approved People export (`people.json`) to the citations still approved (`people_revoked`), and `--forget-session` drops the session's entity citations too; a commit grows that export only with the projects it lands — every other landed project keeps the citations in `approved/people-inputs.json` (whose per-project `people_hashes` let the digest name a landed project whose people changed since approval, under People: "bring in <slug> to refresh"), so an entities re-run between two commits never lands an unreviewed person or citation, and `--people-json --projects <landed slug>` prints the approved payloads.
  - Personal sessions are held: the session summary carries `personal` / `personal_reason` (a generic category, ≤ 6 words); a held session contributes nothing to memory, notes, entities or people and the review shows it only as "Held back as personal: <date> · <reason>"; the owner's `include <date>`, `include personal`, `hold <date>` and `forget <date>` map to `finalize.py --include / --include-personal / --hold / --forget-session` (a date shared by two sessions is refused with the uuids); a roll-up written with a held session, or without an included one, is marked stale, staged with a placeholder body and refused by `--commit` until that project's roll-up is re-run.
- report-feedback: `--auto` mode for agent-initiated bug reports — honors the owner's `state/feedback-prefs.json` toggles (auto-report + send-logs, both default on), dedupes identical titles (24h), and caps volume (5/day).

## Fixed

<!-- fix() PRs go here -->
- report-feedback: read the desktop host's Keychain session (origin-scoped `AG2_CLOUD_TOKEN_*` key) so filing works on Tauri installs, and default the cloud origin to `sutando.ag2.space` (the retired `.ai` host drops the bearer across its redirect).

## Changed

<!-- refactor(), perf(), docs() changes visible to operators go here -->

## Security

<!-- security fixes go here -->
