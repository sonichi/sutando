---
title: Release process proposal — Mini's half
date: 2026-05-19
tags: [release-process, proposal, plan-only]
author: Sutando-Mini
status: draft (plan only; nothing implemented)
related: docs/release-process-proposal-qingyun-sutando.md, docs/release-process.md (not yet written)
---

> **Plan-only document.** Owner (Chi) explicitly said "Plan and make proposals first. Do not implement anything yet." This is half of a co-drafted proposal — qingyun-sutando covers *what goes in* + migration framework + the joint coupling section; this covers the *when* + tag conventions surface.

## Overview — what this half answers

Three concrete questions:

1. **When** is a commit on `main` release-worthy? What is the trigger?
2. **What does a tag look like** — SemVer vs CalVer, tag namespace, where the tag lives (main vs release branch), and whether we cut a GitHub Release on top or stop at the git tag.
3. **Who pushes the tag** and what gates must pass first.

Both halves of this RFC consolidate into `notes/release-process-consolidated.md` for owner review, then promote to `docs/release-process.md` if greenlit.

## Scope of this half

Per the split agreed in #dev (task-1779222840893, 2026-05-19 13:34 PT):

- **This doc covers**: when to cut, SemVer vs CalVer, tag-on-main vs release-branch, GitHub Release vs git tag only.
- **qingyun-sutando covers**: what goes in a release, CHANGELOG culture, migration framework, joint engine↔product coupling.

The two halves are deliberately couplable but not duplicative — the "non-breaking-state gate" referenced in section 4.2 of this doc maps directly onto qingyun-sutando's section 2.7 (migration smoke-test) and section 1 (CHANGELOG completeness). Wiring is explicit, not inferred.

---

## Part 1: When to cut a release

### 1.1 The trigger is feature-completion, not time

Engine release cadence is **driven by feature-completion**, not by a calendar. A release is cut when *any* of the following has happened since the last tag:

| Trigger | Example from this repo |
|---------|-----------------------|
| **Headline feature merged** | #874 unified result markers, #880 multi-core pool, #876 SUTANDO_MEMORY_DIR rename |
| **Breaking contract change** | future workspace contract A/B decision, env-var rename, JSON schema bump |
| **Security fix** | hypothetical: token leak in logs, auth bypass |
| **Migration framework gates a fix** | qingyun's section 2.6 phase 2 — first real migration is the workspace contract A/B |
| **Commercial pin request** | `sutando.ag2.ai` install docs need a name to pin to |
| **Bundle release request** | Sutando.app v0.3.0 needs an engine SHA + tag to ride on |

**Anti-trigger**: do NOT cut on "it's been N weeks." Time-based cadence forces noisy releases when nothing user-facing landed, and stale releases when something urgent did.

**Floor**: if more than one calendar quarter has gone by AND there are unreleased entries in `CHANGELOG-PENDING.md`, the release curator should ping the owner. Not a hard rule — a forcing-function for the "tag drift" failure mode.

### 1.2 Who decides "is this commit release-worthy?"

**Owner-driven, bots-prepared.** Memory says "No merge authority for bots." Same applies to tags: bots can stage a release proposal — draft CHANGELOG entry, propose version bump, surface gate-readiness — but the owner cuts the tag.

A bot proposes a release by:

1. Writing `notes/release-proposals/proposed-vX.Y.Z.md` with: motivation, version bump rationale, CHANGELOG draft, gate-readiness checklist.
2. Posting a notification to `#dev` (Discord) tagging the owner.
3. Waiting. Owner either says go (and runs `gh release create`) or says wait.

This keeps the "decision authority" frictionless when owner is around but allows the engine to accumulate release-readiness signals when they're not. The proposal file is human-readable so owner doesn't have to mentally re-derive whether a tag is justified.

### 1.3 Anti-pattern: rapid-fire releases

Memory feedback `feedback_pr_restraint.md` applies one tier up: don't tag for the sake of tagging. A release should be **one user-facing thing the readme can name**. Two trivial bug-fixes are not a release; they're a CHANGELOG entry under the next real release.

---

## Part 2: SemVer vs CalVer

### 2.1 Recommendation: SemVer

