---
name: own-pr-shepherd
description: "One pass over the owner's own open pull requests: list every open non-draft PR in the configured repos, classify each by its next step (changes requested, no reviewer, behind or conflicting with base, CI red, approved), and do that step without being asked: fix and push, request a reviewer, merge the base in, fix CI, or enqueue where the repo has a merge queue. Run hourly by the schedule-crons `own-pr-shepherd` entry; also use when the owner says 'keep my PRs moving' or 'shepherd my PRs'."
user-invocable: true
---

# Own-PR shepherd

Open PRs are the agent's own queue. They move without the owner asking, and finishing a
batch is not a stopping point. This pass is the backstop that makes that true even when no
event arrived: it lists every PR, so none is forgotten.

## 1. List

```bash
python3 skills/own-pr-shepherd/scripts/own_prs.py list
```

One line per PR, then a `SUMMARY` line:

```
PR o/r#123 class=changes_requested,ci_red next=fix acted=no head=1a2b3c4d base=main queue=no failing=lint url=…
SUMMARY author=<login> repos=1 prs=44 due=12 complete=yes
```

- Repos come from `OWN_PR_SHEPHERD_REPOS` (manifest `config`, env, or `--repo`); empty means
  the repo of the current checkout. The author is `OWN_PR_SHEPHERD_AUTHOR`, else the signed-in
  `gh` login. Drafts are left out.
- `SKIP …` means gh is not signed in or no repo is configured: end the pass quietly.
- `complete=no` (exit 1, with an `INCOMPLETE` line) means GitHub did not return every PR. Act
  on what was listed and say in the pass log that the listing was partial; never report it as
  the whole queue.
- **The `--limit` trap.** `gh pr list` returns 30 PRs unless told otherwise, and says nothing
  about the rest. The script pages the search API to the end. When you list PRs by hand for
  this work, always pass `--limit 1000` and compare the count with the total.
- `acted=yes` means this class was already handled at this head (see step 3). Skip it.

## 2. Act on each PR, primary class first

| class | next | do |
|---|---|---|
| `changes_requested` | `fix` | Read every unresolved review comment. Fix in a commit on the PR branch, push (never force-push), reply on each thread with what changed or why you declined. Then re-notify the reviewer (step 4). |
| `changes_addressed` | `re-solicit` | The head moved past the change request but the review latch still says `CHANGES_REQUESTED`. Ask that reviewer to look again (step 4). |
| `conflicting` | `merge-base` | Merge the base into the branch (`git merge origin/<base>`), resolve, run the tests the conflict touched, push. No rebase, no force-push. |
| `ci_red` | `triage-ci` | `python3 skills/review-preflight/scripts/ci-triage.py <N> --repo <o/r>` maps failing checks to filed issues. Caused by this PR: fix and push. Already failing on the base or filed: comment the issue link once and record it. |
| `behind` | `merge-base` | `gh pr update-branch <N> --repo <o/r>` (a merge, not a rebase). A mechanical base merge needs no reviewer ping. |
| `approved` | `enqueue` | Approved on the current head, mergeable, CI not red, and the base has a merge queue: add it to the queue (`gh api graphql -f query='mutation($id:ID!){enqueuePullRequest(input:{pullRequestId:$id}){mergeQueueEntry{position}}}' -f id=<PR node id>`). |
| `approved` | `ask-owner` | Same, but the base has no merge queue (or it could not be read): ask the owner once with `scripts/ask-owner.py`. Do not merge. |
| `needs_reviewer` | `request-review` | No reviewer requested and no review yet. Pick reviewers with the `collaboration-intelligence` skill, request them on GitHub, and notify them where they read (step 4). |
| `approved_ci_pending`, `waiting_review`, `queued` | `none` | Nothing to do this pass. |
| `unknown` | `recheck` | GitHub has not computed mergeability yet; the next pass sees it. |

A PR can carry several classes; fixing the primary one often clears the rest in the same
push. Before each push, re-read the PR (`gh pr view`): another session may have moved it.

**Pace.** Fixes are real work. Take at most five fix-class PRs (`fix`, `merge-base` with a
conflict, `triage-ci` that needs a fix) per pass, oldest update first; heavy ones go to a
subagent per `docs/subagent-delegation.md`. Cheap steps (`re-solicit`, `request-review`,
`behind`, `enqueue`) are done for every PR that is due. The rest wait for the next pass.

## 3. Record

After each handled step:

```bash
python3 skills/own-pr-shepherd/scripts/own_prs.py record <o/r#N> <class> --head <full head sha after your push>
```

`list` then shows `acted=yes` for that class until the head moves, so a reviewer is asked
once per head, a CI failure that is not this PR's is triaged once, and the owner is asked once.

## 4. Notify reviewers

A push re-notifies no one. Follow CONTRIBUTING.md's "Solicit and NOTIFY reviewers": request on
GitHub and address each reviewer (and their agent stand-in) where they read, resolved by the
`collaboration-intelligence` skill. Not after a mechanical base merge.

## Boundary

Act without asking on everything above. Ask the owner (`scripts/ask-owner.py`) only for:

- **product decisions**: a review asks for a behavior or scope change the owner has not decided;
- **closing a PR**: superseded, duplicate, or no longer wanted. Never close one yourself;
- **merging outside a merge queue**: never `gh pr merge`, never `--admin`, never a bypass.
  Bots never merge directly where repo policy forbids it; enqueueing an approved PR into the
  repo's own merge queue is the only merge step this pass takes.

Also never force-push, never rewrite another author's commits, and never push to a PR whose
branch you cannot push to as yourself (a fork without maintainer edits): note it and move on.
Only the owner's PRs are in scope; other people's PRs are not this pass's to change.

## Event-driven companion

This pass is hourly. With the `pr-watch` skill on for the same repos, review and push events
arrive as tasks within minutes and are handled the same way; the hourly pass then only catches
what no event reported.

Tests: `tests/skills/own-pr-shepherd/own-prs.test.py`.
