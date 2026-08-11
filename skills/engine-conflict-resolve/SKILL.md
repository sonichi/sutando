---
name: engine-conflict-resolve
description: Core-side handler for the desktop app's "Resolve with Sutando" engine-update conflict button. Reproduces the conflicted merge in a scratch git worktree, lets the agent resolve it there, proposes the resolution to the owner, and applies it to the live engine checkout only after explicit confirmation.
---

# engine-conflict-resolve

When the desktop app's git-aware engine updater hits merge conflicts it aborts
completely and records `ENGINE_UPDATE_PENDING.json`. "Resolve with Sutando" in
the app writes a task file `tasks/task-engine-conflict-<epoch>.txt`
(`source: desktop-app`, `interaction_type: system_event`) whose body names the
pending file and the engine checkout. This skill is the protocol for that task.

**Iron rule: never modify the live checkout until the owner confirms.** All
merge work happens in a scratch worktree; only `apply.py` touches the live
checkout, and only behind the updater's own lock and staleness guards.

## Protocol

### 1. Prepare — reproduce the merge in a scratch worktree

Take `--pending` and `--engine` verbatim from the task body (both paths may
contain spaces — always double-quote).

```bash
python3 skills/engine-conflict-resolve/scripts/prepare.py \
  --pending "<…/ENGINE_UPDATE_PENDING.json>" \
  --engine "<…/engine/sutando>"
```

- `{"status": "clean", "merged_sha": …}` — the merge replays cleanly (the
  recorded conflict no longer reproduces). Skip step 2; the merged_sha IS the
  proposal — go to step 3 and report "merged cleanly, no conflicts remained".
- `{"status": "conflicts", "scratch": …, "conflicting_files": […]}` — resolve
  in the scratch (step 2).
- `{"status": "error", "reason": "pending-stale", …}` — the checkout moved
  since the conflict was recorded. Do NOT improvise: report to the owner that
  the state is stale and that relaunching the app (or re-running the updater)
  will re-detect. Stop.
- Re-running prepare is safe: an existing scratch for the same merge is reused.

### 2. Resolve the conflicts IN THE SCRATCH

For each file in `conflicting_files`, read `<scratch>/<file>` — it contains
standard conflict markers (`<<<<<<< HEAD` = the user's local version,
`>>>>>>> <sha>` = the new release). Resolve **semantically**: understand what
the user's change and the release change each intend and produce content that
preserves both intents (when they are irreconcilable, prefer keeping the
user's behavior and note it in the proposal). Edit the files in the scratch,
then stage each one:

```bash
git -C "<scratch>" add -- "<file>"
```

Never edit files in the live checkout, and never run `git` in the live
checkout during this step.

### 3. Propose — commit in the scratch and report to the owner

```bash
python3 skills/engine-conflict-resolve/scripts/propose.py --scratch "<scratch>"
```

- Exit 2 `{"status": "unmerged", …}` — you missed a file; go back to step 2.
- `{"status": "proposed", "merged_sha": …, "files": …, "diffstat": …,
  "summary_lines": […]}` — report the proposal. The proposal text must
  contain:
  - the `summary_lines` (one line per conflicted file: what was kept), with
    your own one-line semantic note per file where the mechanical verdict
    isn't self-explanatory;
  - the `diffstat`;
  - the `merged_sha` and the `scratch` path (the follow-up session needs both);
  - a clear ask: reply **"apply"** to update the live engine, or "discard".

  **Delivery needs BOTH of the following** — the task's `source` is
  `desktop-app`, and no bridge polls results for that source, so a result
  file alone is a dead letter the owner never sees:
  1. Write the task's result file (`results/task-engine-conflict-<epoch>.txt`,
     same id as the task) — protocol hygiene: the dashboard, result-watcher,
     and timeout logic key off it.
  2. **Actively surface the proposal on the owner's live channel**: post it
     to the owner's active AG2 Space room via the gateway `op:message` path
     (the `skills/agent-room-ops/` capabilities — gateway URL + token
     resolution per `_gateway.py` / `gateway_credentials.py`; the legacy
     `AG2_REMOTE_TOKEN` in the repo `.env` is an accepted token alias). If no
     gateway resolves, fall back to the repo's Pending-decisions convention:
     a macOS notification (`osascript -e 'display notification "Engine
     conflict resolved — proposal ready" with title "Sutando"'`) **and**
     append the question to the per-host pending-questions file
     (`<workspace>/hosts/<hostname>/pending-questions.md`, `<hostname>` =
     `bash scripts/sutando-config.sh host-label`).

### 4. WAIT for explicit confirmation

Never auto-apply. Confirmation is a follow-up task or message from the owner
saying apply / yes / go ahead — on whatever channel it arrives (the reply
will NOT come back through the desktop app; that path is one-way). If the
reply is unclear, ask again. Also record the
pending action in `state/voice-session-context.json` (`pending_action`:
`{"kind": "other", "what": "apply engine conflict resolution <merged_sha>",
"where": "<scratch>"}`) so a later session can pick it up.

### 5. Apply — only after the owner confirmed

```bash
python3 skills/engine-conflict-resolve/scripts/apply.py \
  --pending "<…/ENGINE_UPDATE_PENDING.json>" \
  --engine "<…/engine/sutando>" \
  --merged-sha "<merged_sha>"
```

Guards (all enforced by the script, not by you): takes the updater's own
`ENGINE_UPDATE_LOCK.d` mkdir lock (exit 3 = busy, try again shortly);
re-asserts HEAD still equals the snapshot tip recorded in pending (exit 4 =
checkout moved — go back to step 1); verifies the merged sha is a descendant
of HEAD and contains the release; re-asserts the `!/workspace/` sparse guard;
then `git merge --ff-only`. On success it clears the pending file, removes the
scratch worktree, and prints `{"status": "applied", …}` — report that to the
owner (their work remains on the snapshot branch; the engine restarts on the
new code at the next app restart).

If the owner says discard: remove the scratch
(`git -C "<engine>" worktree remove --force "<scratch>"`), leave the pending
file alone (the app's tray still owns that state), and confirm.

## Notes

- The scratch shares the engine repo's object database (`git worktree`) — no
  network, no re-fetch, and `apply` is a pure ref fast-forward.
- Scripts print machine JSON on stdout; human logs go to stderr. Exit 0 covers
  the `clean`/`conflicts`/`proposed`/`applied` outcomes; treat any other exit
  as the JSON `reason` says.
- Tests: `python3 tests/engine-conflict-resolve.test.py` (hermetic, local
  `file://` upstream, space-containing paths).
