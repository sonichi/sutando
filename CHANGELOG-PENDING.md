# CHANGELOG-PENDING

Unreleased changes to be curated into the next `CHANGELOG.md` entry.

**Instructions for contributors:** add a one-line entry under the appropriate section when your PR introduces a user-visible change. PR number in brackets at the end. Entries are curated (not auto-generated) before each release.

Format: `- Brief description of what changed. ([#NNN])`

---

## Added

<!-- feat() PRs go here -->
- import-claude-context: new in-box, user-invocable skill that brings the owner's Claude Code history into Sutando — an LLM-free index of the stock `~/.claude/projects` transcripts, noise-stripped + secret-redacted 0600 dialog dumps, haiku summaries per session/project/entities, and sinks in core memory (≤2 KB file + budget-guarded `MEMORY.md` row), `notes/claude-import/` and People payloads; read-only on `~/.claude`, `--new` incremental, `--forget <slug>` undo. Review gate: `finalize.py` stages by default under `data/claude-import/staged/` (with a `review.md` digest and duplicate people/companies merged first) and writes the sinks only on the owner's "bring it in" (`--commit`, `--projects` for one project, `--discard` to drop). `session-recap/scripts/extract.py` gains `--root`; `context_resume` exports `message_text`/`clean_text`/`NOISE_*_RE`.
  - Counting: `extract.py` counts a session as `extracted` only when it wrote a dump — a session with no cleaned dialog or under `--min-chars` (400) is `skipped_empty`, remembered in `state.json` so `--new` leaves it alone (the fresh-install run said 47 extracted with 43 dumps on disk); `index.py` reports `empty` (no assistant message) and `conversations` = sessions − empty, the number the acknowledgement uses (`--counts-only` stays a file count); `finalize.py` leaves skipped sessions out of every session count and they never block `--commit`; new `progress.py` recounts summaries / roll-ups / entities / staging from disk and rewrites `status.json` (phase `summarizing` → `rolling-up` → `staged`) after each coordinator batch, so the desktop's progress no longer sits at `summarized 0`.
  - Speed: new `summarize.py` does the session summaries, project roll-ups and entities pass with direct, parallel Gemini Flash calls (schema-enforced JSON, platform-managed key first then BYO `GEMINI_API_KEY`, retries with backoff, resumable, counts/tokens-only output) instead of one haiku subagent per session — the owner's 43-session (5.6 MB) import drops from over ten minutes to 3 min 41 s (roll-ups of big projects go as parallel parts + one merge, the entities pass runs in small parallel groups merged in code — one request over 39 sessions never finished); exit 3 when no Gemini credential resolves, and SKILL.md keeps the haiku subagent procedure as that fallback. The owner-facing wording changes from "nothing leaves your Mac" to "processed by the model providers your Sutando already uses; nothing is stored there and nothing is uploaded to AG2 Space".
- report-feedback: `--auto` mode for agent-initiated bug reports — honors the owner's `state/feedback-prefs.json` toggles (auto-report + send-logs, both default on), dedupes identical titles (24h), and caps volume (5/day).

## Fixed

<!-- fix() PRs go here -->
- report-feedback: read the desktop host's Keychain session (origin-scoped `AG2_CLOUD_TOKEN_*` key) so filing works on Tauri installs, and default the cloud origin to `sutando.ag2.space` (the retired `.ai` host drops the bearer across its redirect).

## Changed

<!-- refactor(), perf(), docs() changes visible to operators go here -->

## Security

<!-- security fixes go here -->
