---
name: pr-review
description: "Review tooling for a PR: review-preflight prints REVIEW.md's criteria and the PR's live gate state before a review; ci-triage maps a PR's failing checks to already-filed issues."
user-invocable: true
---

# pr-review

Two tools, invoked by path; neither is a boot dependency of the core.

```bash
python3 skills/pr-review/scripts/review-preflight.py <PR>      # run before reviewing; reads <repo>/REVIEW.md
python3 skills/pr-review/scripts/ci-triage.py <PR> [--repo o/n] # a red check is a pointer into the record: search its SUBJECT, not its name
```

`review-preflight.py` resolves the repo root via `git rev-parse --show-toplevel`, falling back to three
levels above its own file. `ci-triage.py` is advisory (exit 0), and is the module review-preflight will
fold in so a red check maps to a filed issue on every preflight run.

Moved here from `scripts/` on the owner's decision (2026-09-04: "both review-preflight and ci-triage
don't belong to scripts/"); `scripts/review-preflight.py` and `scripts/ci-triage.py` remain as
two-line exec shims for one release so external callers (the pr-triage skill, peers' notes) keep
working until they are repointed.
