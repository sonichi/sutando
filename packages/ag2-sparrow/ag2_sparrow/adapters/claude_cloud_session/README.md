# Claude cloud session adapter

Feeds Sparrow/Sutando inbox tasks into **one persistent** Claude Code cloud session, as turns. The
session runs in Anthropic's cloud with the AG2 Space connector, keeps its memory across messages, keeps
Claude Code's own toolset (shell, files, web, subagents, skills; these ran without permission prompts
in the probes), and posts each reply itself. Only the AG2 Space connector is restricted, to the three
tools that did not prompt (see Limits). Unlike the
[routine adapter](../claude_routine/README.md), which starts a fresh one-shot session per message,
every message here lands in the same session. This relay still has to run on a machine. Stdlib only;
opt-in; the core never imports it.

```
python3 -m ag2_sparrow.adapters.claude_cloud_session.session_adapter \
  --work <dir> --session-id session_... [--claude <path>] [options]
```

`<dir>/tasks/task-*.txt` in, `<dir>/results/task-<id>.txt` out. One turn is queued at a time, taken
in the task protocol's priority order (`priority:` urgent, normal, low; oldest first within a
priority). The inbox loop and the payload builder are the shared
[`ag2_sparrow/relay_loop.py`](../../relay_loop.py); every durable step is the owner module
[`ag2_sparrow/task_lifecycle.py`](../../task_lifecycle.py) (canonical in `src/task_lifecycle.py`),
which only this adapter uses. This adapter renders texts and calls the CLI; it writes nothing under
`results/` or `state/` itself.

## Deployment: one topology

This adapter supports exactly one deployment: a new, dedicated, standalone relay (its own gateway
bridge with the `AGENT_CONNECT_*` environment, as the support-cc relay runs). There is no cutover from
an existing work dir and no Sutando-loader mode.

```
adapter:  --work=$CLOUD_WORK --gateway-state=$GATEWAY_STATE
bridge:   AGENT_CONNECT_TASK_DIR=$CLOUD_WORK/tasks
          AGENT_CONNECT_RESULT_DIR=$CLOUD_WORK/results
          AGENT_CONNECT_STATE_DIR=$GATEWAY_STATE        (a dedicated directory outside $CLOUD_WORK)
```

`tasks/` and `results/` are the bridge's; `$CLOUD_WORK/state/` is the adapter's alone, and nothing
lock-related lives under `$CLOUD_WORK`. `$CLOUD_WORK` is this adapter's own: never share it with any other adapter (the
routine adapter, the Codex adapter, or a second cloud-session adapter). That is an operator rule; the
adapter checks nothing about it beyond the single-process lock. The binary enforces the rest:

- **Lock.** Its first act is the package's one-process lock, `holder_lock` (shared with the routine
  and Codex adapters, so any second adapter of any kind on this dir refuses): a `flock` held for the
  process's life in a lock directory outside `$CLOUD_WORK`, under the service user's local home, and
  a holder record published at once. Then it creates `tasks/`, `results/` and `state/`. A held dir
  is exit 3 naming the holder; a live pre-`holder_lock` adapter (a live pid in `state/connector.pid`)
  is exit 3 naming the pid to stop; any other lock failure is exit 2. The lock is released on exit
  and on SIGTERM. POSIX only; a network-mounted home refuses with `LockError("not a local
  filesystem")` (remedy: a local home for the service user).
- **Upgrade order: stop → install → start.** A `state/connector.pid` left by a pre-`holder_lock`
  version is removed once when it is exactly one dead pid written before this start; anything else
  there refuses naming it.
- **Topology.** `--gateway-state` names the bridge's state dir so the adapter can check the layout:
  a relative path, or one inside `$CLOUD_WORK`, refuses (exit 2, naming it).
- **Fresh-dir rule.** Without `state/format`, `$CLOUD_WORK` may hold only the bridge's `tasks/` and
  `results/` and an interrupted first init's own `state/` (an empty `settled-ids`, an empty `claims/`,
  one marker temp `.format.<pid>.<6 hex>.tmp`); anything else refuses naming it. First init is one durable
  transaction: create and fsync the empty ledger, create and fsync `claims/`, write `state/format`
  (`cloud-session/1`) last by temp + fsync + rename, then fsync `state/`, `$CLOUD_WORK` and its
  parent; an interrupted init completes on the next start. With the marker, `state/` may hold only
  `format`, `claims/`, `settled-ids`, `rate.jsonl`, `done` and `collaborator-turns.json` (a plain
  file when present), and the ledger and `claims/` must exist; anything else refuses naming it.
