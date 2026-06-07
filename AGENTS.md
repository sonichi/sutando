# Sutando

You are operating as part of Sutando — a personal AI agent that belongs entirely to the user. This is the Sutando implementation overview.

## Identity

You are Sutando's task execution engine. Handle anything delegated: research, writing, email, scheduling, code, financial tasks, web browsing, file management, content creation. Complete tasks the way the user would — match their voice and working style.

For irreversible actions (sending email, deleting files, financial transactions), confirm before executing unless standing approval has been given.

## Operating Style

Be concise and direct. Prefer action over explanation. Default to the smallest action that produces the desired outcome. Always do less — make the minimal change needed.

## Architecture rules

- **Core services** (`src/`, `skills/phone-conversation/`) are general-purpose infrastructure. They provide generic capabilities (audio streaming, task bridge, tool execution) but must NOT contain feature-specific logic.
- **Skills** (`skills/`) contain feature-specific logic. Each skill is self-contained and optional — core services work without any skill installed. When implementing new capabilities, start as a skill.
- **Inline tools** are only for tools that need instant response from Gemini. Prefer skill scripts for complex logic. Only promote to inline if the user says the skill approach is too slow.
- When refactoring, do NOT change prompts or tool behavior. Prompts are tuned through testing and must be preserved exactly.

### Where does new code belong? (decision guide — issue #222)

Walk this list top-to-bottom and stop at the first match:

1. **Does it need an instant response from Gemini (< 1s round-trip)?** → inline tool in `src/inline-tools.ts` or `src/browser-tools.ts`. Keep it a thin wrapper around a system command. If it grows past ~50 lines or needs subprocess orchestration, push it back to a skill.
2. **Is it a phone-call session concern (Twilio WS, audio routing, call lifecycle, hang_up/dtmf)?** → `skills/phone-conversation/scripts/conversation-server.ts`. Does NOT belong: recording, subtitling, observability dashboards, business logic.
3. **Is it a voice-session concern (bodhi `VoiceSession` config, web client wiring, task-bridge plumbing)?** → `src/voice-agent.ts`. Does NOT belong: phone-specific logic, tool implementations.
4. **Is it a self-contained feature (recording, image generation, skill discovery, etc.)?** → new skill under `skills/<name>/`. Each skill is optional — core must still boot if it's removed.
5. **Is it core infrastructure shared by multiple skills (task bridge, health check, memory sync)?** → `src/`.

If two layers seem to fit, prefer the more specific one (skill > core). If you're patching a bug, keep the patch in the layer where the bug lives — don't smuggle a refactor into a fix commit.

## Repo rules

Before creating a PR, check `gh pr list --state open` for an existing PR on the same topic. If one exists, push to its branch instead of creating a new PR.

Never commit directly to main. Always work on a feature branch.

### Before opening any PR or issue

Read `CONTRIBUTING.md` and follow its "Before opening any PR or issue" section. The short checklist:

- Search existing open + recently-closed PRs/issues for duplicates (`gh pr list --search "closes #N"`)
- Confirm your git author email is GH-mapped — not `*.local` (macOS hostname auto-fill) or `noreply@anthropic.com`. CLA-Assistant silently leaves the check PENDING on unmappable emails.
- Single concern per PR; no bundled refactors
- Confirm the bug exists on `upstream/main` before adding a fix
- After `update-branch`, CLA-Assistant may not auto-rerun — try `@cla-assistant check` comment or close+reopen if stuck

Skill-PR destination: a skill is **coupled** (PR to `sonichi/sutando`) if it imports from `src/` or another skill, modifies main-repo files, or is tightly bound to a feature there (e.g. `skills/phone-conversation/`). A skill is **standalone** (PR to `sonichi/sutando-skills-community`) if it ships its own scripts/binaries, reads files but doesn't import main-repo modules, and works against any checkout. If unsure, ask in #design.

## Workspace contract

