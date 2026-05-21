---
title: Release process + migration framework — consolidated RFC
date: 2026-05-19
tags: [release-process, migration, rfc, plan-only]
authors: [Sutando-Mini, qingyun-sutando]
status: draft — open questions resolved 2026-05-20, ready to merge (plan only; nothing implemented)
related: docs/release-process-proposal-mini.md, docs/release-process-proposal-qingyun-sutando.md, docs/release-process.md (not yet written)
---

> **Plan-only document.** Merge of both co-authored halves on PR #906:
> - `docs/release-process-proposal-mini.md` — *when* to cut + tag conventions
> - `docs/release-process-proposal-qingyun-sutando.md` — *what goes in* + CHANGELOG + migration framework + joint engine↔product coupling
>
> Both source files are preserved on this branch as the authorship record. This consolidated doc is what owner + Chi review; if greenlit it promotes to `docs/release-process.md`.

## Overview — what this RFC defines and why

Two intertwined pieces of release infrastructure that don't exist in the engine repo today:

1. **Release process for the engine repo** (`sonichi/sutando`) — currently install-from-`main`, no version snapshots, no tags. After this RFC: tagged feature-driven releases (`v0.1.0`, etc.) with author-curated CHANGELOG, idempotent gates before each tag.
2. **Migration framework** — when a release changes the shape of state on disk (env vars, JSON schemas, workspace layout), existing installs auto-migrate on `git pull` instead of silently breaking. Today every PR re-invents its own backward-compat trick; this proposes a numbered `migrations/NNNN-*.{sh,py}` registry + startup-time runner.

### Why now

