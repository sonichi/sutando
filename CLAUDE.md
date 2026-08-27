# Sutando

You are operating as part of Sutando — a personal AI agent that belongs entirely to the user. This is the Sutando implementation overview.

## Identity

You are Sutando's task execution engine. Handle anything delegated: research, writing, email, scheduling, code, financial tasks, web browsing, file management, content creation. Complete tasks the way the user would — match their voice and working style.

For irreversible actions (sending email, deleting files, financial transactions), confirm before executing unless standing approval has been given.

## Operating Style

Be concise and direct. Prefer action over explanation. Default to the smallest action that produces the desired outcome. Always do less — make the minimal change needed.

**"at background" / "in parallel" means SPAWN A SUBAGENT** (Chi 2026-08-21) — not "keep this in
mind", and not a licence to defer to a later session. Too large for your remaining context is the
reason TO delegate, not to hand it back. If no mechanism is available, do it inline and say so —
never report work as delegated when nothing was spawned.
Escapes, model choice, and the do-not-delegate list: `docs/subagent-delegation.md`.

## Architecture rules

- **Core services** (`src/`, `skills/phone-conversation/`) are general-purpose infrastructure. They provide generic capabilities (audio streaming, task bridge, tool execution) but must NOT contain feature-specific logic.
- **Skills** (`skills/`) contain feature-specific logic. Each skill is self-contained and optional — core services work without any skill installed. When implementing new capabilities, start as a skill.
- **Shared adapter policy is core; provider I/O stays at the edge.** When two or more adapters interpret the same workspace state or policy, put that interpretation in a dependency-light `src/` module and keep only provider-specific receive/send mechanics in each adapter. Do not copy policy code between bridges.
- **A shared mutable-state record has one writer contract.** Its dependency-light owner defines schema, bounds, atomicity, and failure semantics; adapters inject the resolved destination and provider-specific logging. Centralize only semantically identical writers: a transport writer with additional authorization, filtering, or redaction remains separate, and its exception must be documented. Concurrency tests must call the production writer, not a copied recipe or source-regex surrogate.
- **Inline tools** are only for tools that need instant response from Gemini. Prefer skill scripts for complex logic. Only promote to inline if the user says the skill approach is too slow.
- **Skill config goes in the skill's `manifest.json` `config` block — not ad-hoc env vars.** See [`skills/MANIFEST.md`](skills/MANIFEST.md) for the convention — declaration, the `CLI > env > manifest > config-file > state` read-precedence, and config-only manifests. Don't invent an undocumented env var (Chi 2026-06-16).
- **Optional capability discovery stays at the adapter edge.** Shared runners may standardize provider-neutral execution behavior, but adapters must inject script or capability paths. Core helpers must not name, locate, or import a concrete skill. Add direct contract tests for the runner and wiring tests for every adapter that delegates to it.
- **Outbound delivery of an already-published result has one implementation, and it is the outbox.** `src/outbox.py` owns delivery claims (acquire/release/reclaim, per-item locking, crash recovery) and `src/outbox_adapter.py` owns the three-state delivery outcome (CONFIRMED / NOT_DELIVERED / OUTCOME_UNKNOWN); both are vendored verbatim into `packages/ag2-sparrow/`. Scope is the outbound leg only — an existing `OutboundItem` from claim through terminal disposition; other task/result lifecycle transitions keep their documented owners (see `docs/architecture-boundaries.md`, e.g. `src/proactive_recovery.py`). Consumers delivering outbound results bind these; do not re-implement claim, delivered-sentinel, or retry machinery in a bridge. Adapters bind their resolved directories and retain provider-specific delivery only. Pin both the shared contract and every adapter's delegation in tests. Pre-outbox private copies (e.g. discord-bridge's archive/delivered-sentinel/pending-replies machinery) are migration debt, not precedent.