- **Probe.** Rename and hard link must work in `results/` and `state/`, else exit 2.

## Setup (once)

1. Create the session interactively: `claude --cloud "s.space agent"`. It prints
   `Created cloud session: … View: https://claude.ai/code/session_… Resume with: claude --teleport session_…`.
   Keep the `session_…` id. The adapter never creates sessions.
2. Open the session's page, and in the composer's **+** → **Connectors** menu switch the **ag2 space**
   connector on. The session then acts as the agent that connector is linked to; its tools are
   `mcp__ag2_space__*` (`ag2_whoami`, `room_actions_describe`, `room_action_execute`, `room_inspect`,
   `room_list`).
3. Paste [`session-prompt.md`](session-prompt.md) (the part between its two rules) as the session's
   first message. It should answer `ready`.
4. Run the adapter with `--session-id session_…` and, so owner-only answers asked in a shared room can
   go to your DM, `--owner-dm <your DM room id>`. No token goes in the vault: the `claude` CLI's own
   login on this machine is the credential (set `CLAUDE_CONFIG_DIR` for the adapter when that login
   lives in a non-default config dir; it is passed through to the CLI).

## Wire

Per task: `claude -p --cloud <session_id> --output-format json` with the text on **stdin**, run as an
argv list (never a shell). The argv carries only that metadata, so the task text never appears in the
process table. The child's environment has only `PATH`, `HOME`, `USER` (the macOS Keychain login needs
it), `LANG`, and `CLAUDE_CONFIG_DIR` when the caller sets it. `--claude` picks the executable (default `claude` on PATH;
the adapter refuses to start, exit 2, when it does not resolve). The CLI returns in about a second with
`{"ok":true,"session_id":"…","url":"…"}` and exit 0 once the turn is **queued**; it does not return the
reply.

Text:

```
[sparrow task] Follow your standing s.space instructions for this message: …
room=<channel_id> thread=<thread_root or none> tier=<owner|team|guest> collaborator=<true|false> task_id=<id> channel_kind=<dm|room|unknown> [owner_dm=<room id>]
note: <NON_OWNER_NOTE>            (non-owner tiers only)
<UNTRUSTED_TASK_<nonce>>
<message body, untrusted>
</UNTRUSTED_TASK_<nonce>>
<bridge-written guardrail lines, when the task file has them>
```