- **Rollback discipline.** Today "what's the last good state?" means hunting a commit SHA. After: `git checkout v0.1.0`.
- **Pin points for downstream consumers and bundle deploys.** Forks, sister-node fleets, Sutando.app, AG2-internal deploys (`sutando.ag2.ai`), commercial integrators — anyone building on top needs a stable identifier for "the engine version I'm running."
- **Silent breakage prevention.** Recent PRs (#876 env rename, #892 tierMap, #884 multi-core state-dir) each invented their own backward-compat trick. The first non-additive change (workspace contract A/B, pending question 2026-05-17 00:40) WILL break installs that just `git pull`. We need migration infra before then.
- **Coord between bots.** Sutando-Mini + qingyun-sutando are both contributing; without a written RFC we'll diverge.
- **Continuous gate, not annual ritual.** The "non-breaking-state gate" baked into the release process (CI + health + migrations) becomes a per-PR discipline.

### Non-goals (deferred until real need shows up)

- **Time-based cadence** (weekly/monthly releases) — feature-completion is the trigger.
- **Backward migrations** (rollback un-migrate) — manual steps in release notes if downgrade ever needed.
- **Data re-encoding** (transforming memory file contents).
- **Lockstep with product version.** Engine and Sutando.app bump on independent SemVer lines.
- **Beta / RC channels** (`v0.4.0-rc.1`).
- **Pure auto-generated CHANGELOG from PR titles.** We use author-curated entries; `gh release create --generate-notes` is a secondary view, not the primary source.
- **Release branches** for parallel released lines (`release/0.1.x`). Patch backports deferred until a real customer-pin case forces it.

---

## Part 1: When to cut a release *(Mini's half)*

### 1.1 Trigger: feature-completion, not time

Engine release cadence is driven by feature-completion, not by calendar. A release is cut when *any* of the following has happened since the last tag:

| Trigger | Example from this repo |
|---------|-----------------------|
| Headline feature merged | #874 unified result markers, #880 multi-core pool |
| Breaking contract change | future workspace contract A/B decision, env-var rename |
| Security fix | hypothetical: token leak in logs, auth bypass |
| Migration framework gates a fix | first real migration (Phase 2 of the framework) |
| Pin request from downstream | `sutando.ag2.ai` install docs, Sutando.app bundle |

**Anti-trigger**: do not cut on "it's been N weeks." Time-based cadence forces noisy releases when nothing user-facing landed, and stale releases when something urgent did.

**Floor**: if more than one quarter has gone by AND there are unreleased entries in `CHANGELOG-PENDING.md`, the release curator pings the owner. Soft signal, not a hard rule.

**Precondition — `main` must be in a working state (decided 2026-05-20).** A trigger above is necessary but not sufficient. A release is cut only when `main` is *also*: CI green, carrying no known release-blocker issue that would land in a broken `v0.x.0`, and with no half-finished migration on top of the last tag. This is a quality gate, not a quantity one — if the repo stays green for weeks with no feature trigger, that's fine; if it goes red mid-iteration we don't cut even with 100+ commits since the last tag. Single dispatch: *is `main` green and not mid-migration, and is there a `feat`/`breaking` entry in `CHANGELOG-PENDING.md` since the last tag?* Yes → cut; otherwise → wait.

### 1.2 Who decides "is this commit release-worthy"

**Owner-driven, bots-prepared.** Memory: "No merge authority for bots." Same applies to tags. Bots can stage a release proposal — draft CHANGELOG entry, propose version bump, surface gate-readiness — but owner cuts the tag.

A bot proposes a release by writing `notes/release-proposals/proposed-vX.Y.Z.md` (motivation, version bump rationale, CHANGELOG draft, gate-readiness checklist), then posting a `#dev` notification tagging the owner. Owner either runs `gh release create` or says wait.

### 1.3 Anti-pattern: rapid-fire releases

A release should be **one user-facing thing the readme can name**. Two trivial bug fixes are not a release; they're a CHANGELOG entry under the next real one.

---

## Part 2: Versioning + tag conventions *(Mini's half)*

### 2.1 SemVer (recommended), not CalVer

`vMAJOR.MINOR.PATCH`:

| Bump | When | Example |
|------|------|---------|
| MAJOR | breaking change with no automatic migration; or removal of long-deprecated feature | future "drop python 3.10 support" |
| MINOR | new user-visible capability; new skill; backwards-compatible schema additions | new bridge, new skill, new tool surface |
| PATCH | bug fix on existing capability; security fix; doc-only release | #804 web-client localhost fix |

**Why SemVer over CalVer**: the audience for engine versions is **install-pinners and bundle-deployers**, not end-users. They need "is this safe to upgrade" — SemVer answers directly. CalVer (`2026.05.0`) tells you nothing about breakage. The migration framework's strict numeric ordering (replay 0001, 0002, 0003) maps cleanly onto SemVer; CalVer would require a separate sequence number.

A SemVer 0.x line gives us "all bets off, expect breakage between MINOR" semantics during the engine-shaping phase. We graduate to 1.0.0 when workspace contract + bridge ABI + skill loader feel stable.

### 2.2 Tag namespace: bare `vX.Y.Z`

**Decided 2026-05-20: bare `vX.Y.Z`, no `engine-` prefix.**

An `engine-` prefix was proposed to defend against a future namespace collision if product (Sutando.app) versioning ever shared this repo's `git tag` namespace. That collision doesn't exist — the product ships from its own repo under its own tag line (see Part 7, Option A), so this repo carries a single version line. A bare `vX.Y.Z` is the conventional, lower-friction choice; all reviewers converged on it.

### 2.3 Pre-release suffixes deferred

No `-rc.1`, `-alpha.1` until a real soft-launch need shows up.

---

## Part 3: Tag on main vs release branch *(Mini's half)*

### 3.1 Recommendation: tag on main; no release branch

```
main: A → B → C → D → E → F
                ^         ^
              v0.1.0    v0.2.0
```

We do not patch old releases. If a bug is found in 0.1.0 after 0.2.0 ships, users upgrade. No 0.1.1 backports.

Release branches add maintenance burden that only pays off for parallel released lines — not our shape today. Bundle-deployers (Sutando.app) pin to a tag, not a branch; a tag on main suffices.

### 3.2 Caveat — when to revisit

If a critical bug surfaces on an engine version that has had a successor (e.g. integrator pinned to v0.3.0, v0.4.0 already shipped with the fix but broke something for them), we cut `release/0.3.x` lazily at that point. Pre-emptive release branching is YAGNI.

### 3.3 What if `main` is not tag-ready

1. **Preferred**: wait and stabilize.
2. **Acceptable**: tag at an earlier commit on main. If main is at SHA F but only A→D are release-ready, tag at D.

We do not branch around it.

---

## Part 4: Git tag + GitHub Release + gates *(Mini's half)*

### 4.1 Mechanically: both

```bash
git tag -a vX.Y.Z -m "engine vX.Y.Z — <one-line summary>"
git push origin vX.Y.Z

gh release create vX.Y.Z \
  --title "engine vX.Y.Z — <one-line summary>" \
  --notes-file release-notes-vX.Y.Z.md \
  --generate-notes
```

- **Git tag** is the cryptographic anchor (annotated, with curator's name + date + one-line summary). Survives even if GitHub goes away. Bundle-deployers reference this.
- **GitHub Release** is the human-readable narrative view. CHANGELOG.md is the source of truth; the GitHub Release page renders the same content for someone arriving from the project sidebar.
- `--generate-notes` adds a "PRs since last tag" view as a **completeness check** (did the author-curated CHANGELOG miss anything?), not as the primary narrative.

### 4.2 Hard gates before tag-push

Tag refused unless all of:

1. **CI green** on the tagged SHA.
2. **Health-check green** (`python3 src/health-check.py`) on a fresh clone of the tagged SHA.
3. **Smoke-check the headline feature** (manual; release proposal names the procedure).
4. **All migrations for this version are present, idempotent, and tested** — see Part 6.
5. **Migration smoke-test**: run prior release first → `git pull` to this release → observe `startup.sh` applies migrations cleanly.

Soft gates (warn, don't block):

- Open issues tagged `release-blocker` on this version's milestone.
- `CHANGELOG-PENDING.md` shouldn't be empty (otherwise: why are we cutting?).

### 4.3 Signed tag

`git tag -a -s vX.Y.Z` is the right default if owner has GPG configured. Bot-prepared tags should never push `-s`; only owner pushes signed tags so the cryptographic anchor is owner-attested.

---

## Part 5: What goes in a release + CHANGELOG culture *(qingyun-sutando's half)*

### 5.1 Categories

Five buckets. Every release-worthy change maps to exactly one:

| Category | What | Example |
|----------|------|---------|
| **feat** | New user-visible capability | #874, #880 |
| **fix** | Bug fix on existing capability | #804 |
| **breaking** | Contract change requiring user action | future workspace contract A/B |
| **docs** | Docs / SKILL.md / pending-questions | #771, #878 |
| **skill** | New skill or skill schema bump | any new `skills/` entry |

**Why not full Conventional Commits** (`refactor`, `chore`, `style`, `test`): release readers care about user-impacting categories. Refactor/chore/test belong in PR bodies, not release notes.

**Special-case `breaking`**: must carry a migration step (see Part 6). No `breaking` without a corresponding migration.

### 5.2 Who curates entries

**Author-curated, release-curator-pruned.** Each PR's author adds a one-line entry to `CHANGELOG-PENDING.md` in their own PR's diff:

```markdown
## Unreleased
- feat(#874): bridges share `parse_markers()` — slack + telegram wired
- fix(#804): web-client poll-presenter uses same-origin endpoint
- skill(#771): whatsapp `wacli` formalized as SKILL.md
```

At release time the curator moves the `Unreleased` section under the version header, trims wording for consistency, and runs `gh release create --generate-notes` as a completeness check.

### 5.3 Draft-in-PR vs draft-at-release

**Both, at different layers:**

- **Draft-in-PR** — required for `breaking` and `feat`. Add a `## CHANGELOG entry` section to PR body so reviewers can sanity-check the user-facing description before merge.
- **Draft-at-release** — editorial pass by curator. Trim. Group by category. Add 2-3 sentence narrative header.

### 5.4 Link convention

Every CHANGELOG entry ends with `(#NNN)` linking to the merging PR. Non-negotiable — a release reader who hits a bug needs grep-able "what changed → which PR" in one step. GitHub auto-renders `#NNN` as a hyperlink.

For closing issues: `closes #NNN` in PR body (auto-close magic phrase per `feedback_auto_close_magic_phrase.md`). CHANGELOG only names the PR; issue is reachable from the PR.

---

## Part 6: Migration framework *(qingyun-sutando's half — load-bearing)*

### 6.1 Why this exists

Owner flagged in DM: "the codebase may have a dependency on a certain format of the runtime and user data. We need to have built-in migration when the contract are broken."

What exists today (none of it solves this):
- `src/migrate.sh` — cross-Mac bundle. **NOT** a within-machine schema migration.
- Ad-hoc patterns: `SUTANDO_PRIVATE_DIR` deprecation alias (#876), `tierMap` defaults-to-owner if absent (#892). Each PR re-invents backward-compat. No central registry.
- No `migrations/` dir, no schema-version file, no startup-time runner.

Recent contract changes have been **additive** — that's why we haven't been bitten. The workspace contract A/B is the first non-additive change on the horizon.

### 6.2 Minimal framework

**Layout:**

```
migrations/
  README.md
  0001-rename-private-dir-to-memory-dir.sh
  0002-add-tier-map-to-discord-access.py
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

Fresh install = `{"applied": [], "current": 0}` (or absent — treat as that).

**Per-migration script contract** (example):

```bash
#!/bin/bash
# migrations/0001-rename-private-dir-to-memory-dir.sh
# Originating PR: #876
set -e
if grep -q "^SUTANDO_PRIVATE_DIR=" "$HOME/.sutando/env" 2>/dev/null; then
    sed -i.bak 's/^SUTANDO_PRIVATE_DIR=/SUTANDO_MEMORY_DIR=/' "$HOME/.sutando/env"
    echo "  migration 0001: renamed SUTANDO_PRIVATE_DIR → SUTANDO_MEMORY_DIR"
else
    echo "  migration 0001: nothing to rename (already at v1)"
fi
```

**Runner** invoked by `src/startup.sh` **before everything else**:

```bash
python3 src/run_migrations.py || {
    echo "FATAL: migration failed. Refusing to start bridges/watchers in a half-migrated state."
    exit 1
}
```

`src/run_migrations.py`:
1. Read `state/schema-version.json` (default to `current=0` if absent).
2. Walk `migrations/` numerically. Run scripts where `N > current`. On exit 0, append N to `applied` + bump `current` atomically.
3. On any failure: stop, log loudly, return non-zero. **Refuses to start the rest.**

### 6.3 Backward-compat overlap rule

For env vars + config keys: the release that ships the new shape keeps reading the old shape too (alias / fallback). The migration writes the new shape. The **next** release removes the old-shape reader.

Timeline example:
- `v0.1.0`: introduces `SUTANDO_MEMORY_DIR`. Code reads `MEMORY_DIR or PRIVATE_DIR` (alias). Migration 0001 rewrites env.
- `v0.1.1`: code drops the alias. Anyone who skipped 0.1.0's migration is broken — one release of warning.

### 6.4 Per-migration tests

Each migration ships with a tempdir test:

```python
def test_renames_old_var():
    with tempdir() as d:
        env = d / ".sutando" / "env"
        env.parent.mkdir(parents=True)
        env.write_text("SUTANDO_PRIVATE_DIR=/foo\nOTHER=bar\n")
        run_migration("0001-rename-private-dir-to-memory-dir.sh", home=d)
        assert "SUTANDO_MEMORY_DIR=/foo" in env.read_text()
        assert "SUTANDO_PRIVATE_DIR" not in env.read_text()
        assert "OTHER=bar" in env.read_text()

def test_idempotent():
    with tempdir() as d:
        env = d / ".sutando" / "env"
        env.parent.mkdir(parents=True)
        env.write_text("SUTANDO_MEMORY_DIR=/foo\n")
        run_migration("0001-rename-private-dir-to-memory-dir.sh", home=d)
        assert env.read_text() == "SUTANDO_MEMORY_DIR=/foo\n"  # unchanged
```

### 6.5 Phasing

- **Phase 1** (1-2 PRs): build runner + schema-version. Backfill `0001-rename-private-dir-to-memory-dir.sh` as the formalized version of #876's alias pattern.
- **Phase 2** (next breaking PR): first **real** migration. Workspace contract A/B (pending question 2026-05-17 00:40) writes `0002-*.sh`.
- **Phase 3** (later): PR template checklist + CI guard — "Does this PR change state format? If yes, attach a migration."

### 6.6 Tied into the release-gate

Part 4.2's gate #4 + #5 are this framework's enforcement points at tag-time. PR-time checks (Phase 3) are the early gate. Both required; redundant on purpose.

---

## Part 7: Engine ↔ product coupling *(joint section)*

Chi clarified mid-thread: `v0.3.0` is the **product** (Sutando.app bundle) version, **not** the OSS engine version. Two release lines, not one.

### Option A: Loose coupling (recommended)

- Engine has its own SemVer line (`v0.1.0` first cut). Bumps when engine features land.
- Sutando.app has its own SemVer line (currently v0.2.11, next v0.3.0). Bumps when product releases ship.
- **Coupling**: each Sutando.app release's notes name the engine commit SHA + tag it ships with:
  > "Sutando.app v0.3.0 — ships with `sonichi/sutando@v0.1.0` (commit abc1234)"
- Anyone running the bundle who hits a bug correlates bundle version → engine state via the notes. Downstream consumers pin install instructions to an engine tag.

### Option B: Lockstep

- Engine and product share the same number — `v0.3.0` in both repos always.
- Product release triggers an engine tag at the same name.
- Simpler to communicate; forces engine bumps whenever product cuts, even if engine didn't change. Conflates two rhythms.

### Recommendation: **Option A**

Product cadence is owner-driven (Sparkle deploys) and may be faster than engine cadence (engine may sit on the same code for 3 product releases). Conflating puts pressure to bump engine on every product cut, devaluing engine versions.

Coupling via release notes naming the engine SHA is the well-established pattern (Chromium versions ↔ Chrome versions).

---

## Part 8: What to cut next — option (c), agreed

Both halves landed on **(c)**: cut `v0.1.0` NOW with manual migration steps in release notes; build Phase 1 framework as `v0.1.1`.

Reasoning (combined):

- **(a) Cut without framework** is safe *for the current cut specifically* (since main is additive-only since #876's alias), but the precedent normalizes "additive is always safe to ship without framework" — and the next non-additive PR could slip through.
- **(b) Build framework first** is correct in principle but adds 1-2 PRs of latency. Too slow if there's pin demand (downstream consumers, Sutando.app bundle).
- **(c) Cut now, framework next** gives us a named v0.1.0 today and forces the framework to be the headline of v0.1.1. The v0.1.0 release notes honestly say "this is the as-built state; the migration framework is itself the v0.1.1 feature."

**Concrete v0.1.0 plan:**
- Tag: `v0.1.0`
- Date: as soon as RFC is greenlit + `CHANGELOG-PENDING.md` is consolidated from current main's PR titles
- CHANGELOG narrative: "Initial engine snapshot. All changes since project inception are batch-recorded. No automatic migrations — install instructions name manual steps where needed."
- Manual migration list in release notes: one step today (the SUTANDO_PRIVATE_DIR → SUTANDO_MEMORY_DIR rename, with the #876 alias meaning most installs don't need to act).

**Then v0.1.1**: ship Phase 1 framework. First real applied migration. From then on, every breaking PR ships with a numbered migration.

---

## Coupling table (no-double-coverage check)

Where the two halves intersect, both authors landed on the same answer:

| This RFC says | Source: Mini | Source: qingyun-sutando | Meets at |
|---------------|--------------|------------------------|----------|
| Hard gate #4: migrations present + tested at tag time | Part 4.2 | Part 6.5 phase 3 | Per-PR + release-time, redundant on purpose |
| Hard gate #5: migration smoke-test | Part 4.2 | Part 6.6 | Same procedure, two enforcement points |
| `feat` doesn't auto-trigger a tag | Part 1.1 | Part 5.1 | A `feat` entry without a tag arms the "tag drift" floor |
| SemVer, bare `vX.Y.Z` (no `engine-` prefix) | Parts 2.1 + 2.2 | Part 6.2 schema-version example uses `v0.1.0` | Aligned |
| Cut v0.1.0 now, framework as v0.1.1 | Part 5 | Section 2.5 phase 1 | Same plan, this RFC phases onto version axis |

No gap. No contradiction.

---

## Consolidated decisions (resolved 2026-05-20)

All open questions were resolved in the review round (Bassil cold-review, Lucy review, Chi sub-agent draft + owner-pick). Recorded here so the doc is self-contained; promotion to `docs/release-process.md` is no longer gated on owner input.

1. **Tag-name prefix** → **bare `vX.Y.Z`**, no `engine-` prefix. Product (Sutando.app) ships from its own repo under its own tag line, so the namespace collision the prefix defended against doesn't exist. See §2.2.
2. **CHANGELOG location** → **top-of-repo `CHANGELOG.md`** (single file). Per-release files in `docs/changelog/` scatter the common "what changed recently?" answer.
3. **Release curator role** → **owner cuts the tag; any bot may draft the release notes.** Mirrors the working PR pattern (bots prepare + review, owner merges).
4. **Signed tags by default** → **deferred.** Unsigned `git tag vX.Y.Z` for the v0.1.x line; GPG-signing is future hardening.
5. **First cut timing** → **option (c)**: cut `v0.1.0` from current `main` with a manual "no migration needed" release note, then ship the migration framework as the headline of `v0.1.1`. The workspace-contract A/B PR is the forcing function for the framework's first real migration.

---

## Next move

Open questions are resolved (see above) and the doc reflects every owner-pick — it's ready to merge. On merge:

1. Promote this doc to `docs/release-process.md`.
2. Open issues:
   - **"Cut v0.1.0"** (one-shot, owner-driven — from current `main`, with a manual "no migration needed" release note).
   - **"Phase 1 migration framework"** (1-2 PRs sized; lives in `migrations/` + `src/run_migrations.py` + tests — headline of `v0.1.1`).
   - **"PR template + CI guard for state-format changes"** (Phase 3, deferred).

**No code shipped.** Plan-only RFC.