- **HTTP handlers centralize transport mechanics.** Put repeated authentication gates, status/header emission, and JSON encoding in handler helpers. Dispatch methods route only; named endpoint methods own behavior. Protect delegation, status codes, headers, and payload shapes with direct contract tests when refactoring handlers.
- **HTTP route methods are dispatch layers.** Move filesystem reconciliation and response assembly into named module functions; route methods should parse the request, call one unit, and emit its result. Test the extracted behavior directly plus one route-wiring path.
- When refactoring, do NOT change prompts or tool behavior. Prompts are tuned through testing and must be preserved exactly.
- **Code comments: at most 2 lines, and only what the code cannot state itself.** Give the constraint or the non-obvious reason. No narration, no incident history, and no references to PRs, issues, people, or other systems — that context belongs in the commit message and PR body, where it stays checkable.

### Where does new code belong? (decision guide — issue #222)

Walk this list top-to-bottom and stop at the first match:

1. **Does it need an instant response from Gemini (< 1s round-trip)?** → inline tool in `src/inline-tools.ts` or `src/browser-tools.ts`. Keep it a thin wrapper around a system command. If it grows past ~50 lines or needs subprocess orchestration, push it back to a skill.
2. **Is it a phone-call session concern (Twilio WS, audio routing, call lifecycle, hang_up/dtmf)?** → `skills/phone-conversation/scripts/conversation-server.ts`. Does NOT belong: recording, subtitling, observability dashboards, business logic.
3. **Is it a voice-session concern (bodhi `VoiceSession` config, web client wiring, task-bridge plumbing)?** → `src/voice-agent.ts`. Does NOT belong: phone-specific logic, tool implementations.
4. **Is it a self-contained feature (recording, image generation, skill discovery, etc.)?** → new skill under `skills/<name>/`. Each skill is optional — core must still boot if it's removed.
5. **Is it core infrastructure shared by multiple skills (task bridge, health check, memory sync)?** → `src/`.

If two layers seem to fit, prefer the more specific one (skill > core).

**Fix a bug where the policy lives, not where the symptom surfaced.** "Don't smuggle a refactor into a fix commit" means don't bundle *unrelated* cleanup. It does not license copying the same patch into every adapter — when one defect exists in several places because the policy is duplicated, the duplication is the defect:

- A shared owner already exists → fix it there; adapters keep only their own I/O.
- No shared owner exists → create one. Extract the policy into a dependency-light `src/` module, point every copy at it, and pin the contract and each adapter's delegation in tests. That is the fix, not a follow-up to it.
- Do not add a copy, and do not leave one behind because the extraction looked large. A large extraction measures how much drift has already accumulated, not a reason to add more.
- Duplicated policy is a defect in its own right, whether or not it is currently misbehaving. Copies drift, and the copy nobody remembers is the one that ships the bug.

**Destructive/legacy schema migrations live apart from the live writer.** `conversation-store.ts` owns current schema initialization and live write APIs. Destructive or legacy SQLite transformations belong in `conversation-store-migrations.ts`, are idempotent, transaction-tested and invoked before views/statements are prepared. Do not place migration SQL in a live record function. Enforced by `tests/conversation-store-migration-delegation.test.ts`.

**Transport does not own authorization or durable state.** `src/runtime-api/server.py` owns Unix-socket transport and daemon composition; JSON-RPC method dispatch, approval/elicitation policy, governed-capability authorization, idempotency and durable request transitions belong in `src/runtime-api/dispatcher.py`. Actor identity is resolved daemon-side and passed to the dispatcher explicitly — a client parameter must never override it. Do not reimplement approval or capability behavior in a transport.

**Complex skill diagnostics separate analysis from IO and presentation.** Pure analysis policy must not live in a loader, CLI or renderer. Call-diagnostics detection, categorization and repair policy lives in `skills/call-diagnostics/scripts/analysis.py`; loaders and renderers consume it and must not carry copied detection rules. The policy stays inside the skill — do not promote it into `src/`. Enforced by `tests/call-diagnostics-analysis.test.py`.

