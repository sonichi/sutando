# Claude Code Hook Contract — v1

The stable contract between Claude Code's hook events (an **external vendor
protocol**) and Sutando's observability spine. What we register, what each raw
event carries, how it maps into the universal obs envelope, which fields
persist, and where the privacy boundary sits. The machine-readable twin is
[`src/observability/claude/hooks/hook-registry.json`](../../src/observability/claude/hooks/hook-registry.json),
kept honest by `tests/observability/claude/hooks/hook-registry.test.ts` —
edit the registry and the code together or the conformance test fails.

Per `standard.md` (sutando-socket): the thing standardized here is **not
Claude's event names** — it is AG2's *interpretation, mapping, privacy boundary
and source of truth* for those events.

## 1. Provider + version

- Provider: `claude-code` (Claude Code CLI). Hook keys verified against
  https://code.claude.com/docs/en/hooks.md at the time each was added; field
  shapes verified against **real payloads**, which beat the docs where they
  disagree (`tool_response` not `tool_output`; `MessageDisplay` streams
  `delta` chunks; `UserPromptSubmit` carries `prompt`).
- Unknown/new events do not break anything (see §7).

## 2. Registered events (the actual settings payload)

`build-hook-settings.mjs` registers **10 event keys**, all pointing at the thin
forwarder `obs-hook.sh`:

`UserPromptSubmit, UserPromptExpansion, MessageDisplay, PreToolUse,
PostToolUse, Stop, SessionStart, SessionEnd, PreCompact, Notification`

Two deliberate deltas between *registered* and *modeled*:

- **Modeled but NOT registered** (decoder + mapper handle them defensively;
  Claude will not fire our hook for them today): `PostToolUseFailure`,
  `SubagentStart`, `SubagentStop`, `TaskCreated`, `TaskCompleted`. Registering
  any of these is a deliberate follow-up behavior change, not a doc edit.
- **Side-channel**: `PostToolUse` carries a second, independent registration
  (`matcher: Skill`) feeding anonymous skill-usage product telemetry
  (PostHog) — a separate pipeline from the obs collector, out of scope here.

## 3. Raw input fields

Every payload may carry the common envelope: `session_id`, `transcript_path`,
`cwd`, `permission_mode`, `ts`. Per-event fields are the strict interfaces in
`src/observability/claude/cc-hooks.ts` (the "detect → strictly type" boundary);
that file is the authoritative field list, this doc does not duplicate it.

## 4. Raw → normalized mapping

Normalization happens **collector-side** (`hook-map.ts`, pure; plus a stateful
normalizer for streamed messages). The hook script itself never transforms.
Mapping (authoritative table = the registry JSON):

| Raw event | Normalized kind(s) |
| --- | --- |
| PreToolUse | `tool.call` |
| PostToolUse / PostToolUseFailure | `tool.result` (+ paired `file.read` / `file.change` for file tools) |
| UserPromptSubmit | `cc.prompt` |
| UserPromptExpansion | `cc.prompt_expansion` |
| MessageDisplay | *(none from the pure mapper)* → stateful `cc.message` |
| Stop | `cc.hook.stop` |
| SessionStart / SessionEnd | `cc.hook.session_start` / `cc.hook.session_end` |
| PreCompact | `cc.hook.pre_compact` |
| Notification | `cc.hook.notification` |
| SubagentStart / SubagentStop | `cc.hook.subagent_start` / `cc.hook.subagent_stop` |
| TaskCreated / TaskCompleted | `cc.hook.task_created` / `cc.hook.task_completed` |

All normalized events ride the **existing universal obs envelope**
(`src/observability/events.ts`) — hook standardization adds a vocabulary and a
boundary, **not** a second envelope.

### Naming discipline (three "task"s, per standard.md)

- `delivery.task.*` — Sparrow/bridge durable tasks (the `tasks/` + `results/`
  file protocol). **Fact source: the task files + broker lifecycle.**
- `cc.hook.task_*` — Claude Code's *internal* TaskCreated/TaskCompleted.
  **These are engine-internal and must never be read as Sparrow task state.**
- `tool.*` / `cc.prompt` / `cc.message` — turn/tool execution telemetry.

Hooks are **execution telemetry, not a task state machine**: `PreToolUse`
proves a tool call was attempted; `Stop` proves a turn ended. Neither proves a
Sparrow task started, stayed healthy, or completed — those facts come from the
task/result files and broker lifecycle, and consumers must not infer them from
hook events.

### Permission vs. governed approval

`permission_mode` (and any future `PermissionRequest` registration) concerns
**local coding-runtime tool permission**. The runtime-api's approval lane
(`capability.approval.*`, `src/runtime-api/`) concerns **governed business
actions** (message.send, merges, spends) with bind-to-action+resource,
consume-once semantics. They are distinct lanes and must never share a kind or
be folded into one `approval_required` flag.

## 5. Persistence + redaction boundary

Three tiers (registry `visibility` field):

- **product** — events intended for AG2 Space surfaces. **Currently: none.**
  Promoting a kind to product tier requires an explicit redaction review, not
  a registry edit alone.
- **diagnostic** — local collector/sink only. Everything today.
- **raw** — full payloads; exist only transiently at the ingest route. Never
  persisted verbatim by the mapper (`trunc()` caps `tool_input`/
  `tool_response`/`prompt`).

`sensitive: true` entries (prompts, tool I/O, messages, unknown payloads) may
contain file contents, shell secrets, absolute paths, or customer data. The
standing rule: **no hook payload is ever forwarded to a room or remote surface
as-is.** Whatever product tier lands later must map to summaries (e.g. "Agent
ran tests"), never raw `tool_input`.

## 6. Delivery semantics

`obs-hook.sh` is fail-open by design: no endpoint configured → drain stdin,
no-op; POST capped at 1s; all errors swallowed; always exit 0. Hook delivery is
therefore **best-effort** — it never blocks or fails the agent, and consumers
must tolerate gaps. Anything that needs reliable delivery does not belong on
this channel (it belongs on the task/result protocol or the runtime API).

## 7. Unknown events

`decodeClaudeCodeHook` accepts any object with a non-empty string
`hook_event_name`. Not-yet-modeled events map to
`cc.hook.<snake(hook_event_name)>` carrying all non-envelope fields
(diagnostic tier, treated as sensitive). New vendor events therefore degrade
gracefully; modeling one properly means: add its interface to `cc-hooks.ts`,
a mapper case, a registry entry, and (if it should actually fire) the
registration — the conformance test forces the first three to move together.

## 8. Schema version

`hook-registry.json` carries `schemaVersion: 1`. Bump on breaking changes to
the registry shape itself; adding events or kinds is additive and needs no
bump.
