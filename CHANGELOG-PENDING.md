# CHANGELOG-PENDING

Unreleased changes to be curated into the next `CHANGELOG.md` entry.

**Instructions for contributors:** add a one-line entry under the appropriate section when your PR introduces a user-visible change. PR number in brackets at the end. Entries are curated (not auto-generated) before each release.

Format: `- Brief description of what changed. ([#NNN])`

---

## Added

<!-- feat() PRs go here -->

## Fixed

<!-- fix() PRs go here -->
- start-cli.sh no longer adopts (or restarts/kills) a coexisting install's `sutando-core` claude: core-process matching is scoped to the launcher's own tmux socket via exec-time env markers (`TMUX=`, `SUTANDO_TMUX_SOCKET=`) with a parent-tmux-server fallback, and a refused adoption now names the foreign pid instead of silently exiting 0. ([#2884])

## Changed

<!-- refactor(), perf(), docs() changes visible to operators go here -->

## Security

<!-- security fixes go here -->
