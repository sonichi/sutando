---
name: own-pr-shepherd
description: "One pass over the owner's own open pull requests: list every open non-draft PR in the configured repos whose last non-merge commit is the owner's, classify each by its next step (changes requested, no reviewer, behind or conflicting with base, CI red, approved), and do that step: fix and push, ask a reviewer, merge the base in, fix CI; an approved PR is the owner's call. Opt-in: the hourly schedule-crons `own-pr-shepherd` entry runs only once the owner enables it. Also use when the owner says 'keep my PRs moving' or 'shepherd my PRs'."
user-invocable: true
---

# Own-PR shepherd

Once the owner turns this on, their open PRs are the agent's own queue: they move without the
owner asking, and finishing a batch is not a stopping point. This pass is the backstop that
makes that true even when no event arrived: it lists every PR, so none is forgotten.

## 0. Enable (owner's word only)

Off on every install. The `own-pr-shepherd` entry in `crons.example.json` is `"opt_in": true`,
so the seeder never starts it. Add it to the host's `crons.json` only when the owner asks, and
set `OWN_PR_SHEPHERD_REPOS` with them: an empty repo list makes the pass `SKIP`. Enable it on
one host per owner email; the acted record is per host, so two hosts would each act once.

## 1. List

```bash
python3 skills/own-pr-shepherd/scripts/own_prs.py list
```

One line per PR, then a `SUMMARY` line:

```
PR o/r#123 class=changes_requested,ci_red next=fix acted=no head=1a2b3c4d base=main failing=lint url=…
SUMMARY author=<login> email=<owner email> repos=1 prs=44 not_mine=3 due=12 complete=yes
```

- Repos come from `OWN_PR_SHEPHERD_REPOS` (manifest `config`, env, or `--repo`); there is no
  default. The search author is `OWN_PR_SHEPHERD_AUTHOR`, else the signed-in `gh` login. Drafts
  are left out.
- **Whose PR it is.** A login can be shared by several agents, so the login alone does not make
  a PR the owner's. A PR is listed only when its last non-merge commit's author email is
  `OWN_PR_SHEPHERD_EMAIL` (else `git config user.email`), the same rule `pr-shepherd-contract`'s
  `resolve_actor` uses. Others are counted as `not_mine` and left alone.
- `SKIP …` means no repo is configured, no owner email is known, or gh is not signed in: end
  the pass quietly.
- `QUOTA_LOW` means fewer GraphQL points remain than `OWN_PR_SHEPHERD_MIN_QUOTA` (installs on
  one login share the budget). The listing stops there: act on nothing this pass.
- `complete=no` (exit 1, with an `INCOMPLETE` line) means GitHub did not return every PR. Act
  on what was listed and say in the pass log that the listing was partial; never report it as
  the whole queue.
- **The `--limit` trap.** `gh pr list` returns 30 PRs unless told otherwise, and says nothing
  about the rest. The script pages the search API to the end. When you list PRs by hand for
  this work, always pass `--limit 1000` and compare the count with the total.
- `acted=yes` means this class was already handled at this head (see step 3), or a reviewer ask
  for this PR was logged in `state/review-asks.jsonl` (by `notify_reviewers.py`, from this pass
  or any other path) after the head commit. Skip it.

## 2. Act on each PR, primary class first

| class | next | do |
|---|---|---|
| `changes_requested` | `fix` | Read every unresolved review comment. Fix in a commit on the PR branch, push (never force-push), reply on each thread with what changed or why you declined. Then re-notify the reviewer (step 4). |
| `changes_addressed` | `re-solicit` | A non-merge commit landed after the change request and the review latch still says `CHANGES_REQUESTED`. Ask that reviewer to look again (step 4). A base merge alone does not count: the PR stays `changes_requested`. |
| `conflicting` | `merge-base` | Merge the base into the branch (`git merge origin/<base>`), resolve, run the tests the conflict touched, push. No rebase, no force-push. |
| `ci_red` | `triage-ci` | `python3 skills/review-preflight/scripts/ci-triage.py <N> --repo <o/r>` maps failing checks to filed issues. Caused by this PR: fix and push. Already failing on the base or filed: comment the issue link once and record it. |
| `behind` | `merge-base` | `gh pr update-branch <N> --repo <o/r>` (a merge, not a rebase). A mechanical base merge needs no reviewer ping. |
| `approved` | `ask-owner` | Approved on the current head, mergeable, CI not red: ask the owner once with `scripts/ask-owner.py` (with `--option`, so it is a card). Do not merge and do not enqueue: a merge queue merges, and merging is the owner's call. |
| `needs_reviewer` | `request-review` | No reviewer requested and no review yet. Pick reviewers with the `collaboration-intelligence` skill, request them on GitHub, and notify them where they read (step 4). |
| `approved_ci_pending`, `waiting_review`, `queued` | `none` | Nothing to do this pass. |
| `unknown` | `recheck` | GitHub has not computed mergeability yet; the next pass sees it. |

A PR can carry several classes; fixing the primary one often clears the rest in the same
push. Before each push, re-read the PR (`gh pr view`): another session may have moved it.

**Pace.** Fixes are real work. Take at most five fix-class PRs (`fix`, `merge-base` with a
conflict, `triage-ci` that needs a fix) per pass, oldest update first; heavy ones go to a
subagent per `docs/subagent-delegation.md`. Cheap steps (`re-solicit`, `request-review`,
`behind`, `ask-owner`) are done for every PR that is due. The rest wait for the next pass.

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
`collaboration-intelligence` skill. Send the room ask with its `notify_reviewers.py`, which
logs it in `state/review-asks.jsonl`: that ledger is the one ask record every path reads, so an
ask made elsewhere is not repeated here. Not after a mechanical base merge.

## Boundary

Act without asking on everything above. Ask the owner (`scripts/ask-owner.py`) only for:

- **product decisions**: a review asks for a behavior or scope change the owner has not decided;
- **closing a PR**: superseded, duplicate, or no longer wanted. Never close one yourself;
- **merging**: an approved PR goes to the owner. Never `gh pr merge`, never `--admin`, never a
  bypass, and never enqueue into a merge queue: this pass takes no merge step.

Also never force-push, never rewrite another author's commits, and never push to a PR whose
branch you cannot push to as yourself (a fork without maintainer edits): note it and move on.
Only the owner's PRs are in scope; other people's PRs are not this pass's to change.

## Event-driven companion

This pass is hourly. With the `pr-watch` skill on for the same repos, review and push events
arrive as tasks within minutes and are handled the same way; the hourly pass then only catches
what no event reported.

Tests: `tests/skills/own-pr-shepherd/own-prs.test.py`.
