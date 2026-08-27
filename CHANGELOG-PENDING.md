# CHANGELOG-PENDING

Unreleased changes to be curated into the next `CHANGELOG.md` entry.

**Instructions for contributors:** add a one-line entry under the appropriate section when your PR introduces a user-visible change. PR number in brackets at the end. Entries are curated (not auto-generated) before each release.

Format: `- Brief description of what changed. ([#NNN])`

---

## Added

<!-- feat() PRs go here -->
- report-feedback: `--auto` mode for agent-initiated bug reports — honors the owner's `state/feedback-prefs.json` toggles (auto-report + send-logs, both default on), dedupes identical titles (24h), and caps volume (5/day).

## Fixed

<!-- fix() PRs go here -->
- report-feedback: read the desktop host's Keychain session (origin-scoped `AG2_CLOUD_TOKEN_*` key) so filing works on Tauri installs, and default the cloud origin to `sutando.ag2.space` (the retired `.ai` host drops the bearer across its redirect).

## Changed

<!-- refactor(), perf(), docs() changes visible to operators go here -->

## Security

<!-- security fixes go here -->
