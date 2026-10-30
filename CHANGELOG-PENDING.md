# CHANGELOG-PENDING

Unreleased changes to be curated into the next `CHANGELOG.md` entry.

**Instructions for contributors:** add a one-line entry under the appropriate section when your PR introduces a user-visible change. PR number in brackets at the end. Entries are curated (not auto-generated) before each release.

Format: `- Brief description of what changed. ([#NNN])`

---

## Added

<!-- feat() PRs go here -->

## Fixed

<!-- fix() PRs go here -->
- task-orphan-check: a task's age is how long it has waited in this inbox (its `local_time:` header), not since the sender sent it, so messages held at the broker while the app was closed are answered on relaunch instead of archived as orphans. Only a `local_time:` above `task:` counts, so a body line cannot make a fresh task from an older writer look orphaned. ([#5929])

## Changed

<!-- refactor(), perf(), docs() changes visible to operators go here -->

## Security

<!-- security fixes go here -->
