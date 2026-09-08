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
owner caught that same miss more than once (2026-07-24 "did you use the review skill?"; earlier on
\#2177/\#2180 "did you follow the reviewer's guide?"). Successive "be more careful" fixes did not hold,
because the failure is not inattention: a reviewer who believes the diff is small has no prompt to
re-read anything. Printing the criteria and the PR's live gate state is what makes the step
unskippable, so the guarantee is structural rather than disciplinary.

```bash
python3 skills/review-preflight/scripts/review-preflight.py <PR>      # run before reviewing; reads <repo>/REVIEW.md
python3 skills/review-preflight/scripts/ci-triage.py <PR> [--repo o/n] # a red check is a pointer into the record: search its SUBJECT, not its name
```

`review-preflight.py` resolves the repo root via `git rev-parse --show-toplevel`, falling back to three
levels above its own file. `ci-triage.py` is advisory (exit 0), and is the module review-preflight will
fold in so a red check maps to a filed issue on every preflight run.

Moved here from `scripts/` on the owner's decision (2026-09-04: "both review-preflight and ci-triage
don't belong to scripts/"); `scripts/review-preflight.py` and `scripts/ci-triage.py` remain as
two-line exec shims for one release so external callers (the pr-triage skill, peers' notes) keep
working until they are repointed.
