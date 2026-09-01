---
name: engine-conflict-resolve
description: Ask Sutando to resolve a conflicted update of a Sutando checkout. When pulling the latest release/main conflicts with local changes, Sutando reproduces the merge in a scratch git worktree, resolves the conflicts there, proposes the resolution to the owner, and fast-forwards the live checkout only after explicit confirmation. First entry path is the desktop app's "Resolve with Sutando" engine-update button.
---

# engine-conflict-resolve

Anyone running Sutando from a git checkout can ask it to pull the latest
main/release — and when the update conflicts with their local changes, ask
Sutando to resolve the merge itself. This skill is that protocol: reproduce
the merge in a scratch worktree, resolve the conflicts there semantically,
propose the result, and land it in the live checkout only after the owner
explicitly confirms.

The first producer of the pending state is the desktop app's git-aware engine
updater: on a conflicted update it aborts completely and records
`ENGINE_UPDATE_PENDING.json`; the app's "Resolve with Sutando" button then
writes a task file `tasks/task-engine-conflict-<epoch>.txt`
(`source: desktop-app`, `interaction_type: system_event`) whose body names the
pending file and the engine checkout. That task is one entry path, not the
feature: the scripts take the pending-file shape as input from any producer
(a ref-based entry point — merge any two refs with no pending file — plus a
chat-facing command are queued follow-ups).

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
  recorded conflict no longer reproduces). Skip step 2 but still RUN step 3:
  only `propose.py` records the proposal `apply.py` will accept. Report
  "merged cleanly, no conflicts remained".
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
  "summary_lines": […], "proposal_record": …}` — the script has atomically
  recorded this exact `merged_sha` in `ENGINE_CONFLICT_PROPOSAL.json` next to
  the pending file; `apply.py` will land ONLY that recorded commit. If you
  change anything in the scratch afterwards, you MUST re-run propose.py (it
  overwrites the record) and re-report — the owner confirms a specific
  proposal, never "whatever is in the scratch now". Report the proposal. The
  proposal text must contain:
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
     and timeout logic key off it. **State in the result which delivery
     channel step 2 actually used** (the room, or the fallback).
  2. **Actively surface the proposal via the deterministic deliverer** —
     write the proposal text to a file, then:

     ```bash
     python3 skills/engine-conflict-resolve/scripts/deliver.py \
       --message-file "<proposal.txt>" \
       --title "Engine update conflict — proposal ready"
     ```

     The destination is DECLARED, never guessed: `--room` >
     `$ENGINE_CONFLICT_NOTIFY_ROOM` > this skill's `manifest.json` `config`
     default (skills/MANIFEST.md precedence). The room must be an
     **owner-only** room; deliver.py never infers a "last active" room —
     a merge proposal is owner-only material and a guessed room may be
     shared. When a room resolves, the post goes through the
     `agent-room-ops` gateway module (`op:message`). **When no room is
     configured, or the post fails for any reason**, deliver.py always
     executes the Pending-decisions fallback: a macOS notification plus a
     question section inserted into the per-host
     `<workspace>/hosts/<hostname>/pending-questions.md` (above the
     `# Resolved` divider, via the shared `src/pending_questions_md.py`
     locator). Its JSON output tells you which path ran — put that in the
     result file per point 1.

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
checkout moved — go back to step 1); requires the `ENGINE_CONFLICT_PROPOSAL.json`
record to exist, to match the live pending state, and `--merged-sha` to be
exactly the recorded commit (exit 5 = proposal missing/mismatched — re-run
propose.py, re-confirm with the owner); verifies the merged sha is a
descendant of HEAD and contains the release; re-asserts the `!/workspace/`
sparse guard; then `git merge --ff-only`. On success it clears the pending
file and the proposal record, removes the scratch worktree, and prints
`{"status": "applied", …}` — report that to the owner (their work remains on
the snapshot branch; the engine restarts on the new code at the next app
restart).

If the owner says discard: remove the scratch
(`git -C "<engine>" worktree remove --force "<scratch>"`), delete the
`ENGINE_CONFLICT_PROPOSAL.json` record, leave the pending file alone (the
app's tray still owns that state), and confirm.

## Git resolution (installed hosts)

The scripts never assume a usable `git` on PATH — an installed Mac may have a
sanitized PATH or only the Xcode-CLT stub. Resolution order (config declared
in this skill's `manifest.json` `config` block, per skills/MANIFEST.md):

1. `--git` CLI flag (explicit override);
2. `$ENGINE_CONFLICT_GIT` (the manifest-declared env spelling);
3. the manifest `config` default itself;
4. **derived from the engine path already passed in**: the desktop bundle
   ships a runnable `<engine-parent>/bin/git` (desktop #305) — for prepare/
   apply this comes from `--engine`, for propose from the scratch worktree's
   `.git` pointer file, so desktop tasks need no configuration at all;
5. the repo's `src/git_binary.py` contract (first non-stub git on PATH; the
   system git only when the CLT are verifiably installed — never pops the
   installer dialog).

If nothing usable resolves, every script prints `{"status": "no-git", …}`
(exit 6) instead of a traceback. An explicitly configured value (tiers 1–3)
that is not runnable is an error, not a fall-through.

## Notes

- The scratch shares the engine repo's object database (`git worktree`) — no
  network, no re-fetch, and `apply` is a pure ref fast-forward.
- Scripts print machine JSON on stdout; human logs go to stderr. Exit 0 covers
  the `clean`/`conflicts`/`proposed`/`applied` outcomes; treat any other exit
  as the JSON `reason` says.
- Tests: `python3 tests/engine-conflict-resolve.test.py` (hermetic, local
  `file://` upstream, space-containing paths).
