---
name: review-preflight
description: "Review tooling for a PR: review-preflight prints REVIEW.md's criteria and the PR's live gate state before a review; ci-triage maps a PR's failing checks to already-filed issues."
user-invocable: true
---

# review-preflight

Two tools, invoked by path; neither is a boot dependency of the core.

**Why the preflight exists, and why it is a script rather than a reminder.** Consulting the review
criteria used to rely on memory, so it was skipped exactly where it felt safe to skip -- small diffs
-- and a readiness verdict with the criteria unread is an over-claim rather than a fast review. The
owner caught that same miss more than once and asked directly whether the review skill had been
used. Successive "be more careful" fixes did not hold,
because the failure is not inattention: a reviewer who believes the diff is small has no prompt to
re-read anything. Printing the criteria and the PR's live gate state is what makes the step
unskippable, so the guarantee is structural rather than disciplinary.

```bash
python3 skills/review-preflight/scripts/review-preflight.py <PR>      # run before reviewing; reads <repo>/REVIEW.md
python3 skills/review-preflight/scripts/ci-triage.py <PR> [--repo o/n] # a red check is a pointer into the record: search its SUBJECT, not its name
```

## Delegated reviews

When the review itself is handed to another model (Codex via `claude-codex`, Gemini via
`claude-router`), that model never loads `CLAUDE.md` or `REVIEW.md`, so it cannot see the lessons
this preflight prints. The session that delegates owns the step:

1. Run `review-preflight.py <PR>` first and paste its criteria into the delegate's brief.
2. Check the gate items it reports (prior art, stale approvals) yourself; they are live GitHub
   state, not something the delegate re-derives.
3. Verify any finding you will post as a blocker before posting it.

The preflight reads `<repo>/REVIEW.md` and exits non-zero where there is none, so for a PR in a repo
without one, write the brief from the PR itself.

`review-preflight.py` resolves the repo root via `git rev-parse --show-toplevel`, falling back to three
levels above its own file. `ci-triage.py` is advisory (exit 0), and is the module review-preflight will
fold in so a red check maps to a filed issue on every preflight run.

Moved here from `scripts/` on the owner's decision (2026-09-04: "both review-preflight and ci-triage
don't belong to scripts/"); `scripts/review-preflight.py` and `scripts/ci-triage.py` remain as
two-line exec shims for one release so external callers (the pr-triage skill, peers' notes) keep
working until they are repointed.
