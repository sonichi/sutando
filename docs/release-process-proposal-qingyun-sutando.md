---
title: Release process proposal — qingyun-sutando's half
date: 2026-05-19 (review-round resolutions integrated 2026-05-20)
tags: [release-process, proposal, plan-only]
author: qingyun-sutando
status: draft — open questions resolved; pending merge
reviewers_integrated: bassilkhilo-ag2 (2026-05-20 06:28), liususan091219 (2026-05-20 11:31), sonichi (sub-agent draft 19:28 + owner-pick 20:30)
related: notes/release-process-proposal-mini.md, docs/release-process.md (not yet written)
---

> **Plan-only document.** This is half of a co-drafted proposal — companion `docs/release-process-proposal-mini.md` covers the *when* + tag conventions; this covers the *what goes in* + migration framework + the joint coupling section.
>
> **Review round integrated 2026-05-20.** All open questions resolved (see end of doc). Lucy's 3 implementation questions addressed in §2.7 (fresh-clone gate scope) and §2.8 (multi-core race + merge-order collision). Engine tag format swept from `engine-vX.Y.Z` → bare `vX.Y.Z` per Chi's owner-pick. Per Chi 2026-05-20 20:30: "Once the RFC text reflects these, it's mergeable."

## Overview — what we're trying to do, why it matters

**What**: define a release process for the Sutando engine (`sonichi/sutando`) — currently install-from-`main` with no versioning, no snapshots, no migration story. Two parts:

1. **Release process** — when to cut a tag, what goes into release notes, who curates the CHANGELOG.
2. **Migration framework** — when a release changes the shape of state on disk (env vars, JSON schemas, workspace layout), existing installs get auto-migrated cleanly on `git pull` instead of breaking silently.

**Why it matters**:

- **Rollback discipline.** Today: "we broke something — what's the last known good state?" Answer: hunt for a commit SHA. After: `git checkout v0.1.0`.
- **Pinning for anyone building on top.** Forks, downstream consumers, sister-node fleets, anyone running their own Sutando — install docs can say "we tested against engine v0.X" and pin reliably. Without versions, install instructions can only reference a moving target (`main`) or a bare commit SHA.
- **Silent breakage prevention.** Recent PRs (#876 env rename, #892 tierMap, #884 multi-core state-dir) each invented their own backward-compat trick. No registry, no startup-time check, no upgrade-path test. The first non-additive change (workspace contract A/B, pending question 2026-05-17 00:40) WILL break some installs. We need migration infra before then.
- **Coord between bots.** Sutando-Mini and qingyun-sutando are both contributing to release plumbing. Without a written RFC we'll diverge.
- **Continuous gate, not annual ritual.** The "non-breaking-state gate" baked into the release process (CI + health + migrations) becomes a per-PR discipline, not just a once-per-tag check.

**What it does NOT try to do**: time-based release cadence, backward migrations (rollback un-migrate), data content re-encoding, lockstep with the product (Sutando.app) version. Those are explicitly deferred.

**Co-author split** (per task-1779222840893): Mini takes *when to cut* + tag conventions; this doc takes *what goes in* + CHANGELOG culture + migration framework + the joint engine↔product coupling section. Both halves consolidate on this PR's branch.

## Motivations

Why now, and why both halves of this proposal exist:

### M1. We need named snapshots of known-good states

Today the install model is `git clone && bash src/startup.sh` — users install at whatever `main` HEAD happens to be. When something breaks, "downgrade to the last good state" means hunting for a commit SHA. Named release tags make rollback a single instruction (`git checkout v0.1.0`) and let install docs pin to a specific snapshot instead of moving with main.

### M2. State-format and contract changes happen often — silently breaking users

Recent contract churn that's already shipped: `SUTANDO_PRIVATE_DIR → SUTANDO_MEMORY_DIR` rename (#876), `tierMap` added to Slack `access.json` (#892), `state/cores/<id>.alive` schema for multi-core (#884). Each PR re-invented its own backward-compat trick (env var alias, default-to-owner if absent, etc.). No central registry, no startup-time enforcement, no test coverage of the "user upgrades through this version" path. The first non-additive change on the horizon (workspace contract A/B, pending-question 2026-05-17 00:40) WILL break installs that just `git pull`. Migration framework before then, not after.

### M3. Pin points for downstream consumers and bundle deploys

Anyone building on top of the engine — forks, sister-node fleets, packaged bundles like Sutando.app, AG2-internal deploys (`sutando.ag2.ai`), commercial integrators — needs a stable identifier for "the engine version I'm running." Without tags, install docs can only reference a moving target (`main`) or a bare commit SHA. Both modes break the "I'm on engine v0.X" question that downstream users need to answer when reporting bugs, pinning environments, or coordinating compatibility.

### M4. Two bots are coordinating — we need a shared spec

Sutando-Mini + qingyun-sutando are both contributing to the release-process design. Without a written RFC, we'll drift to incompatible models in our own implementations. This document is the shared contract for what we both build against.

### M5. The release process is also the migration-test framework

If we have a "non-breaking-state gate" baked into the release process (CI green + health-check green + smoke test the headline feature + **all migrations applied + tested**), then we get a continuous safety net, not just a once-per-release one. Every PR that touches state format is held to "ship a migration" by the same checklist that gates the next release. Discipline lives in the workflow, not in tribal memory.

## Non-goals

Things this proposal explicitly does NOT do, to keep scope small:

- **Time-based cadence** (weekly/monthly releases) — feature-completion is the trigger.
- **Backward migrations** (rolling back v0.4 → v0.3 cleanly) — manual steps in release notes for now.
- **Data re-encoding** (transforming memory file contents). Skip unless real need.
- **Synchronizing engine version with product version 1:1.** They are two release lines.
- **Beta / RC channels** (`v0.4.0-rc.1`). Skip until a real soft-launch need shows up.
- **Automatic CHANGELOG generation purely from PR titles.** We use author-curated entries with `gh release create --generate-notes` as a secondary view, not a primary source.

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
  "engine_version_at_apply": "v0.1.0"
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
- `v0.1.0`: introduces `SUTANDO_MEMORY_DIR`. Code reads `MEMORY_DIR or PRIVATE_DIR` (alias). Migration 0001 rewrites env file.
- `v0.1.1`: code drops the `PRIVATE_DIR` alias. Anyone who skipped 0.1.0's migration is broken — they had one release of warning.

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

Per Chi's owner-pick (§ Open questions §4a), the cut gate is now:

**Necessary conditions:**

1. **`main` is in a working state** — CI green, no known-blocker issues, no half-finished migration on top of last tag.
2. **Something worth tagging** — at least one `feat` or `breaking` entry in `CHANGELOG-PENDING.md` since the last tag.

**At cut time, on the tagging machine:**

3. **All migrations for this version's PRs are present, idempotent, and tested.** ← from this section.
4. Manual smoke-check headline feature.
5. **Migration smoke-test**: run prior-release first → `git pull` to this release → observe `startup.sh` applies migrations cleanly and the rest of startup continues.

**Health-check scope clarification** (responds to Lucy Q3): the gate items above run against the **tagging machine's existing install** (workspace + credentials present), NOT a fresh clone. A fresh clone legitimately fails several health-check items (no `.sutando/env`, no credentials, no `workspace/state/`); requiring it to pass would be incoherent. Fresh-install path has its own first-run checklist (covered separately in `docs/install.md` when written).

### 2.8 Concurrency & ordering (open implementation Qs from Lucy's review)

Raised in Lucy's 2026-05-20 11:31 PT review. Recording the resolutions inline so the framework spec is self-contained.

**Q1 — startup-time runner vs multi-core pool.** Multiple pool cores boot near-simultaneously (#880) and could race the migration runner against the same `state/schema-version.json`. Resolution: **migrations run as a pre-pool step, not per-core.** `src/startup.sh` invokes `run_migrations.py` BEFORE `install-core-pool.sh` bootstraps the launchd cores. Pool cores assume migrations are already applied — if `current` < the migration set baked into their code, they refuse to start (same fail-closed contract as the runner). Single-writer-by-construction, no lock needed.

For sessions started outside `startup.sh` (e.g. a manual `claude` invocation), the runner is also called from the first import of `workspace_default.resolve_workspace()` — sentinel-gated, sees `applied` already populated, exits as no-op. Idempotency makes the extra call free.

**Q2 — migration number allocation under concurrent PRs.** The numeric replay invariant assumes monotonic `NNNN-*.{sh,py}` files. PR author allocates the next number, but two PRs racing for the same number is possible. Resolution: **number is reserved at merge time, not author time.**

- Author opens PR with `NNNN-description.sh` matching the highest existing number + 1 they observe.
- A pre-merge CI check (`tests/migrations/test_no_collision.py`) fails if two open PRs propose the same number, OR if a PR's number isn't `max(main) + 1` at merge attempt.
- On collision: the second PR rebases, renumbers, re-pushes. Cost is bounded — the CI signal fires before the merge button is enabled.
- Replay order is then guaranteed monotonic on `main`, so the runner's "for each N > current, run N" loop has no ambiguity.

This is the same shape as Rails ActiveRecord migrations and Alembic — both ship the collision-on-merge convention.

**Q3 — health-check gate on fresh clone.** Addressed in §2.7 above (fresh clone has its own first-run path; release gate runs against the tagging install).

---

## Joint section: engine ↔ product coupling

**Context**: Chi clarified mid-thread (task-1779222797248) that "v0.3.0 is the product release version, not OSS release." So two release lines, not one.

**Constraint**: Sutando.app product release (Sparkle-driven) and `sonichi/sutando` engine release (git tags) bump independently. They are NOT synced 1:1.

**Coupling point** — needs explicit decision in the consolidated doc:

### Option A: Loose coupling (recommended)

- Engine repo has its own SemVer line — first cut is `v0.1.0`. Bumps when engine features land.
- Sutando.app has its own SemVer line (currently v0.2.11, next `v0.3.0`). Bumps when product releases ship.
- **Coupling**: each Sutando.app release's notes name the engine commit SHA + tag it ships with. e.g.:
  > "Sutando.app v0.3.0 — ships with `sonichi/sutando@v0.1.0` (commit abc1234)"
- Anyone running the bundle who hits a bug can correlate their bundle version → engine state via the notes. Downstream consumers can pin install instructions to an engine tag, not a bare sha.

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

## Open questions for owner — RESOLVED 2026-05-20

All 4 questions resolved in the review round (Bassil cold-review 06:28, Lucy review 11:31, Chi sub-agent draft 19:28, Chi owner-pick 20:30). Recording the decisions inline so the history reads as a single thread.

1. **Engine tag format** → **bare `v0.MINOR.PATCH`** (no `engine-` prefix). All three reviewers converged; product repo (Sutando.app) ships separately under its own tag line, so the namespace collision the `engine-` prefix was defending against doesn't exist (see Joint section, Option A). The doc has been swept to remove every prior `engine-vX.Y.Z` instance.
2. **CHANGELOG location** → **top-of-repo `CHANGELOG.md`** (single file). Bassil's argument: the common reader question is "what changed recently?" and `docs/changelog/` per-release files scatter the answer.
3. **Release curator** → **owner cuts the tag; any bot can draft the release notes**. Mirrors the working PR-review pattern (bots prepare + review, owner merges).
4. **What to cut next** → **option (c)**, with refinement: cut the first release from current main with a manual "no migration needed" note in the release body, then ship the migration framework as the primary payload of the next release. Bassil flagged that the queued PRs are additive/fix-only — there's nothing for a migration runner to do in the cut-from-current-main path. The **workspace contract A/B change** (pending question 2026-05-17 00:40) is the right forcing function for shipping the framework — when that PR is ready it needs `migrations/0002-workspace-contract.{sh,py}` to be safe.

### 4a. "When to cut" gate — Chi's owner-pick (refines Mini's Part 1.1)

Chi's final formulation (2026-05-20 20:30 PT) replaces commit-count thresholds with a **working-state** check:

> A release cut requires `main` to be in a **working state** — CI green, no known-blocker issues that would land in a broken `v0.x.0`, no half-finished migration on top of last tag. AND there's something worth tagging — a `feat` or `breaking` entry in `CHANGELOG-PENDING.md` since the last tag.

Quality gate, not quantity. If the repo stays green for weeks without a feature trigger, that's fine; if it goes red mid-iteration we don't cut even if 100+ commits have landed since last tag. Single dispatch: *is main green & not actively in a half-finished migration?* Yes + something to tag → cut. Otherwise → wait.

This supersedes the "≤50 commits since last tag" ceiling from Chi's earlier sub-agent draft.

### 4b. GPG-signed tags

**Deferred.** Use unsigned `git tag v0.X.Y` for v0.1.x. Tracked in the open-items list (§ "Future hardening" below).

---

## Future hardening (deferred — track separately)

These came up in review but are deliberately out of scope for the first version of the framework. Each gets its own tracking issue when the time comes.

- **Signed tags (`git tag -s v0.X.Y`).** Per Chi 2026-05-20: unsigned for v0.1.x. Future hardening = `gpg.signingkey` config on the tagging machine + workflow lint that rejects unsigned tag pushes.
- **Backward migrations** (`v0.4 → v0.3` rollback). Manual notes in release body until a real downgrade case forces the infra. Most users won't downgrade.
- **Data re-encoding** (transforming memory file contents on schema change). Skip unless a real case appears.
- **PR template `migration:` checklist item** with CI guard that diffs state-file shapes against main and flags PRs missing a migration entry when shapes change. Phase 3 in §2.6.
- **`docs/install.md`** — fresh-install path needs its own first-run checklist (separate from the release-gate health-check scope, see §2.7).
- **`docs/release-process.md`** — promote this RFC + Mini's companion sections into the canonical user-facing doc once both halves are greenlit.

## Next move

Per Chi's 2026-05-20 20:30 owner-pick ("Once the RFC text reflects these, it's mergeable"), the next steps are:

1. **Merge this PR.** All open questions resolved; tag format swept; Lucy's Q1+Q2+Q3 addressed in §2.8 + §2.7.
2. **Open Phase 1 framework PRs** — `migrations/` registry + runner + `schema-version.json` + tests/migrations/ harness. Suggested issues: one for runner+harness, one for migration 0001 (PRIVATE_DIR → MEMORY_DIR backfill as the first recorded migration).
3. **Cut `v0.1.0`** from current main with manual "no migration needed" release note (option (c) per Open question §4) once Phase 1 is in.
4. **Workspace contract A/B PR (pending question 2026-05-17 00:40)** ships migration 0002 as a forcing-function test of the framework.

No code shipped in this RFC. Plan only.
