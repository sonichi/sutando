# CHANGELOG-PENDING

Unreleased changes to be curated into the next `CHANGELOG.md` entry.

**Instructions for contributors:** add a one-line entry under the appropriate section when your PR introduces a user-visible change. PR number in brackets at the end. Entries are curated (not auto-generated) before each release.

Format: `- Brief description of what changed. ([#NNN])`

---

## Added

<!-- feat() PRs go here -->
- ag2-sparrow: opt-in AgentActivity task lifecycle. `REMOTE_TASK_LIFECYCLE_V2=1` sends the lease `worker_id` and `execution_generation` on ack and results, beats each task on `/v1/tasks/heartbeat`, stops the core when the broker cancels or reclaims an attempt (withdrawn when a newer generation is acked here first; a newer attempt it stopped anyway is posted as failed); a task the broker closes, or cancels before the ack, leaves the inbox if the core has not taken it up, and the core is stopped if it has, and reconciles with `/v1/tasks/active`. An attempt nobody will answer stops beating and is posted as failed: its task file left the queue unanswered for 10 min (never dropped from flight as completed elsewhere), or the core took the queued file up and then wrote no turn activity or result for 30 min while the core is not mid-turn (2 h when its state is unknown). `REMOTE_TASK_TURN_CONTEXT=1` adds per-attempt turn files and gates each result on the turn's room actions. Both default off. ([#5883])

## Fixed

<!-- fix() PRs go here -->
- task-orphan-check: a task's age is how long it has waited in this inbox (its `local_time:` header), not since the sender sent it, so messages held at the broker while the app was closed are answered on relaunch instead of archived as orphans. Only a `local_time:` above `task:` counts, so a body line cannot make a fresh task from an older writer look orphaned. ([#5929])

## Changed

<!-- refactor(), perf(), docs() changes visible to operators go here -->

## Security
- privacy: new `hooks/public-write-privacy-guard.py` (PreToolUse on Bash, auto-registered for the core, every worker and their subagents) refuses a `gh` PR / issue / release / `gh api` write to a non-private repo whose text names a private repo, room, person or agent from the host's `<workspace>/state/privacy/public-denylist.txt`, or carries a home-directory path, a Matrix room ID or an MXID, naming the term and its category. It fails closed on a body it cannot read. `scripts/seed-public-denylist.py` seeds the list from the host's private repos, rooms and rosters; the list never enters the repo. **Migration:** run `python3 scripts/seed-public-denylist.py` once per host. Until then the gate still refuses home paths, MXIDs and room IDs, skips the private-term list, and warns on stderr with that instruction.

<!-- security fixes go here -->