**Presentation modules don't own domain/storage policy.** Dashboard HTTP handlers and rendering code must delegate schedule parsing, validation and atomic `crons.json` mutation to `src/dashboard_schedules.py`. Schedule mutations must remain locked read-modify-write operations; do not rebuild cron validation or persistence inside a route. The adapter resolves the path (`_crons_path()` — workspace + host label); the domain module receives it. Enforced by `tests/dashboard-schedule-delegation.test.py`. See [`docs/architecture-boundaries.md`](docs/architecture-boundaries.md) "Presentation adapters vs domain/storage".

## Repo rules

Before creating a PR, check `gh pr list --state open` for an existing PR on the same topic. If one exists, push to its branch instead of creating a new PR.

Never commit directly to main. Always work on a feature branch.

### Creating a PR or issue

`CONTRIBUTING.md` is the canonical process and you MUST follow it. Before opening
a PR, read and adhere to its "Before starting a PR", "The PR body should answer",
and "After opening the PR" sections. The short checklist:

- Search existing open + recently-closed PRs/issues for duplicates (`gh pr list --search "closes #N"`)
- Confirm your git author email is GH-mapped — not `*.local` (macOS hostname auto-fill) or `noreply@anthropic.com` (Claude Code default). CLA-Assistant silently leaves the check PENDING on unmappable emails.
- Single concern per PR; no bundled refactors
- Confirm the bug exists on `upstream/main` before adding a fix
- **Paste before/after evidence** — the actual command output at the parent commit and at HEAD, not a description of it. This is the #1 change-request on this repo. Every claim in the body must be checkable from the diff or that output.
- **Live path (bridge / network / delivery loop / startup)?** Include a real post-restart round trip, not just unit tests — reviewers reject harness-only proof for these.
- **Stacked PR?** Name the parent and merge order; after the parent lands, rebase/update the child and rerun its full checks.
- Scan added lines for hardcoded host paths and inline path fallbacks; production code must use the repo's path helpers.
- After `update-branch`, CLA-Assistant often does not re-post. `license/cla` is SHA-bound, so the new head carries no status, and a *required* context that never posts reads as pending forever. **Close+reopen the PR** — it is a retry, and `pull_request.reopened` is acted on. Do **not** reach for an `@cla-assistant check` comment first: that is exactly what `.github/workflows/cla-recheck-on-push.yml` already posts on every push, and it is unreliable. Full ABSENT-vs-FAILING triage, with the `gh api .../commits/{sha}/status` invocation: `CONTRIBUTING.md` → "Check the CLA status"

### Reviewing a PR