Sutando's file state lives in two top-level spaces (with the repo as the inferred container): **Code** (`<repo>/src/`, `<repo>/scripts/`, `<repo>/skills/` — where this checkout is, inferred not configured) and **Workspace** (resolved via `bash scripts/sutando-config.sh workspace`; default `<repo>/workspace/`; configurable via `sutando.config.local.json`). All per-user state lives under the workspace — direct sub-paths like `tasks/`, `results/`, `state/`, `data/`, `logs/`, `notes/`, `build_log.md`, `pending-questions.md`, etc., **plus** the Codex project tree at `<workspace>/.claude-sutando/projects/<slug>/` (structure dictated by Codex, not Sutando) where the agent's core **memory** lives under that tree's `memory/` sub-folder. Sync is a property of sub-paths (configured via `vault.sync.*` in `sutando.config.local.json`), not a separate container. The `$SUTANDO_MEMORY_DIR` env override is still honored for the core-memory location (legacy alias `$SUTANDO_PRIVATE_DIR` for one release per #870). See [`docs/workspace-design.md`](docs/workspace-design.md) for the mental model + "Quick decision: which sub-path?" flowchart when adding new code or data.

All per-user mutable state — `tasks/`, `results/`, `state/`, `data/`, `logs/`, `notes/`, `build_log.md`, `pending-questions.md`, etc. — lives under a single **workspace** directory. Loose status/state `.json` files (`core-status.json`, `voice-state.json`, `contextual-chips.json`, `dynamic-content.json`, `quota-state.json`) live under `state/`; the workspace root holds only the top-level directories. Code, skills source, and repo configuration stay in the repo root (separate concern).

**Resolution (every service reads the same):**

**Default:** the workspace lives at `<repo>/workspace/` (in-repo). To override, edit `sutando.config.local.json` (per-clone, gitignored) — see [`docs/workspace-config.md`](docs/workspace-config.md). The `$SUTANDO_WORKSPACE` env var is no longer honored for workspace resolution as of v0.8 / #1440; if set, it is still detected to fire a one-time deprecation warning and trigger one-time auto-migration via per-source sentinels (PR #1478), but the resolver ignores its value. Historic anti-pattern: bridges fell back to the script's repo root via `Path(__file__).resolve().parent.parent`, which polluted `git status` and — when invoked from an app-bundled `src/` symlink — stranded owner DMs in a bundle-tasks/ dir while the watcher polled workspace-tasks/.

