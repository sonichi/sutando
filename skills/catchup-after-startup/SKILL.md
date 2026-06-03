---
name: catchup-after-startup
description: "Rebuild last-session context from everything persisted to disk (session-state.md, conversation.log, sqlite, PRs, tasks, build_log). Run as the first action of a fresh session so the conversation buffer has context before the user types. Recall half of issue #1032."
user-invocable: true
---

# Catchup

Reconstruct "what was happening before this session started" by reading everything Sutando persists across restarts. Designed to run as the **first** action of a fresh Sutando session, so the conversation buffer carries context before the user types anything.

This is the **recall half** of [#1032](https://github.com/sonichi/sutando/issues/1032) (episodic event memory). The capture half — wiring `event_log.py` into every lifecycle point — is the other half of that issue and remains a separate followup. Catchup ships now because almost all the recoverable signal is already on disk; it just needs to be assembled in one place.

**Usage**: `/catchup-after-startup`

ARGUMENTS: $ARGUMENTS

Optional first arg is an hour window for time-bounded sections (default 3). `/catchup-after-startup 12` widens to the last 12h.

## What it pulls together

0. **Relay notes from prior session(s)** — `workspace/relay/relay-*.md` (written by `/relay`). **Read FIRST** because the narrative continuity encodes intent + judgment the structured snapshot below can't carry. See the §Relay-note read-and-archive flow below for details.
1. **Last session checkpoint** — `session-state.md` (written by `src/session-handoff.sh` on context compaction)
2. **Open PRs** — `gh pr list --author liususan091219 --state open`
3. **In-flight tasks** — `workspace/tasks/task-*.txt`
4. **Recent results** — `workspace/results/` mtime within window
5. **Pending questions** — `pending-questions.md` tail
6. **Recent voice / phone / discord activity** — last N h from `data/conversation.sqlite` (voice + phone + discord_voice tables, post-#1051 schema)
7. **Recent chat** — `logs/conversation.log` last N h
8. **Recent commits** — `git log --all --since=Nh`
9. **build_log.md tail** — most recent prose entries from the proactive loop
10. **Health** — `health-check.py` one-liner

Sections that come up empty print a short "(none)" rather than getting dropped, so it's obvious whether nothing happened vs whether the lookup failed.

## Relay-note read-and-archive flow

The relay folder layout (mirrors `workspace/tasks/` + `workspace/results/`):

```
workspace/relay/
├── relay-{epoch}.md       # pending — written by /relay during prior session(s)
└── processed/
    └── relay-{epoch}.md   # archived after this catchup invocation reads them
```

**Catchup-after-startup MUST:**

1. `mkdir -p "$WORKSPACE/relay/processed"` (idempotent — handles first-ever invocation).
2. List `"$WORKSPACE/relay/"relay-*.md` in **filename order** (epoch in the filename → chronological creation order). Skip the `processed/` subdirectory. Note: we sort by filename, NOT mtime, because `/relay --append` updates the mtime of an existing note while keeping the original filename — mtime-sort would shuffle an appended note to the end (newest), whereas name-sort preserves the original creation thread oldest-first.
3. For each unprocessed file, in order:
   - **Claim-by-mv FIRST.** Attempt `mv "$file" "$WORKSPACE/relay/processed/$(basename "$file")"`. If the mv succeeds, this catchup invocation has won the claim on this file; proceed to step (b). If the mv fails (file no longer at the source path — a concurrent catchup already consumed it), `continue` silently. `mv` within one filesystem is a single `rename(2)` syscall and is the atomic primitive here; do NOT condition the print step on the file being present at the source path (that's the TOCTOU race).
   - **Then read the moved file.** `cat "$WORKSPACE/relay/processed/$(basename "$file")"` under a "📡 Relay note from prior session" header that includes the file's mtime as a human-readable age (e.g. `_(written 2h ago)_`), before any of the structured sections (1-10) below. The age annotation is a stale-note guard — a relay note that sat undrained for 3 days because no catchup fired shouldn't get taken as current context.
4. If no unprocessed relay files exist (none surviving the claim-by-mv loop), print "(no relay notes from prior session — consider invoking /relay before ending sessions going forward)" under the same header and proceed to section 1.

The atomicity guarantee comes from the `mv` claim being the FIRST action per file, not the LAST — `mv` is the rename(2) syscall, which the kernel guarantees is atomic for paths on the same filesystem. The pre-mv `[ -f "$file" ]` existence check in the impl is a cheap fast-path; the authoritative race-loss signal is the mv's non-zero exit status. Two concurrent catchups can race the same file's mv, but exactly one will win — the loser sees the file already moved and continues. This mirrors the same atomic-claim pattern the result-watcher drain already uses on `task-*.txt`.

**Phase 2 caveat — cross-host fleet sync.** Syncing *unprocessed* `relay-*.md` (i.e. the source-path directory, not just `processed/`) across multiple cores via memory-sync would reintroduce a cross-host consume race: two cores' catchups could each claim the same file locally before the sync converges. Phase 1 is single-host only (relay folder lives under the per-host workspace) so this doesn't bite. If/when fleet sync of pending relay notes lands, the cross-host claim needs a different primitive (advisory lock file, per-host ownership tag in the note, or push-only-to-`processed/` semantics) — flagged here so Phase 2 doesn't accidentally lose the atomicity guarantee.

## Steps

1. Run `bash scripts/catchup-after-startup.sh [hours]`. Defaults to a 3-hour window via `CATCHUP_HOURS=3`.
2. Read the output into the conversation context. Do NOT discard sections silently — they shape decisions made next (which PR to push, which task is stale, which channel the conversation was in). **Read the relay note FIRST and treat it as the narrative ground truth before validating against the structured snapshot.**
3. If the user's first prompt references something the catchup briefing doesn't cover (e.g. "what happened yesterday"), widen the window: `/catchup-after-startup 24` and re-read.
4. Cite specific recovered items as the basis for the next action (e.g. "Per the relay note, PR #1051 was just merged and the next step is X — addressing that first.").

## Setup (one-time, recommended)

Catchup reads `session-state.md` to learn what the previous session was doing at the moment it ended. Out of the box, that file is written **only** by the `PreCompact` hook in `src/session-handoff.sh` — so if the previous session exited via ⌘Q (or crashed) without a compaction in between, the file is stale: it reflects the last compact, not the last close. The most-recent N-minute window is then invisible to the next session's catchup.

Closing that gap = adding a **SessionEnd** hook that fires the same `session-handoff.sh`. After install, `session-state.md` always reflects the latest close (compact OR clean exit), and catchup gets a freshest possible briefing.

```bash
bash $(bash scripts/sutando-config.sh claude-sutando-config-dir)/skills/catchup-after-startup/scripts/install-hook.sh
```

The installer is idempotent — safe to re-run. It edits `$(bash scripts/sutando-config.sh claude-sutando-config-dir)/settings.json` and adds:

```json
"SessionEnd": [{
  "hooks": [{
    "type": "command",
    "command": "bash \"${SUTANDO_REPO_DIR:-$HOME/Desktop/sutando}/src/session-handoff.sh\" \"${TRANSCRIPT_PATH:-}\""
  }]
}]
```

Requires `SUTANDO_REPO_DIR` env or a checkout at `~/Desktop/sutando` (the same convention `session-handoff.sh` uses for auto-detect).

### Migrating from a pre-#1366 install (`SessionStop` → `SessionEnd`)

Before [#1366](https://github.com/sonichi/sutando/pull/1366) `install-hook.sh` registered the hook under the event name `SessionStop` — which Claude Code silently no-op'd (`Unknown hook event 'SessionStop' was ignored`). If you installed before that PR merged, your `$(bash scripts/sutando-config.sh claude-sutando-config-dir)/settings.json` still carries the dead key, and any *other* hooks you (or other skills) registered under `SessionStop` are equally dead, regardless of the command they invoke.

**No action needed in normal use.** The migration auto-runs every time `/catchup-after-startup` fires — i.e. every fresh session bootstrap that goes through `/schedule-crons` or `/proactive-loop` step 1. It's a universal key-rename: every entry under `SessionStop` is moved to `SessionEnd` and the `SessionStop` key is dropped. Dedup is built-in (a command already present in `SessionEnd` is not re-added).

If you'd rather migrate by hand without waiting for the next session:

```bash
python3 $(bash scripts/sutando-config.sh claude-sutando-config-dir)/skills/catchup-after-startup/scripts/migrate-settings-hooks.py
```

Or re-run the installer (which calls the migration + then ensures the `session-handoff.sh` `SessionEnd` entry is present):

```bash
bash $(bash scripts/sutando-config.sh claude-sutando-config-dir)/skills/catchup-after-startup/scripts/install-hook.sh
```

Verify:

```bash
python3 -c 'import os, json; s=json.load(open(os.environ.get("CLAUDE_CONFIG_DIR", os.environ["HOME"]+"/.claude") + "/settings.json")); h=s.get("hooks",{}); print("SessionEnd:", json.dumps(h.get("SessionEnd"), indent=2)); print("SessionStop key present:", "SessionStop" in h)'
```

`SessionEnd` should list every previously-stale command (including `session-handoff.sh`); `SessionStop key present` should print `False`.

**Without the hook** catchup still works — you just lose the last few minutes of the previous session's narrative when that session ended outside a compact. The rest (open PRs, in-flight tasks, sqlite, conversation.log, build_log) is real-time persisted and recovers regardless.

## Wiring for auto-invocation (operator-side, NOT in this PR)

The skill ships as the slash command only. **Auto-firing on every fresh session is the operator's choice** — this PR doesn't modify any loop or hook to call `/catchup-after-startup` for you. Wire it yourself wherever your proactive-loop / startup-orchestrator skill defines its on-activation block. Sample snippet for a personal proactive-loop SKILL.md:

```markdown
## Session-start catchup (FIRST action of a fresh session)

If this is the first proactive-loop pass after a fresh session start
(cold start, no prior context about what was happening), run
`/catchup-after-startup` BEFORE anything else. Read the briefing into
context, then proceed with the normal loop. Skip on subsequent passes
within the same session.
```

Also useful to invoke manually after a `/pull-and-restart` (services restart but the conversation buffer is the same) or after a context compaction (layer the briefing onto the new compacted context).

## Dependency note: sqlite section requires #1051's per-surface schema

The voice/phone/discord activity section queries `voice` / `phone` / `discord_voice` tables — introduced in [sonichi/sutando#1051](https://github.com/sonichi/sutando/pull/1051). On a db that pre-dates #1051, the section prints "(sqlite query failed — db schema may pre-date #1051)" and the other 9 sections still work. If #1056 lands first, that section will be empty until #1051 merges; the rest of the briefing is unaffected.

## What it does NOT recover

- **In-flight reasoning that never hit disk** during the previous session ("I was about to do X but hadn't said it yet"). Out of scope without a finer-grained checkpoint mechanism. Mitigated by the SessionEnd hook (separate followup) which forces a session-handoff snapshot on clean exit, not just PreCompact.
- **The model's working memory / vibe / rapport.** Catchup gives data, not feel.
- **Events from before the time window.** Widen with `/catchup-after-startup 24` or `/catchup-after-startup 168` (a week).

Both mitigations are tracked under #1032's wider scope.
