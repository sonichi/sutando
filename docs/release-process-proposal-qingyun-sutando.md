---
title: Release process proposal — qingyun-sutando's half
date: 2026-05-19
tags: [release-process, proposal, plan-only]
author: qingyun-sutando
status: draft (plan only; nothing implemented)
related: notes/release-process-proposal-mini.md, docs/release-process.md (not yet written)
---

> **Plan-only document.** Owner (Chi) explicitly said "Plan and make proposals first. Do not implement anything yet." This is half of a co-drafted proposal — Mini covers the *when* + tag conventions; this covers the *what goes in* + migration framework + the joint coupling section.

## Scope of this half

Per the split agreed with Sutando-Mini in #dev (task-1779222840893, 2026-05-19 13:34 PT):

- **Mini covers**: *when* to cut a release, SemVer vs CalVer, tag-on-main vs release-branch, GitHub Release vs git tag only.
- **This doc covers**: *what goes in* a release, CHANGELOG culture, **migration framework**, plus the joint *engine ↔ product coupling* section.

After both halves land, merge into `notes/release-process-consolidated.md` for Chi + Qingyun review. If they greenlight, promote to `docs/release-process.md`.

---

## Part 1: What goes in a release (CHANGELOG culture)

### 1.1 Categories

Five buckets. Every release-worthy change maps to exactly one:

| Category | What | Example from this repo |
|----------|------|------------------------|
| **feat** | New user-visible capability | #874 unified result markers, #880 multi-core pool |
| **fix** | Bug fix on existing capability | #804 web-client localhost:7877 → same-origin |
| **breaking** | Contract change requiring user action | future workspace contract A/B decision |
| **docs** | Docs / SKILL.md / pending-questions | #771 whatsapp SKILL.md, #878 host-CLI bindings doc |
| **skill** | New skill or skill schema bump | (any new entry under `skills/`) |

**Why these and not Conventional Commits' full set** (`refactor`, `chore`, `style`, `test`, etc.): a release reader cares about user-impacting categories. Refactor/chore/test belong in the PR body, not the release notes.

**Special-case `breaking`**: must also carry the migration step (see Part 2). No `breaking` without a migration entry.

### 1.2 Who curates entries

**Author-curated, release-curator-pruned.** Each PR's author adds a one-line entry to a top-of-file `CHANGELOG-PENDING.md` section in the PR's own diff:

```markdown
## Unreleased
- feat(#874): bridges share `parse_markers()` — slack + telegram wired, discord + task-bridge deferred to #896
- fix(#804): web-client poll-presenter uses same-origin endpoint
- skill(#771): whatsapp `wacli` formalized as SKILL.md
```

At release time, the **release curator** (whoever cuts the tag) moves the `Unreleased` section under the new version header in `CHANGELOG.md`, trims wording for consistency, and runs `gh release create vX.Y.Z --generate-notes` to also capture the PR-titles list as a secondary view.

**Why author-curated**: by the time a PR merges, the author has the freshest mental model of what's user-impacting. Asking the release curator to backfill 30 entries from PR titles is error-prone (titles are written for review, not for users).

**Why release-curator-pruned**: PR authors will sometimes inflate. The curator catches that.

### 1.3 Draft-in-PR vs draft-at-release

**Both — at different layers:**

- **Draft-in-PR** — required for `breaking` and `feat`. Add a `## CHANGELOG entry` section to the PR body so reviewers can sanity-check the user-facing description before merge.
- **Draft-at-release** — additional editorial pass by the release curator. Trim. Group by category. Add a 2-3 sentence narrative header for the release.

### 1.4 Link conventions

Every CHANGELOG entry ends with `(#NNN)` linking to the merging PR. This is non-negotiable — a release reader who hits a bug needs to grep "what changed" → "which PR" in one step. GitHub's auto-rendering turns `#NNN` into a hyperlink.

For closing issues: still use `closes #NNN` in PR body (auto-close magic phrase, per memory `feedback_auto_close_magic_phrase.md`). The CHANGELOG entry only needs the PR number; the issue is reachable from the PR.

---

## Part 2: Migration framework

This is the load-bearing half. Owner flagged it in DM: "the codebase may have a dependency on a certain format of the runtime and user data. We need to have built-in migration when the contract are broken."

### 2.1 What we have today (none of it solves this)