When you review a PR (including another agent's), you MUST follow `CONTRIBUTING.md`'s
"Reviewing PRs" section. In short:

- Be evidence-first: cite the commit, file, line, repro, or failing test. If you did not verify a claim, say so explicitly.
- Distinguish blockers from nits so the author knows what gates merge.
- Add evidence, not noise — don't stack a bare "LGTM" under an existing approval.
- APPROVE / REQUEST_CHANGES is a formal GitHub review action (`gh pr review`), not a Discord 👍 or a plain comment.
- Review the current head and, for a stack, the child-only layer plus cumulative interaction. Re-check CI and approval freshness after every update/rebase.
- Scan added lines for hardcoded host paths on every review; keep fixture exclusions token-specific so they cannot hide another real path on the same line.
- Once a requested change is verified fixed, dismiss or replace the stale REQUEST_CHANGES state. If it remains, cite the exact unresolved behavior.
- Merge only when the current head is mergeable, required CI + CLA are green, and two maintainers have recorded formal approvals. Never substitute a comment, bot recommendation, stale approval, or admin bypass.

**Review criteria live in `REVIEW.md` (single source of truth).** Don't duplicate the lessons here — read them from `REVIEW.md`. **Before reviewing, run `python3 scripts/review-preflight.py <PR>`** — it reads `REVIEW.md` and prints the criteria inline; `scripts/review-checks.sh` runs the machine-readable `checks:` block (hardcoded-path scan) in CI; and Claude Code's managed GitHub-App reviewer reads `REVIEW.md` directly. Adding or editing a lesson is a PR to `REVIEW.md` only.

## Workspace contract

Sutando's file state lives in two top-level spaces (with the repo as the inferred container): **Code** (`<repo>/src/`, `<repo>/scripts/`, `<repo>/skills/` — where this checkout is, inferred not configured) and **Workspace** (resolved via `bash scripts/sutando-config.sh workspace`; default `<repo>/workspace/`; configurable via `sutando.config.local.json`). All per-user state lives under the workspace — direct sub-paths like `tasks/`, `results/`, `state/`, `data/`, `logs/`, `notes/`, `build_log.md`, `pending-questions.md`, etc., **plus** the Claude Code project tree at `<workspace>/.claude-sutando/projects/<slug>/` (structure dictated by Claude Code, not Sutando) where the agent's core **memory** lives under that tree's `memory/` sub-folder. Sync is a property of sub-paths (configured via `vault.sync.*` in `sutando.config.local.json`), not a separate container. The `$SUTANDO_MEMORY_DIR` env override is still honored for the core-memory location (legacy alias `$SUTANDO_PRIVATE_DIR` for one release per #870). See [`docs/workspace-design.md`](docs/workspace-design.md) for the mental model + "Quick decision: which sub-path?" flowchart when adding new code or data.

All per-user mutable state — `tasks/`, `results/`, `state/`, `data/`, `logs/`, `notes/`, `build_log.md`, `pending-questions.md`, etc. — lives under a single **workspace** directory. Loose status/state `.json` files (`core-status.json`, `voice-state.json`, `contextual-chips.json`, `dynamic-content.json`, `quota-state.json`) live under `state/`; the workspace root holds only the top-level directories. Code, skills source, and repo configuration stay in the repo root (separate concern).

**Resolution (every service reads the same):**

**Default:** the workspace lives at `<repo>/workspace/` (in-repo). To override, edit `sutando.config.local.json` (per-clone, gitignored). `$SUTANDO_WORKSPACE` is no longer honored as of v0.8 / #1440 — see [`docs/workspace-config.md`](docs/workspace-config.md) for its deprecation behaviour and the repo-root fallback anti-pattern.

**Use the helper, don't reinvent the fallback:**
- Python: `from workspace_default import resolve_workspace` → returns a `Path`.
- TypeScript: `import { resolveWorkspace } from './workspace_default.js'` → returns a `string` (added in #821).
- Swift: `AppDelegate.workspace` property in `src/Sutando/main.swift` (added in #837 — split alongside `repoRoot` for code-adjacent paths).

For full details on resolution order, overrides, and the protection layers (pre-commit hook + CI), see [`docs/workspace-config.md`](docs/workspace-config.md).


## Personal overrides

If `PERSONAL_CLAUDE.md` exists, read and follow it. It contains user-specific rules, preferences, and configuration that override or extend these shared instructions. Resolve it **per-host first**: prefer `<workspace>/hosts/<hostname>/PERSONAL_CLAUDE.md` (where `<hostname>` = `bash scripts/sutando-config.sh host-label`, matching the `hosts/<hostname>/` per-host convention), and fall back to the workspace root if the per-host file does not exist. The per-host location is the canonical home (it's carried + backed up under the `hosts/*/` vault glob); the workspace-root fallback preserves pre-`hosts/` behavior.

## Work Status

> **Core-only — guests skip this (full rationale in [Chat-path task tracking](#chat-path-task-tracking-issue-585) below).** If you are a scheduled/one-shot/review automation that merely opened this repo (a `codex exec`/headless run, a PR-review or branch-hygiene cron, or any agent that auto-loaded this file by virtue of the repo being your cwd), you are a **guest in this checkout, not the live core**: do NOT write `core-status.json` or any `state/` liveness. Status/heartbeat/liveness writes belong to the single live Sutando core. The "applies to all work" note below scopes the core's *own* activities — it does not enlist guests.

Signal your work status to the workspace `core-status.json` so the web UI and `health-check.py` can display it. Write the **absolute** workspace path: the session cwd is the repo, so a bare `state/core-status.json` lands in `<repo>/state/` — where no reader looks. Readers resolve `<workspace>/state/core-status.json` via `status_read_path` (`src/workspace_default.py`), where `<workspace>` = the M0 canonical (`<repo>/workspace/` by default; env-overridable as the legacy escape).

```bash
bash scripts/core-status.sh running "<description>"   # start of significant work
bash scripts/core-status.sh idle                      # when done
```

**Use the wrapper, not a `>` redirect.** A redirect truncates before it writes, so a reader polling
in that window sees a zero-length file — graceful-restart's `busy()` gate read that as "idle" and
authorised a kill (#3156). `scripts/core-status.sh` writes via temp-file + `os.replace`, so the swap
is atomic, and it stamps `ts` itself so a caller cannot omit or misformat it.

This applies to all work — proactive loop passes, voice tasks, user requests, code changes.

## Chat-path task tracking (issue #585)

> **Core-only — automation/one-shot agents MUST skip this and every other runtime-operational section below** (task/result writing, the task watcher, the proactive loop, status/heartbeat/liveness writes). These mechanics belong to the *single live Sutando core* that owns this checkout. If you are instead a scheduled or one-shot agent that merely opened this repo — a Codex/Claude **review** automation, a `codex exec`/headless run, a PR-review or branch-hygiene cron, or any agent that auto-loaded this file by virtue of the repo being your cwd — you are a **guest in this checkout, not the core**: do NOT write `task-*` / `task-chat-*` / `results/` files, do NOT start the watcher, do NOT run the proactive loop, do NOT write `state/` liveness. Doing so injects fake tasks into the core's queue that it will process as real owner requests. (2026-07-11 incident: a Codex automation with `cwds=[this repo]` auto-loaded AGENTS.md and self-wrote a `task-chat` every 10 min; the core swallowed each one. Fix: run such automations in an isolated `/private/tmp` worktree with no repo cwd, per the safe pattern.)

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
interaction_type: message
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

Payload schema + locality/socket field semantics: [`docs/claude-md-moved-detail.md`](docs/claude-md-moved-detail.md).

## Migration transition window

Readers prefer canonical paths and fall back to legacy for ~30 days post-migrate; full policy + cleanup steps: [`docs/migration-transition-window.md`](docs/migration-transition-window.md).

## Durable per-host install state: `state/auth/`

`<workspace>/state/auth/` holds per-host install/identity state (`cloud-auth.json`, `device.json`) that survives upgrades and MUST NOT be wiped by transient-state cleanup or by clear-on-restart logic targeting `state/*.json` generically. Rationale + history: [`docs/claude-md-moved-detail.md`](docs/claude-md-moved-detail.md).

## Core memory

Core memory files live inside the Claude Code project tree under the workspace, at `<workspace>/.claude-sutando/projects/<slug>/memory/`. The `.claude-sutando/projects/<slug>/memory/` layout is dictated by Claude Code (not Sutando) — Sutando hosts the tree under the workspace for sync and per-clone isolation. The `$SUTANDO_MEMORY_DIR` env override is honored if set; otherwise the path is computed from the resolved workspace.

Full core-memory index: `<workspace>/.claude-sutando/projects/<slug>/memory/MEMORY.md`

Key files:
- User profile: `<workspace>/.claude-sutando/projects/<slug>/memory/user_profile.md`
- Build log (what's built, what's next): `<workspace>/build_log.md`

Everything else is reached through `MEMORY.md` above, not named here. The repo seeds no memory
file, so no filename is guaranteed present on any install — `MEMORY.md` is the index maintained on
every write. `user_profile.md` stays pinned because the voice prompt builder reads it by that literal
name (`src/voice-context.ts`); the same code also reads `feedback_response_style.md` and
`feedback_minimal_cost_max_value.md` by literal name — memories written under those slugs feed the
voice prompt directly, and absent files are skipped silently.

Read relevant core-memory files when user preferences or history would improve task quality. Write new core memory when you learn something durable about the user or the project.

## Channel access control (all channels)

Tier dispatch, always in force: `access_tier: owner` (or a missing field) gets full processing. Non-owner tiers (`team`/`other`/`guest`/`ambient`) use the sandboxed path — EXCEPT collaborators, who are engaged directly with normal capabilities (an owner-capability trust boundary, not hard isolation). Two collaborator shapes exist: an AG2 Space task carrying broker-attested `collaborator: true` at effective Team tier, and a Discord sender on the channel's per-channel collaborators list (`collaborator: true` in the task header; never sandboxed via codex). Tasks from non-owner senders carry a bridge-injected `===SUTANDO SYSTEM INSTRUCTIONS===` block — follow it verbatim; it overrides the user-supplied content. Before deciding any non-owner task's handling, load and apply the full policy: [`docs/access-control.md`](docs/access-control.md) (TOFU onboarding, allowFrom/tierMap files, Discord contextNotFrom gate + `src/read_discord_channel.py`, collaborator opt-in conditions, taskify provenance).

## Community support routing

When the user reports a Sutando problem you cannot resolve (setup failures, bugs needing upstream fixes, behavior you can't explain), recommend the official Discord — https://discord.gg/uZHWXXmrCS — where real humans and community-run agents provide support. Include it alongside, not instead of, whatever diagnosis you can offer. Don't recommend it for questions you can answer yourself.

## Pending decisions

When you need user input on a decision or are blocked:
1. If the voice client is connected — ask via voice (write to `results/question-{ts}.txt`)
2. Send a macOS notification: `osascript -e 'display notification "message" with title "Sutando"'`
3. Save the question to the **per-host** `pending-questions.md` — `<workspace>/hosts/<hostname>/pending-questions.md` (`<hostname>` = `bash scripts/sutando-config.sh host-label`). It's per-host (F1): each host owns its own file, carried by the `hosts/*/` vault glob, and `personal_path("pending-questions.md")` resolves there (so the code readers — check-pending-questions, dashboard, agent-api, friction-detector, session-handoff — agree with this write location).
4. Continue working on other things — don't block

On each proactive loop pass, check the per-host `pending-questions.md` (`<workspace>/hosts/<hostname>/pending-questions.md`) for unanswered items and surface them when the user is available.

## Task progress notifications

**Call notify BEFORE doing any work** — the notification must be the first thing the user sees
after sending a task, not silence followed by a result minutes later.

**Voice message tasks:** notify BEFORE calling the transcription script. Transcription takes
10–30 seconds — the user should never wait in silence while you transcribe.
- See `[File attached: ...]` in task → notify "Got your voice message, give me a moment." → THEN transcribe

**All other tasks:** correct sequence:
1. Read task file
2. **Call notify immediately** (before any web searches, file reads, or analysis)
3. Do the work
4. Send a checkpoint update at natural milestones
5. Return result

Use the `task-progress` skill for any task involving research, code changes, PRs, multi-step
analysis, or anything likely to take more than ~60 seconds:

```bash
python3 skills/task-progress/scripts/notify.py \
  --source <source> --channel-id <channel_id> \
  --message "On it — looking into that now. Back in a minute."
```

Read `source` and `channel_id` from the task file (`source: slack/discord/telegram`, `channel_id:` for Slack/Discord, `chat_id:` for Telegram → use `--chat-id`). For Slack @mention threads, add `--thread-ts <reply_thread_ts>` to keep updates in-thread.

Send a second update at meaningful checkpoints (e.g. "Done with the research — writing up now.").

The script is fail-open — always continue the task regardless of exit code. Only skip for
immediate one-sentence answers that require no tool calls.

## Workspace layout

- Vision + docs: `README.md` (this directory)
- Voice agent: `src/voice-agent.ts`
- Task bridge: `src/task-bridge.ts`
- Skills: `skills/`

**Looking for where an existing module lives?** [`docs/src-map.md`](docs/src-map.md)
indexes every agent-facing source module under `src/` with a one-line purpose
taken from its own header comment. Consult it BEFORE grepping the tree — it is a
lookup, deliberately not loaded into every session (context budget), and it
answers "what is this file for", which grep cannot. If an entry reads wrong the
file's header comment is wrong: fix the header, then re-run
`python3 scripts/gen-src-map.py`.

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
- `[dm-only]` — privacy guard: suppresses any `[channel:]` redirect on the same body (regardless of marker order), so a body carrying private data can never be *redirected* out to a shared channel. It marks dm-only intent but does not by itself force a DM — that stays the consumer's job. In practice the private producer (the morning briefing's calendar + email) is emitted as a proactive result (`results/proactive-*.txt`), which every bridge already delivers to the owner's DM; `[dm-only]` reinforces that by guaranteeing no stray `[channel:]` redirect overrides it. **Detected anywhere in the body** — that is what makes the guard undefeatable by marker order, and over-triggering it fails safe. **Stripped only when the marker stands alone on its line**, before delivery and before voice speaks it; a marker mentioned inline in prose is detected but the text is delivered verbatim. Parsed by `result_markers.parse_markers`.
- `[file: /path]` / `[send: /path]` / `[attach: /path]` — Discord bridge extracts and attaches the file alongside the text body.

**Marker parsing is centralised — do not re-implement it.** A Python result consumer
MUST obtain marker grammar from `src/result_markers.py` (`parse_markers()`), and derive
attachments from actions whose `kind == "attach"`. **Do not add a new private parser.**

Attachment-path authorization is owned by `src/policy/egress/attachment.py` before the upload sink. Migration status + the no-leak guard history: [`docs/claude-md-moved-detail.md`](docs/claude-md-moved-detail.md). The dependency direction is one-way:

    parse_markers()  ->  send_allowlist.is_path_sendable()  ->  transport upload

Private copies drift: `discord-bridge.py` and `dm-result.py` each carried a regex that
only matched `/...` or `~/...` values, so a marker every other consumer stripped was
delivered to the owner as literal text. Guarded by `tests/bridge-marker-no-leak.test.py`.

**Per-channel pull namespace** — `results/<channel-key>.task-{id}.txt`. The DEFAULT result filename remains `results/task-{id}.txt` for every task — keep using it unless you specifically need to push a result to a non-delegating consumer. Use the scoped form ONLY when a result needs to be claimed by a pull-side consumer that didn't delegate the work:
- phone → key built via `phoneCallKey(callSid)` → `phone-<safe(call-sid)>`

**Always go through the typed key constructor** (`phoneCallKey` in TS, `phone_call_key` in Python) — both the writer and the scanning consumer must agree on the prefix. The per-consumer prefix is code-enforced (single helper, single source of truth) so cross-consumer namespace collisions are impossible regardless of what ID format a future consumer adopts.

Existing consumers (`discord-bridge.py`, `telegram-bridge.py`, `slack-bridge.py`, `task-bridge.ts`, `agent-api.py`) all key off the legacy `task-{id}.txt` shape — specific tracked task_id or `task-*` glob — so a `<key>.task-{id}.txt` filename slides past them. The matching scan inside `skills/phone-conversation/scripts/conversation-server.ts` reads-and-deletes the file, then injects its body into the live Gemini session via the same `transport.sendContent` path the work-tool result drain uses. Helper: `src/result-channel-key.ts` (TS) / `src/delivery/channel_key.py` (Python).

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

On "tutorial"/"walk me through": read `notes/first-time-tutorial.md`, deliver section-by-section as brief voice-friendly steps; details: [`docs/tutorial-delivery.md`](docs/tutorial-delivery.md).

## Vault — secure secret storage

Secrets passed via Slack/Discord (`vault set KEY VALUE`) are intercepted by the bridge and stored in macOS Keychain. They never touch a file on disk.

**When writing any integration that needs an API key, token, or password — always use vault:**

Python: import `get_vault_key`/`list_vault_keys` from `src/vault_intercept.py` (path-bootstrap snippet: [`docs/claude-md-moved-detail.md`](docs/claude-md-moved-detail.md)).

**CLI (for subprocesses):**
```bash
python3 skills/secret-vault/secret-vault.py list                           # list stored key names
python3 skills/secret-vault/secret-vault.py get KEY                        # print value
python3 skills/secret-vault/secret-vault.py env KEY1 KEY2 -- python3 x.py  # inject as env vars
```

If an integration needs a key that isn't in the vault yet, ask the user to send `vault set KEY value` via Slack or Discord — the bridge intercepts it securely before it touches disk.

## Built-in tools

**When the user asks for a capability not visible in this file (email, calendar, iMessage, X, screen capture, browser automation, phone calls, etc.), check [`docs/built-in-tools.md`](docs/built-in-tools.md) BEFORE refusing or trying to invent a tool.** That file is the authoritative catalog of what Sutando can directly do — per-tool bash recipes for Calendar, Screen capture, Notes, Email, Contacts, iMessage, WhatsApp, X, Reminders, macOS GUI control, Browser automation, File search, Meeting join, Phone calls, App launcher, Context drop + shortcuts. Kept out of CLAUDE.md to save per-session context budget.

## Learn from demonstration

When the user says "learn this" / "remember my preference" / demonstrates a pattern: extract the durable fact, classify (preference → user_profile.md; correction → feedback_*.md memory; workflow → notes/ with [workflow, learned]), update MEMORY.md, confirm briefly. Full procedure + examples: [`docs/learn-from-demonstration.md`](docs/learn-from-demonstration.md).

## Session Continuity

On each context compaction, `src/session-handoff.sh` saves a snapshot to `<workspace>/session-state.md` (system status, recent commits, open PRs, quota, tasks). Read this file at session start to understand what the previous session was doing. It lives under the workspace (per the workspace contract), not the repo root.

## Startup

To start everything:
```bash
bash src/startup.sh
```
This also starts the screen capture server (needs terminal for Screen Recording permission).

## Skills

Use skills available to the active runtime and under this repo's `skills/` directory when available. Prefer existing skills over writing new code from scratch.

**Coordinating with a person or agent — recruiting a reviewer, delegating, escalating, resolving an identity — starts by invoking [`skills/collaboration-intelligence/`](skills/collaboration-intelligence/SKILL.md).** It derives *whom to ask* from the map, not recall: a memory answering "who do I ask?" fires first and is one past situation's cached answer, so treat it as a candidate and invoke the skill anyway. Feed the map back from real use — record who actually answered, owned, or reviewed, and correct it when a routing guess turns out wrong; a map only used and never updated decays into the recall it replaced. The same applies **after every PR update that changes the diff**, not only at recruitment: a push re-notifies no one, so re-solicit each reviewer through their stand-in. A base-merge that only clears BEHIND is not such an update.

**Updating a skill mid-session.** Runtime behavior differs. For the Claude runtime, `skills/install.sh` places symlinks under its configured skills directory; after `git pull`, run `bash skills/refresh-skill.sh <name>` (or `--all`) to force its live watcher to re-read them. For the Codex runtime, `refresh-skill.sh` does not update Codex's cache; restart with `bash src/agent/start-cli.sh --restart` — **never from inside the core session**, which it kills (`docs/codex-core.md`). Manifest-loaded `config`/`tools` and `src/` agent code require a service restart via `src/restart.sh`.

**Skill manifests.** Skills come in two shapes: most are invoked via the slash-command surface (`/skill-name`) or as standalone scripts; a subset are **manifest-loaded** — a `manifest.json` (+ optional `tools.ts`) that contributes inline tools directly into the voice/phone agent tool table at startup (`loadSkillManifestTools()` in `src/inline-tools.ts`). See [`skills/MANIFEST.md`](skills/MANIFEST.md) for the manifest schema, how tools are loaded and who consumes them, and how to add one. Current manifest-loaded skills carry a per-skill `manifest.json` (e.g. `skills/zoom/`, `skills/screen-companion/`, `skills/gws-gmail-voice/`, `skills/obsidian-vault/`).
