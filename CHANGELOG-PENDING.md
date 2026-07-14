# CHANGELOG-PENDING

Unreleased changes to be curated into the next `CHANGELOG.md` entry.

**Instructions for contributors:** add a one-line entry under the appropriate section when your PR introduces a user-visible change. PR number in brackets at the end. Entries are curated (not auto-generated) before each release.

Format: `- Brief description of what changed. ([#NNN])`

---

## Added

<!-- feat() PRs go here -->

## Fixed

<!-- fix() PRs go here -->

- Pending questions listed in the web UI can be answered again. `POST /answer` skipped every free-form section, so answering any question returned "not found or already answered"; question ids are now derived from the title (stable across rewrites of the file) instead of the section's position, and archived entries below the `# Resolved` divider are no longer offered as open. ([#2103])

## Changed

<!-- refactor(), perf(), docs() changes visible to operators go here -->

## Security

<!-- security fixes go here -->