- `src/migrate.sh` — cross-Mac bundle. **NOT** a within-machine schema migration.
- Ad-hoc patterns: `SUTANDO_PRIVATE_DIR` deprecation alias (#876), `tierMap` defaults-to-owner if absent (#892). Each PR re-invented backward-compat. No central registry.
- **No `migrations/` dir, no schema-version file, no startup-time migration runner.**

Recent contract changes have all been **additive** (new env var, new tier field, new state subdir) — that's why we haven't been bitten. The workspace contract A/B decision (pending question 05-17 00:40) is the first non-additive change on the horizon. We need this framework before that lands.

### 2.2 Minimal framework

**Directory layout:**

```
migrations/
  README.md
  0001-rename-private-dir-to-memory-dir.sh
  0002-add-tier-map-to-discord-access.py
  0003-workspace-contract-state-files.sh
  ...
tests/migrations/
  test_0001_rename_private_dir.py
  test_0002_tier_map_default.py
  ...
state/
  schema-version.json
```

**Schema-version file** (`state/schema-version.json`):

```json
{
  "applied": [1, 2],
  "current": 2,
  "engine_version_at_apply": "engine-v0.1.0"
}
```

Single source of truth for "what's run on this install." Fresh install = `{"applied": [], "current": 0}` (or file absent — treat as that).

**Per-migration script contract:**

```bash
#!/bin/bash
# migrations/0001-rename-private-dir-to-memory-dir.sh
# Originating PR: #876
# Rename: SUTANDO_PRIVATE_DIR env var → SUTANDO_MEMORY_DIR
set -e

# Idempotency check — if both vars are unset, or only the new one is set, exit 0
# (already at the post-migration state).
if grep -q "^SUTANDO_PRIVATE_DIR=" "$HOME/.sutando/env" 2>/dev/null; then
    sed -i.bak 's/^SUTANDO_PRIVATE_DIR=/SUTANDO_MEMORY_DIR=/' "$HOME/.sutando/env"
    echo "  migration 0001: renamed SUTANDO_PRIVATE_DIR → SUTANDO_MEMORY_DIR in ~/.sutando/env"
else
    echo "  migration 0001: nothing to rename (already at v1 or fresh install)"
fi
```

**Runner** invoked by `src/startup.sh` **before everything else**:

```bash
# In src/startup.sh, near the top (before bridges, watchers, etc.):
python3 src/run_migrations.py || {
    echo "FATAL: migration failed. Refusing to start bridges/watchers in a half-migrated state."
    exit 1
}
```

Where `src/run_migrations.py`:
1. Reads `state/schema-version.json` (defaults to `{"applied": [], "current": 0}` if missing).
2. Walks `migrations/` numerically — files matching `NNNN-*.{sh,py}`.
3. For each N > `current`, run the script. On exit 0, append N to `applied` and bump `current` atomically (write to tmp + rename).
4. On any failure: stop, log loudly with the script path + exit code + stderr tail. **Return non-zero so startup.sh refuses to start the rest.**

### 2.3 Backward-compat overlap rule (formalize an existing pattern)

For env vars and config keys, the **release that ships the new shape** keeps reading the old shape too (alias / fallback). The migration writes the new shape. The **next release** removes the old-shape reader.

Example timeline:
- `engine-v0.1.0`: introduces `SUTANDO_MEMORY_DIR`. Code reads `MEMORY_DIR or PRIVATE_DIR` (alias). Migration 0001 rewrites env file.
- `engine-v0.1.1`: code drops the `PRIVATE_DIR` alias. Anyone who skipped 0.1.0's migration is broken — they had one release of warning.

Users have **one release of safety to upgrade through**, not over. Skip-version users (going 0.0 → 0.2 in one jump) still work because the runner replays migrations 1 then 2 in order; the alias-window for any given var is one release.

### 2.4 Per-migration tests

Each migration ships with a tempdir-based test:

```python
# tests/migrations/test_0001_rename_private_dir.py
def test_renames_old_var():
    with tempdir() as d:
        env_file = d / ".sutando" / "env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("SUTANDO_PRIVATE_DIR=/foo\nOTHER_VAR=bar\n")
        run_migration("0001-rename-private-dir-to-memory-dir.sh", home=d)
        assert "SUTANDO_MEMORY_DIR=/foo" in env_file.read_text()
        assert "SUTANDO_PRIVATE_DIR" not in env_file.read_text()
        assert "OTHER_VAR=bar" in env_file.read_text()

def test_idempotent_when_already_renamed():
    with tempdir() as d:
        env_file = d / ".sutando" / "env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text("SUTANDO_MEMORY_DIR=/foo\n")
        run_migration("0001-rename-private-dir-to-memory-dir.sh", home=d)
        assert env_file.read_text() == "SUTANDO_MEMORY_DIR=/foo\n"  # unchanged
```

Catches: forgot to handle case X, migration script crashes on missing input file, idempotency broken.

### 2.5 Out of scope (defer until needed)

- **Backward migrations** (rolling back v0.4 → v0.3). Most users won't downgrade. If they need to, manual steps in the release notes.
- **Data re-encoding** of memory file content. Skip unless real need.
- **Multi-version skip handling**: runner handles this naturally (replays N=1,2,3 in order even if user jumps from 0 → 3). No special code needed.

### 2.6 Phasing

- **Phase 1** (1-2 PRs): build the runner + schema-version file. Backfill migration 0001 as the `PRIVATE_DIR → MEMORY_DIR` rename (currently relies on the alias-deprecation from #876 — the migration is mostly a no-op recording "you're at version 1" for installs that already followed the alias path).
- **Phase 2** (next breaking PR): the first real migration is the workspace contract A/B decision (pending question 05-17 00:40). Author of THAT PR writes `0002-workspace-contract.sh`.
- **Phase 3** (later): PR template checklist item — "Does this PR change state format? If yes, attach a migration." CI guard that diffs state-file shapes against main and flags PRs missing a migration entry.

### 2.7 Tied back into the release process

The "non-breaking state gate" from Mini's half becomes:

1. CI green.
2. Health-check all-green.
3. **All migrations for this version's PRs are present, idempotent, and tested.** ← from this section.
4. Manual smoke-check headline feature.
5. **Migration smoke-test**: run prior-release first → `git pull` to this release → observe `startup.sh` applies migrations cleanly and the rest of startup continues.

---

## Joint section: engine ↔ product coupling

**Context**: Chi clarified mid-thread (task-1779222797248) that "v0.3.0 is the product release version, not OSS release." So two release lines, not one.

**Constraint**: Sutando.app product release (Sparkle-driven) and `sonichi/sutando` engine release (git tags) bump independently. They are NOT synced 1:1.

**Coupling point** — needs explicit decision in the consolidated doc:

### Option A: Loose coupling (recommended)

- Engine repo has its own SemVer line — first cut is `engine-v0.1.0`. Bumps when engine features land.
- Sutando.app has its own SemVer line (currently v0.2.11, next `v0.3.0`). Bumps when product releases ship.
- **Coupling**: each Sutando.app release's notes name the engine commit SHA + tag it ships with. e.g.:
  > "Sutando.app v0.3.0 — ships with `sonichi/sutando@engine-v0.1.0` (commit abc1234)"
- Customer who hits a bug correlates bundle version → engine state via the notes. Commercial deploys (`sutando.ag2.ai`) pin install instructions to an engine tag, not a bare sha.

### Option B: Lockstep

- Engine and product share the same number — `v0.3.0` in both repos always.
- Product release triggers an engine tag at the same name.
- Simpler to communicate ("you're on v0.3.0") but forces engine bumps whenever product cuts a release, even if engine didn't change. Conflates two different rhythms.

### Recommendation

**Option A.** Reasons:
- Product cadence is owner-driven (Sparkle deploys) and may be faster than engine cadence (engine may sit on the same code for 3 product releases).
- Conflating them puts pressure to bump engine version on every product cut, devaluing engine versions.
- The "coupling via release notes naming the engine SHA" pattern is well-established (e.g. Chromium versions ↔ Chrome versions).

### Open for Mini's input

Mini may have a different read since they own the *when to cut* half. Worth converging before consolidation.

---

## Open questions for owner

These need explicit owner direction before consolidation:

1. **Engine versioning starts at?** I've been writing `engine-v0.1.0` as the first cut. Alternative: bare `v0.1.0` in the engine repo namespace (less Tag-name verbose). Owner picks.
2. **CHANGELOG location** — top-of-repo `CHANGELOG.md` (traditional) or `docs/changelog/` per-release files (more granular but more files)? I lean traditional `CHANGELOG.md`.
3. **Who is the release curator** — owner only, or any of the bots can curate with owner approval at tag-time? Memory `bot2bot conventions` says "No merge authority for bots" — same constraint applies to releases. Bots can prepare CHANGELOG drafts, owner cuts the tag.
4. **What to cut next** — three options:
   - (a) Cut `engine-v0.1.0` from current main without migration framework. Risky if multi-core changes aren't strictly additive.
   - (b) Build Phase 1 migration framework first, then cut `engine-v0.1.0` with migration 0001 as the first applied step. Cleaner, +1-2 PRs latency.
   - (c) Cut `engine-v0.1.0` NOW with manual migration steps in release notes, build framework as `engine-v0.1.1`. Pragmatic.

   I lean **(c)** for pragmatism. Confirmed in #dev. Mini may have a different view.

---

## Next move

1. Mini drafts `notes/release-process-proposal-mini.md` (when to cut + tag conventions).
2. Merge into `notes/release-process-consolidated.md`.
3. Chi + Qingyun review.
4. If greenlight, promote to `docs/release-process.md` + open issues for Phase 1 framework work.

No code shipped this turn. Plan only.