Everything after the first line is the routine adapter's payload, built by the same shared function:
fresh nonce per turn, attested tier and collaborator, header whitespace removed, the shared
`task_io.NON_OWNER_NOTE` for non-owner tiers. The routing line adds `channel_kind`, read from the attested
`channel_kind` header the gateway stamps (`dm`, `room`, anything else `unknown`; a body line cannot
claim it), and `owner_dm` from `--owner-dm`. With them the standing instructions apply the audience
rule: in the owner's DM, answer there; in a room, post only what the room is meant to read, and send an
answer meant for the owner alone (research, findings, anything about the owner, any data from the
owner's connected accounts) to `owner_dm` with exactly one line in the room, "I sent it to you in our
DM."; with no `owner_dm`, such an answer becomes "I can't answer that here." in the room and nothing
else. The whole text is at most 65,536 characters; only the body is cut, ending in
`[truncated]`. A task with no room is not queued.

**Who is served.** Admission is the shared `task_io.admission` gate over the attested headers: the owner,
an attested collaborator (Team), nobody else. A non-owner without collaborator access (Guest, and any
collaborator claim the broker did not attest) is refused with the routine adapter's plain line ("This agent
only answers its owner here.") unless `--allow-non-owner` is passed, which queues such tasks into this
session anyway — the owner's history and connector, bounded only by the note and the standing
instructions. An attested collaborator (the broker's `collaborator: true` on the team tier, from the parsed
headers, never the message text) never enters this session: with `--allow-collaborator` it is served on
an isolated local turn; without it, it gets the same refusal line. `--allow-non-owner` does not imply
`--allow-collaborator`. The owner path is unchanged.

**The isolated collaborator turn** is `<--claude> -p --safe-mode --restricted --strict-mcp-config
--no-session-persistence --tools Read,Grep,Glob --output-format json` with the routine adapter's payload on
stdin (one lead line naming the situation, then the note, `collaborator=true`; not this session's reminder,
no `channel_kind`, no `owner_dm`), in a fresh empty temp directory removed afterwards, with the same
five-variable environment as above, bounded by `--collaborator-timeout` (default 60 s; `--timeout` is the
`--cloud` call's alone). What it can reach: the CLI's login and
the three read tools over an empty directory. What it cannot: this session (no `--cloud`, no session id, no
`--resume`, and the turn itself is never saved, so nothing can resume it), its history and connector, Bash
or code-running tools, the web, MCP servers, CLAUDE.md, hooks, skills, settings, or the work dir. The JSON
`result` string becomes the task's result as **inert text**: every result-delivery marker the shared parser
knows (`[no-send]`, `[REPLIED]`, `[deduped:]`, `[channel:]`, `[thread:]`, `[thread]`, `[reply:]`,
`[dm-only]`, `[file:]`/`[send:]`/`[attach:]`) is quoted in the parser's own form (`[ no-send]`) before
publication and checked by the parser again, so the gateway executes nothing from it and posts it once in
the originating room. Any failure (non-zero exit, no `result`, timeout, no temp dir, missing CLI) gets "This
message could not be sent to the cloud agent right now." — one try, no retry. The turn runs inside the same
claim/journal/ledger lifecycle as a queued turn, recorded as a `final` outcome, so it is never counted
against the owner provider's limiter or `--min-gap-seconds`; the turn still costs the owner's Claude plan.
One exception, which fails safe: a crash mid-turn leaves a `CALLING` claim, and the restart records it as
`unknown` in the journal like any interrupted call, so that one attempt does count toward the owner lane's
cap and gap, and its staged `NON_OWNER_FAILED` body is published without a second turn. Its own cap is
`--max-collaborator-per-hour` (1 or more, default 10, `state/collaborator-turns.json`, part of this
adapter's state contract), and its own gate: a collaborator waits only on that cap, never on the session's
cap, hold or gap, and an owner task never waits on the collaborator cap. A task that has waited longer than
`--collaborator-max-age` (default 1800 s; 0 turns the cut-off off) gets the generic line instead of a turn,
so a burst is not answered hours late. The wait counts since the task was first seen in a pending scan, or
its file time if earlier; a restart does not reset it. The line goes out at once: a stale task takes the
ungated lane and never waits on the collaborator cap, and no claim is prepared for it. The adapter log shows
`collaborator turn ->` instead of `queue ->`.

## Results

| outcome | result file |
|---|---|
| any JSON record with `"ok": true` anywhere in stdout or stderr, whatever else the output or exit code says | `[no-send]` + `# claude cloud session: <url>`; with `--result-mode echo`, `Sent to the cloud agent: <url>` (owner tasks only; non-owner rooms always get `[no-send]`) |
| no receipt, and a non-zero exit whose output proves the CLI never reached the service (DNS failure, refused connection), or the executable failed to start | retried after `--backoff` × 2ⁿ (cap `--backoff-max`), up to `--max-retries` (default 2), then "could not be reached … after several tries" |
| no receipt otherwise: any other non-zero exit, exit 0 without one, or a timeout (`--timeout`, default 60 s; a receipt printed before the timeout still counts as queued) | "may or may not have reached the cloud agent …; it was not retried" — never resubmitted |

A non-owner task's failure line never carries the exit code.

### The task lifecycle (task_lifecycle.py)

Each task has one claim, `state/claims/<task_id>/claim.json` (temp + fsync + rename), one attempt at a
time: `PREPARED → CALLING → {NOT_DELIVERED | CONFIRMED | OUTCOME_UNKNOWN} → RESULT_STAGED → PUBLISHED →
SETTLED`. `CALLING` is durable before the CLI runs. The exact rendered result body is staged at
`state/claims/<task_id>/body` before it is published, and removed at `SETTLED`.

- **Which result exists.** A strict classifier answers exactly one of `LIVE_READY`, `LIVE_UNREADY`,
  `CLAIMED` (a result-disposal claim `.task-<id>.disposing-*`), `ARCHIVED`, `QUARANTINED` (any copy
  under `results/undelivered/`, hash-free), `ABSENT`, or `UNKNOWN` when any lookup fails. Precedence:
  `UNKNOWN` > `LIVE_READY` > `ARCHIVED` > `QUARANTINED` > `CLAIMED` > `LIVE_UNREADY` > `ABSENT`. The
  decision is taken under `results/.disposal.lock`: `ABSENT` may call; a terminal state
  (`LIVE_READY`, `ARCHIVED`, `QUARANTINED`) settles with no call and no new result; `CLAIMED`,
  `LIVE_UNREADY` and `UNKNOWN` park the task and re-check it later.
- **Publication (writer contract).** A result is written once and never replaces another. Under the
  disposal lock the stage is copied to a unique dot-prefixed temp in `results/`, fsynced, read back,
  renamed to a private sibling, and renamed no-replace to `results/task-<id>.txt`, with `results/`
  fsynced after each step; the final file is read back under the same lock. A successful rename is
  the win; if the final file is gone afterwards (a drain took it), the task still reads as won and is
  never retried. If the name is taken, a ready result there settles the task (lost); an unfinished one
  parks it. Delivery and claims of published results belong to the bridge's outbox; nothing here
  re-implements them.
- **Cancellation.** The task file is checked once more right before `CALLING` is written: a task
  observed absent there is never called and its claim is dropped; a task observed present may be
  called, even if it is removed before `CALLING` lands (it then follows its outcome).
- **Recovery** (on start, after the lock): `PREPARED`/`NOT_DELIVERED` are retried only when the task
  file exists and the classifier says `ABSENT`, settled when the task was removed or a terminal result
  exists, parked otherwise; `CALLING` becomes `OUTCOME_UNKNOWN`, is counted at the restart time and
  answered with its staged interrupted body — "may or may not have reached (interrupted)" for an owner
  turn, `NON_OWNER_FAILED` for a collaborator or any other non-owner — unless a result exists, never
  resent; later phases finish publishing.
- **Settled ids.** `state/settled-ids` is append-only, one validated id per newline-terminated record.
  Order, under the lock: the claim is written `SETTLED` (its directory fsynced), then the id is
  appended in full and the ledger fsynced, and only then does the in-memory set change. A settled id
  is never called again, whatever its file looks like later. On start, every `SETTLED` claim missing
  from the ledger is re-appended, then the validated ledger is fsynced, before any claim is compacted
  (24 h after `SETTLED`). Only an unterminated suffix left by a crash is truncated; any terminated
  malformed record refuses naming its byte offset.
- **Errors.** Any claim or ledger I/O error (read, write, fsync, rename, listing) keeps the claim as
  it was, admits no work, compacts nothing, and exits 2 naming the path.
- **Rate journal.** `state/rate.jsonl`: one fsynced line per attempt event (`reserved`, `queued`,
  `unknown`, `unsent`) with the stable id `<task>:<attempt>:<kind>`. A reducer keeps one charge per
  attempt: `unsent` cancels it, `queued`/`unknown` count once at the later time, `reserved` alone
  counts nothing; replayed lines collapse. A torn last line is dropped on open, and lines older than
  two hours are compacted away by temp + rename.

Pacing: at least `--min-gap-seconds` (default 2) after the latest counted turn and at most
`--max-per-hour` (default 60) counted turns in a sliding hour, both read from the journal, so a restart
resets neither. A proven-unsent attempt counts nothing; an interrupted call counts at the restart time,
the latest it can have been queued.

## Limits

- No reply readback: the CLI only queues; the session posts itself, and its `posted` / `not posted`
  line is visible only on the session page.
- Any tool that raises a permission prompt inside the session stalls the queue: every turn queued
  after it waits until the owner answers it. In the probe, the connector's `room_list` raised one;
  `ag2_whoami`, `room_actions_describe` and `room_action_execute` did not, so the standing
  instructions allow only those three connector tools. Claude Code's own tools stay available; they
  ran without prompts in the probes, and one that starts prompting would stall the queue the same
  way. The adapter cannot detect a stall, because the CLI only reports that a turn was queued (tracked in the liveness
  issue), and nothing on the CLI side can answer the prompt. Fix it from the session's page: answer
  the prompt, and set the session's permission mode there if the page offers it.
- A session the owner closes or archives makes every turn fail, so every task gets a failure result
  until the adapter is pointed at a new session.
- Turns queue in one session in order; a long turn delays the ones behind it.
- Cost comes from the owner's Claude plan.

## Measured

By hand on Claude Code 2.1.302, not by this adapter: queuing a turn returned in about 1 s, and a turn sent on stdin reached the session as the exact text; in the probe
the session's `ag2_whoami` plus room post took 16 s. This adapter was tested only against a fake
`claude` executable: `python3 packages/ag2-sparrow/tests/test_claude_cloud_session_adapter.py` and the
owner's `test_task_lifecycle.py` (under the repository coverage gate through
`tests/ag2-sparrow-claude-cloud-session.test.py` and `tests/ag2-sparrow-task-lifecycle.test.py`).
