# CHANGELOG-PENDING

Unreleased changes to be curated into the next `CHANGELOG.md` entry.

**Instructions for contributors:** add a one-line entry under the appropriate section when your PR introduces a user-visible change. PR number in brackets at the end. Entries are curated (not auto-generated) before each release.

Format: `- Brief description of what changed. ([#NNN])`

---

## Added

<!-- feat() PRs go here -->
- ag2-sparrow: opt-in AgentActivity task lifecycle. `REMOTE_TASK_LIFECYCLE_V2=1` sends the lease `worker_id` and `execution_generation` on ack and results, beats each task on `/v1/tasks/heartbeat`, stops the core when the broker cancels or reclaims an attempt (withdrawn when a newer generation is acked here first; a newer attempt it stopped anyway is posted as failed); a task the broker closes, or cancels before the ack, leaves the inbox if the core has not taken it up, and the core is stopped if it has, and reconciles with `/v1/tasks/active`. An attempt nobody will answer stops beating and is posted as failed: its task file left the queue unanswered for 10 min (never dropped from flight as completed elsewhere), or the core took the queued file up and then wrote no turn activity or result for 30 min while the core is not mid-turn (2 h when its state is unknown). `REMOTE_TASK_TURN_CONTEXT=1` adds per-attempt turn files and gates each result on the turn's room actions. Both default off. ([#5883])
- ag2-sparrow: with `REMOTE_TASK_TURN_CONTEXT=1`, a task the owner answered resumes as a new turn carrying the answer as quoted room data, once per question episode, and each task file tells the agent to ask a blocking question with `room.question.ask` on its task id, sending any visible text with `room.message.send` first (its result file is not posted while the question is open). A wake's task file says it is a new turn of the task and must end with its own result. For a wake turn only (its file carries a `wake_episode:` header), the Stop hook does not count a result older than the queued file as its answer, and a turn that recorded a turn-ledger no-send loses the in-progress exemption, so the wake it left unanswered blocks; every other task is judged as before. A woken attempt is judged answered only by its own result, not the first attempt's archived reply, so a wake stopped or closed before the core takes it is withdrawn and a lost wake is failed. A question about the owner's own errand is asked in their DM, not a shared room, and a turn whose question's outcome was lost posts `waiting_input`, never its "I asked" note as the reply. ([#5885])
- task watcher: with `SUTANDO_STOP_INTERRUPT=1` (off by default; also the agent-activity manifest's config), a Stop interrupts the core's running turn (one Escape to its pane) when that pane is mid-turn on exactly the stopped task, so a long tool call no longer delays the stop until it finishes. A turn parked on a terminal prompt (a live dialog in the pane, or an open terminal prompt card for the session) counts as on that task however old its last row, so the Escape answers the prompt, and the session's prompt cards are expired; a task the engine fails as stalled takes the same path. Otherwise the Stop stays cooperative. Each decision is logged as an owner activity row, and the task shows `stop requested` until the stop is confirmed, then CANCELLED. With the switch off, a cancel marks the task CANCELLED at once, as before. Cancel instructions and the agent-activity hook now handle a gateway instance's `task-<instance>~task-<id>` as one id, so lane tasks get their activity rows. It never Escapes a task whose turn already ended (a done row) or that has its result, reads `esc to interrupt` only on the live status line, uses the launcher's `SUTANDO_TMUX_WINDOW`, and does not confirm a stop while a pool worker still holds the task. ([#5913])
- ag2-sparrow: with `REMOTE_TASK_TURN_CONTEXT=1`, a task that asks this agent a room question carries the question as quoted room data and tells the agent to answer it once with `room.question.answer`; the turn gate ignores outcomes about another task, so answering never parks or closes the agent's own task. A question whose answer the turn's journal shows succeeded is recorded locally (bounded, per install) and a later task for it is told it was already answered instead of being given the answer rule. ([#5937])

## Fixed

<!-- fix() PRs go here -->
- task-orphan-check: a task's age is how long it has waited in this inbox (its `local_time:` header), not since the sender sent it, so messages held at the broker while the app was closed are answered on relaunch instead of archived as orphans. Only a `local_time:` above `task:` counts, so a body line cannot make a fresh task from an older writer look orphaned. ([#5929])

## Changed

<!-- refactor(), perf(), docs() changes visible to operators go here -->

## Security

<!-- security fixes go here -->