`vMAJOR.MINOR.PATCH` (matches qingyun-sutando's `v0.1.0` convention).

| Rule | Bump | Example |
|------|------|---------|
| MAJOR | breaking contract change with no automatic migration; or removal of long-deprecated feature | future "drop python 3.10 support" |
| MINOR | new user-visible capability; new skill; backwards-compatible schema additions | new bridge (Slack, WhatsApp), new skill, new tool surface |
| PATCH | bug fix on existing capability; security fix; doc-only release | #804 web-client localhost fix, telemetry corrections |

**Why SemVer over CalVer:**

- The audience for engine versions is **install-pinners and bundle-deployers**, not end-users. They care about "is this a safe upgrade" more than "when was this cut." SemVer answers the first question directly.
- CalVer (e.g. `2026.05.0`) tells you nothing about whether a bump will break your install. Useful for OS distros where time-of-cut is the user-visible signal (Ubuntu 24.04). Not useful here.
- A SemVer 0.x line gives us "all bets off, expect breakage between MINOR" semantics for free during the engine-shaping phase, which is exactly where we are. We graduate to 1.0.0 when the workspace contract, bridge ABI, and skill loader feel stable.
- qingyun-sutando's migration framework (section 2.3 "Backward-compat overlap rule") relies on a strict version ordering — replay 0001, 0002, 0003. SemVer's numeric ordering maps cleanly. CalVer requires a separate migration sequence number, an extra concept for no benefit.

**Pre-release suffixes (`-rc.1`, `-alpha.1`)**: deferred per qingyun-sutando's non-goals. We add when a real soft-launch need shows up, not before.

### 2.2 Tag namespace: bare `vX.Y.Z`

**Resolved 2026-05-20: bare `vX.Y.Z`, no prefix.** (This half originally recommended an `engine-` prefix; the review round overrode it.)

The prefix was proposed to defend against a future namespace collision if product (Sutando.app) versioning ever shared this repo's `git tag` namespace. The decision: that collision doesn't materialize — the product ships from its own repo under its own tag line — so this repo carries one version line and a bare `vX.Y.Z` is the conventional, lower-friction choice. See the consolidated doc §2.2.

---

## Part 3: Tag on main vs release branch

### 3.1 Recommendation: tag on main; no release branch

```
main: A -> B -> C -> D -> E -> F
                    ^         ^
                  v0.1.0    v0.2.0
```

The tag is just a named pointer at a commit on `main`. No `release/0.1.x` branch is cut.

**Why no release branch:**

- We do not patch old releases. If a bug is found in 0.1.0 after 0.2.0 ships, users upgrade to 0.2.0; there is no 0.1.1 patch backport. This is correct for a fast-moving engine with a small install base.
- Release branches add a maintenance burden (cherry-picks, divergent CI, "which branch is canonical?") that pays off only when supporting parallel released lines. Not our shape today.
- Bundle-deployers (Sutando.app) pin to a specific tag, not a branch — so `v0.1.0` as a tag on main is sufficient for their needs.

**Caveat — when to revisit**: if Sutando.app ever needs a critical patch on an engine version that has already had a successor (commercial customer pinned to v0.3.0, but v0.4.0 has shipped and broke something they need), we cut `release/0.3.x` lazily at that point. Pre-emptive release branching is YAGNI.

### 3.2 What if `main` is in a "not-quite-ready" state at tag time?

Two options, in order of preference:

1. **Wait and stabilize main.** Ideal — fewer parallel branches.
2. **Tag at an earlier commit on main.** If `main` is at SHA `F` but only commits A→D are release-ready, tag `v0.1.0` at D. Subsequent commits E and F ride the next release.

We **do not** cut a release branch and stabilize there. If main isn't tag-ready, the discipline lives in not merging speculatively to main, not in branching around it.

---

## Part 4: Git tag vs GitHub Release

### 4.1 Recommendation: both — git tag is the source of truth; GitHub Release is the user-facing view

Mechanically:

```bash
# After PR for vX.Y.Z lands on main:
git tag -a vX.Y.Z -m "engine vX.Y.Z — <one-line summary>"
git push origin vX.Y.Z

# Then immediately:
gh release create vX.Y.Z \
  --title "engine vX.Y.Z — <one-line summary>" \
  --notes-file release-notes-vX.Y.Z.md \
  --generate-notes  # also captures PR-titles as a secondary view
```

**Why both:**

- **Git tag** is the cryptographic anchor. Annotated tag (`-a`) carries the curator's name, date, and a one-line summary that survives even if GitHub goes away. Bundle-deployers and install-pinners reference this.
- **GitHub Release** is where the human-readable CHANGELOG narrative lives. qingyun-sutando's `CHANGELOG.md` is the source of truth for *what* changed; the GitHub Release page is the same content rendered in the right place for someone arriving from the project sidebar.
- `--generate-notes` adds a secondary "all PRs since last tag" view automatically. We use it as a **completeness check** (did the author-curated CHANGELOG miss anything?), not as the primary narrative.

### 4.2 What gates must pass before tag-push?

Hard gates (must be green; tag refused otherwise):

1. **CI green** on the tagged SHA.
2. **Health-check green** (`python3 src/health-check.py`) on a fresh clone of the tagged SHA.
3. **Smoke-check the headline feature** (manual; release proposal names the procedure).
4. **All migrations for this version are present, idempotent, and tested** — qingyun-sutando's section 2.7. Tags cannot ship `breaking` entries without corresponding migrations.
5. **Migration smoke-test** — run prior release first → `git pull` to this release → observe `startup.sh` applies migrations cleanly. Catches "framework works on greenfield install but not on upgrade."

Soft gates (warn but don't block):

- Open issues tagged `release-blocker` on the milestone for this version. The curator should clear these before tagging, but if owner says "ship anyway," the tag goes.
- `CHANGELOG-PENDING.md` shouldn't be empty (otherwise this is a no-content release — why are we cutting?).

### 4.3 Annotated, signed tag

```bash
git tag -a -s vX.Y.Z -m "..."
```

`-s` (signed) is the right default if owner has GPG configured. Skipping it is fine for the first few cuts; harden later. Bot-prepared tags should never push `-s`; only owner pushes signed tags. This guarantees the cryptographic anchor is owner-attested.

---

## Part 5: What to cut next (open question 4 from qingyun-sutando)

qingyun-sutando proposed three options and leaned **(c)** — cut `v0.1.0` now with manual migration steps in release notes; build the framework as `v0.1.1`.

**My read: (c) for pragmatism. Same conclusion, slightly different reasoning.**

- (a) — cut without framework — is risky **only** if a breaking change ships in the same release. The current `main` is additive-only changes since #876's `PRIVATE_DIR → MEMORY_DIR` alias landed. The alias itself is the manual migration. So (a) is actually safe *for this specific cut*, but the precedent is bad — we'd be implying "additive is always safe to ship without framework," and the next non-additive PR could slip through.
- (b) — build framework first — is correct in principle but adds 1-2 PRs of latency. If owner wants a tagged engine soon (to unblock `sutando.ag2.ai` pinning or Sutando.app's v0.3.0 ride), this is too slow.
- (c) — cut now, framework next — gives us a named v0.1.0 today and forces the framework to be the headline of v0.1.1. The release-notes for v0.1.0 honestly say "this is the 'as-built' state; the migration framework is itself the v0.1.1 feature." Sets correct expectations and lets us pin without delay.

**Concrete proposal for v0.1.0 contents:**

- Tag: `v0.1.0`
- Date: as soon as owner greenlights this RFC + the CHANGELOG-PENDING.md is consolidated from current main's PR titles.
- CHANGELOG narrative: "Initial engine snapshot. All changes since project inception are batch-recorded. No automatic migrations — install instructions name manual steps where needed (currently: SUTANDO_PRIVATE_DIR → SUTANDO_MEMORY_DIR rename, see PR #876)."
- Manual migration list in release notes: the small set of "if upgrading from a pre-tag install, do X" steps. Today there's effectively one (the env var rename), and the alias from #876 means most installs don't even need to act.

**Then v0.1.1**: ship qingyun-sutando's Phase 1 framework. First real applied migration. From then on, every breaking PR ships with a numbered migration.

---

## Part 6: How this half couples with qingyun-sutando's half

Explicit wiring (so the consolidated doc has no contradictions):

| This half says | qingyun-sutando's half says | How they meet |
|----------------|----------------------------|--------------|
| "Tag-time gate #4: all migrations for this version present + tested" (4.2) | "Per-PR discipline: PR template checklist 'does this change state format?'" (2.6 phase 3) | Same check, two enforcement points. Per-PR is the early gate; release-time is the final gate. Both required; redundant on purpose. |
| "Tag-time gate #5: migration smoke-test" (4.2) | "Migration smoke-test in release process" (2.7 step 5) | Same procedure. This half names it as part of the tag-push gate; that half names it as part of the framework. |
| "Trigger M1 = headline feature merged" (1.1) | "CHANGELOG category = feat" (1.1) | A `feat` entry doesn't auto-trigger a tag, but the existence of `feat` entries with no tag is the "tag drift" signal that arms the floor in 1.1. |
| "SemVer, bare `vX.Y.Z`" (2.1 + 2.2) | "v0.1.0 first cut" | Aligned; bare tag (no prefix) per the resolved decision. |
| "Cut v0.1.0 now, framework as v0.1.1" (Part 5) | "Phase 1 framework as v0.1.1; Phase 2 = workspace contract migration" (2.6) | Same plan, this half phases it onto the version axis. |

No double-coverage. No gap.

---

## Open questions for owner

**All resolved 2026-05-20** — see the consolidated doc's "Consolidated decisions" section. Kept here as the original proposal's open items, with resolutions:

1. **Tag-name prefix** → resolved: **bare `vX.Y.Z`** (this half's `engine-` recommendation was overridden in the review round).
2. **First cut timing**: greenlight (c) — cut `v0.1.0` from current main, framework as `v0.1.1` next? Or prefer (b) — block on framework?
3. **Signed tags**: GPG-sign the owner-pushed tag by default, or skip until later? Probably skip for v0.1.0, revisit at v0.2.0.
4. **Release-curator role**: this doc and qingyun-sutando's both lean "owner cuts the tag." Should we name a fallback curator (a specific bot, or just "any bot can prepare; owner pushes")?

---

## Next move

1. Consolidate this half + qingyun-sutando's half → `notes/release-process-consolidated.md`. Single doc, no overlap, joint open-questions section.
2. Owner + Qingyun review.
3. If greenlit: promote consolidated doc to `docs/release-process.md`. Open issues:
   - "Cut v0.1.0" (one-shot, owner-driven, blocked on consolidation greenlight).
   - "Phase 1 migration framework" (qingyun-sutando's territory, sized 1-2 PRs).
   - "PR template + CI guard for state-format changes" (Phase 3 from qingyun-sutando's section 2.6, deferred).

No code shipped this turn. Plan only.