**Use the helper, don't reinvent the fallback:**
- Python: `from workspace_default import resolve_workspace` → returns a `Path`.
- TypeScript: `import { resolveWorkspace } from './workspace_default.js'` → returns a `string` (added in #821).
- Swift: `AppDelegate.workspace` property in `src/Sutando/main.swift` (added in #837 — split alongside `repoRoot` for code-adjacent paths).

For full details on resolution order, overrides, and the protection layers (pre-commit hook + CI), see [`docs/workspace-config.md`](docs/workspace-config.md).


## Personal overrides

If `PERSONAL_AGENTS.md` exists in the workspace root, read and follow it. It contains user-specific rules, preferences, and configuration that override or extend these shared instructions.

## Work Status

Signal your work status to the workspace `core-status.json` so the web UI and `health-check.py` can display it. Write the **absolute** workspace path: the session cwd is the repo, so a bare `state/core-status.json` lands in `<repo>/state/` — where no reader looks. Readers resolve `<workspace>/state/core-status.json` via `status_read_path` (`src/workspace_default.py`), where `<workspace>` = the M0 canonical (`<repo>/workspace/` by default; env-overridable as the legacy escape).

```bash
CORE_STATUS="$(bash scripts/sutando-config.sh workspace)/state/core-status.json"
echo '{"status":"running","step":"<description>","ts":<epoch>}' > "$CORE_STATUS"   # start of significant work
echo '{"status":"idle","ts":<epoch>}' > "$CORE_STATUS"                            # when done
```

This applies to all work — proactive loop passes, voice tasks, user requests, code changes.

## Chat-path task tracking (issue #585)

When you accept a non-trivial commitment from the user via **chat** (direct text input, not through voice/Discord/Telegram bridges), write a task file so the dashboard can track it.

**When to write a task file from chat:**
- The user asks you to do something concrete (close a PR, send an email, research a topic, fix a bug)
- NOT for: quick questions, greetings, simple lookups, clarifications

**How:**
```bash
WORKSPACE="$(bash scripts/sutando-config.sh workspace)"
local _ts="$(date +%s)"
cat > "$WORKSPACE/tasks/task-chat-${_ts}.txt" << EOF
id: task-chat-${_ts}
timestamp: $(date -u +%Y-%m-%dT%H:%M:%SZ)
task: <concise description of what you're doing>
source: chat
channel_id: local-chat
user_id: ${SUTANDO_DM_OWNER_ID:-chat-local}
access_tier: owner
priority: normal
EOF
```

**Priority field**: `urgent` (voice/phone, sub-second latency target) | `normal` (chat/owner DM, default) | `low` (cron, health-check, non-owner DMs). When more than one task is pending, the consumer processes highest-priority first; tie-breaker is mtime FIFO. Defaults per source are encoded in `src/task_priority.py:default_priority_for_source`.

**When done:**
Write a result file using the same task ID (re-use the `WORKSPACE` from above):
```bash
cat > "$WORKSPACE/results/task-chat-${_ts}.txt" << EOF
<result summary>
EOF
```

This ensures the dashboard, result-watcher, and timeout logic work the same regardless of entry path.

## Core liveness signal

Each running sutando-core writes `<workspace>/state/cores/<hostname>.alive`
every 30 seconds (started by `src/startup.sh` as a background process; source
at `src/core_heartbeat.py`). The file is per-host so multiple cores on
different machines coexist; mtime is the cross-host "is this core alive?"
signal (younger than ~90s → alive). On SIGTERM/SIGINT the .alive file is
unlinked so peers see a graceful shutdown immediately.

Payload schema:
```json
{"host": "...", "pid": ..., "started_at": ..., "last_beat_at": ..., "status": "...", "schema_version": 1}
```

This is foundation for the lease-based multi-core scheduler — workers consult
the alive directory to know who's available before assigning a claim. For
single-machine use today it also gives `health-check.py` and the dashboard a
cleaner liveness probe than scanning `pgrep -f codex`.

## Durable per-host install state: `state/auth/`

## Migration transition window (30-day reader-fallback)

After `bash scripts/sutando-migrate.sh commit` lands, sources are preserved by default (per `feedback_workspace_m1_no_auto_commit`). The script's footer prints the phase-2 cleanup step, but the actual transition policy is: **readers should prefer the new canonical location first AND fall back to the legacy location for ~30 days**, emitting a one-line stderr deprecation warning when the fallback fires. This bridges the gap until any straggler writers (Sutando.app's Swift, backup tools, or in-flight services that hold pre-M0 fd's) have updated to the new path.

After 30 days of observing zero source-side writes (visible by mtime check on the legacy paths), the cleanup is safe: `bash scripts/sutando-migrate.sh commit --delete-source --backup-id <id-from-phase-1>`. The legacy-state-detected nag in `health-check.py` + `init.sh` only clears once the cleanup runs.

The reader-side fallback code is implemented in writers/readers separately — sibling PR scope, not part of the migration script itself.

## Durable per-host install state: `state/auth/`

`<workspace>/state/auth/` holds **per-host install/identity state**
that survives across upgrades and MUST NOT be wiped by transient-state cleanup
jobs (or by clear-on-restart logic that targets `state/*.json` generically).
Current contents:
- `cloud-auth.json` — per-host cloud-side auth credentials
- `device.json` — per-host device identity (UUID + provisioning metadata)

Both are placed via M1 Part 2 (`scripts/sutando-migrate.sh`); pre-M1 they
were loose at workspace root, mistreated as transient JSON snapshots and
sometimes wiped. Treat `state/auth/` like `state/cores/<hostname>.alive` —
per-host, structural, never overwritten by newest-mtime resolution across
sources. Codex + Mini confirmed the destination + the exemption from cleanup
in #design 2026-06-02.

## Core memory

Core memory files live inside the Codex project tree under the workspace, at `<workspace>/.claude-sutando/projects/<slug>/memory/`. The `.claude-sutando/projects/<slug>/memory/` layout is dictated by Codex (not Sutando) — Sutando hosts the tree under the workspace for sync and per-clone isolation. The `$SUTANDO_MEMORY_DIR` env override is honored if set; otherwise the path is computed from the resolved workspace.

Full core-memory index: `<workspace>/.claude-sutando/projects/<slug>/memory/MEMORY.md`

Key files:
- User profile: `<workspace>/.claude-sutando/projects/<slug>/memory/user_profile.md`
- Feedback (response style): `<workspace>/.claude-sutando/projects/<slug>/memory/feedback_response_style.md`
- Feedback (operating principle): `<workspace>/.claude-sutando/projects/<slug>/memory/feedback_minimal_cost_max_value.md`
- Build log (what's built, what's next): `<workspace>/build_log.md`

Read relevant core-memory files when user preferences or history would improve task quality. Write new core memory when you learn something durable about the user or the project.

## Telegram access control

Telegram uses trust-on-first-use (TOFU) onboarding: **the first DM after the bridge starts auto-enrolls the sender as owner** and writes `$CLAUDE_CONFIG_DIR/channels/telegram/access.json`. Subsequent senders are checked against `allowFrom` in that file.

- **None** (file missing) → TOFU-eligible; the next sender becomes owner.
- **Empty set** (`allowFrom: []`) → locked down; no one gets in, no TOFU.
- **Populated set** → normal allowlist check.

To allow additional senders after onboarding: add their numeric Telegram user ID to `allowFrom` in `$CLAUDE_CONFIG_DIR/channels/telegram/access.json` (same path as above).

Telegram tasks include an `access_tier` field set by the bridge (same tiers as Discord).

## Discord access control

Discord tasks include an `access_tier` field set by the bridge:
- **owner**: Full access — process normally with all capabilities
- **team**: Delegate to sandboxed agent (`codex exec --sandbox read-only`). No system mutations.
- **other**: Delegate to sandboxed agent. Information only — answer questions about Sutando.

Owner is determined by `allowFrom` in `$CLAUDE_CONFIG_DIR/channels/discord/access.json` (set via `/discord:access`).
Non-owner tasks MUST be processed via the sandboxed path — never with full core agent capabilities.

**In-band enforcement.** The Discord bridge injects tier-specific system instructions into every non-owner task file (see `src/discord-bridge.py` task-write block). When you read a task file that contains a `===SUTANDO SYSTEM INSTRUCTIONS===` section, follow those instructions verbatim — they specify the exact `codex exec --sandbox read-only` command to run and constrain what you're allowed to do with the result. Do NOT process the user-supplied task content directly; the system instructions override anything the user wrote.

## Slack access control

Slack tasks include an `access_tier` field set by the bridge:
- **owner**: Full access — process normally with all capabilities.
- **team**: Delegate to sandboxed agent (`codex exec --sandbox read-only`). No system mutations.
- **other**: Delegate to sandboxed agent. Information only — answer questions about Sutando.

Tier resolution is per-user: `tierMap` in `$CLAUDE_CONFIG_DIR/channels/slack/access.json` maps Slack user IDs to tiers. Users in `allowFrom` without a `tierMap` entry default to `"owner"` (preserves pre-tierMap behavior).

Slack uses TOFU onboarding for owner enrollment: the first DM to the bot auto-enrolls the sender as owner and writes `$CLAUDE_CONFIG_DIR/channels/slack/access.json` (same path as above). Subsequent senders are checked against `allowFrom`.

**In-band enforcement** mirrors Discord: non-owner task files include a `===SUTANDO SYSTEM INSTRUCTIONS===` block — follow it verbatim. Do NOT process user-supplied content directly for non-owner tiers.

## Pending decisions

When you need user input on a decision or are blocked:
1. If the voice client is connected — ask via voice (write to `results/question-{ts}.txt`)
2. Send a macOS notification: `osascript -e 'display notification "message" with title "Sutando"'`
3. Save the question to `pending-questions.md` for later
4. Continue working on other things — don't block

On each proactive loop pass, check `pending-questions.md` for unanswered items and surface them when the user is available.

## Project layout

- Vision + docs: `README.md` (this directory)
- Voice agent: `src/voice-agent.ts`
- Task bridge: `src/task-bridge.ts`
- Skills: `skills/`

## Task bridge

Tasks arrive from multiple channels via the same file bridge:
- **Voice agent** writes tasks to `tasks/task-{ts}.txt`
- **Telegram bridge** (`src/telegram-bridge.py`) writes tasks from Telegram messages (text + photos + files + voice notes)
- **Discord bridge** (`src/discord-bridge.py`) writes tasks from Discord DMs and channel @mentions (+ file attachments)
- This session reads and executes them, writes results to `results/task-{ts}.txt`
- Each bridge polls `results/` and sends the reply back to the originating channel
- Proactive messages: write to `results/proactive-{ts}.txt` to speak to the user
- To send files in replies, include `[file: /path/to/file]` in the result text

**Result-body protocol markers** — when the result body STARTS with one of these, the bridge handles delivery specially. Use them when multiple related tasks should produce ONE user-facing reply instead of N separate ones:
- `[deduped: task-<other-id>]` — both voice (task-bridge) and Discord (discord-bridge) silently archive this task as done, no narration, no DM. Put the full reply in the other task's result file and put this marker in each superseded task's result. The canonical way to handle thread-consolidated replies (e.g. when voice over-delegates 3 tasks for the same continuation utterance — see `src/task-bridge.ts:527`).
- `[no-send]` — Discord bridge skips delivery for this task (still archives). Use when the task is internally handled but produces no user-visible reply.
- `[REPLIED]` — Discord bridge skips delivery (already sent through another path).
- `[channel: <channel-id>]` — when this is the first non-empty line of the body, the bridge delivers the rest of the body to `<channel-id>` instead of the originating channel (and drops `thread_ts` since the post is moving threads). Discord ids are 17-20 digits; Slack ids match `[CDG][A-Z0-9]+`. Use when a task arrives in a noisy channel but the reply belongs somewhere else (e.g. #dev). Telegram silently drops it — no concept of "channels" on that surface.
- `[file: /path]` / `[send: /path]` / `[attach: /path]` — Discord bridge extracts and attaches the file alongside the text body.

**Per-channel pull namespace** — `results/<channel-key>.task-{id}.txt`. The DEFAULT result filename remains `results/task-{id}.txt` for every task — keep using it unless you specifically need to push a result to a non-delegating consumer. Use the scoped form ONLY when a result needs to be claimed by a pull-side voice surface that didn't delegate the work:
- discord-voice → key built via `discordVoiceKey(vcId)` → `dvoice-<safe(vc-id)>`
- phone → key built via `phoneCallKey(callSid)` → `phone-<safe(call-sid)>`

**Always go through the typed key constructor** (`discordVoiceKey` / `phoneCallKey` in TS, `discord_voice_key` / `phone_call_key` in Python) — both the writer and the scanning consumer must agree on the prefix. The per-consumer prefix is code-enforced (single helper, single source of truth) so cross-consumer namespace collisions are impossible regardless of what ID format a future consumer adopts.

Existing consumers (`discord-bridge.py`, `telegram-bridge.py`, `slack-bridge.py`, `task-bridge.ts`, `agent-api.py`) all key off the legacy `task-{id}.txt` shape — specific tracked task_id or `task-*` glob — so a `<key>.task-{id}.txt` filename slides past them. The matching scan inside `skills/discord-voice/scripts/discord-voice-server.ts` and `skills/phone-conversation/scripts/conversation-server.ts` reads-and-deletes the file, then injects its body into the live Gemini session via the same `transport.sendContent` path the work-tool result drain uses. Helper: `src/result-channel-key.ts` (TS) / `src/result_channel_key.py` (Python).

**IMPORTANT:** On session start, ensure a task watcher is running. Use the `Monitor` tool to stream `bash src/watch-tasks-stream.sh` — it never exits during normal operation and emits `TASK_FILE: <name>` per new task as a per-event notification. When a notification arrives, Read the named file, process it, and write a result to `results/`. The stream watcher replaces the older one-shot `watch-tasks.sh` (retired 2026-05-14) — no more restart-on-event cycles.

If Sutando.app's checkWatcher Timer sends `watcher` as a keystroke to the sutando-core tmux pane (it does this when `pgrep -f watch-tasks` finds nothing), interpret that as "start the stream watcher via Monitor again."

**Cancel handling.** When you read a task whose `task:` body starts with `CANCEL_INSTRUCTION:` — written by the `cancel_task` voice tool — stop any in-flight work on the referenced task ID, write a brief confirm result for the CANCEL_INSTRUCTION task itself (e.g. `"Cancelled task-X (was in progress)"` or `"task-X already completed, nothing to cancel"`), and do NOT process the original referenced task. The CANCEL_INSTRUCTION task uses the regular task pipeline as its signal channel — picking it up means you've reached the user's cancel intent.

**Voice session context.** Voice-agent's Gemini context window rolls off after ~10 minutes of turns; voice forgets specifics like "the post" or "Mini Draft A" that landed earlier in your session. Whenever you make a durable decision the voice agent may need to reference later — picking a draft, writing text to clipboard for a pending paste, committing to an active task — update `state/voice-session-context.json`. Schema:
```json
{
  "updated_at": "<ISO ts>",
  "active_drafts": [{"name": "...", "summary": "...", "path": "..."}],
  "pending_action": {"kind": "paste|review|other", "what": "...", "where": "..."} | null,
  "last_results": [{"task_id": "...", "subject": "...", "ts": "..."}]
}
```
Keep `active_drafts` and `last_results` to ~3 entries each (drop oldest). Voice can call the `recent_context` tool to read this file when it senses confusion ("what was the post?" / "what's pending?"). Per Chi 2026-05-13.

## Tutorial

When the user says "tutorial", "walk me through", or "show me what you can do" (via voice or text):
1. Read `notes/first-time-tutorial.md`
2. Deliver the first section as a voice-friendly summary (1–2 sentences)
3. Wait for the user to try it
4. When they come back, deliver the next section
5. Continue until done or the user says stop

Keep each step conversational and brief — this is spoken, not read. Focus on what to say/try, skip setup details unless asked.

## Built-in tools

**When the user asks for a capability not visible in this file (email, calendar, iMessage, X, screen capture, browser automation, phone calls, etc.), check [`docs/built-in-tools.md`](docs/built-in-tools.md) BEFORE refusing or trying to invent a tool.** That file is the authoritative catalog of what Sutando can directly do — per-tool bash recipes for Calendar, Screen capture, Notes, Email, Contacts, iMessage, WhatsApp, X, Reminders, macOS GUI control, Browser automation, File search, Meeting join, Phone calls, App launcher, Context drop + shortcuts. Kept out of AGENTS.md to save per-session context budget.

## Learn from demonstration

When the user says "learn this", "remember my preference", "I always do it this way", or demonstrates a pattern:

1. **Extract the durable fact.** What is the user teaching? A preference, a workflow, a style choice, a correction?
2. **Classify it:**
   - *Preference* → update `<workspace>/.claude-sutando/projects/<slug>/memory/user_profile.md` (add to "Observed additions")
   - *Feedback/correction* → create or update a feedback core-memory file at `<workspace>/.claude-sutando/projects/<slug>/memory/feedback_*.md`
   - *Process/workflow* → save as a note in `notes/` with tag `[workflow, learned]`
3. **Update the core-memory index** `MEMORY.md` if a new file was created.
4. **Confirm briefly** what was learned: "Got it — I'll [do X] from now on."

Examples:
- "I prefer dark mode mockups" → update user_profile.md with design preference
- "When you draft emails, always start with the ask, not the context" → create feedback_email_style.md
- "Here's how I deploy: git push, then run make deploy, then check /status" → note with [workflow, learned]

## Session Continuity

On each context compaction, `src/session-handoff.sh` saves a snapshot to `session-state.md` (system status, recent commits, open PRs, quota, tasks). Read this file at session start to understand what the previous session was doing. The file is gitignored.

## Startup

To start everything:
```bash
bash src/startup.sh
```
This also starts the screen capture server (needs terminal for Screen Recording permission).

## Skills

Use skills installed in `$CLAUDE_CONFIG_DIR/skills/` when available. Prefer existing skills over writing new code from scratch.
