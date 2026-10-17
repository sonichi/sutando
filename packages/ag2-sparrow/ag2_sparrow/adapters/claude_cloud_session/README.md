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

`<dir>/tasks/task-*.txt` in, `<dir>/results/task-<id>.txt` out,
`<dir>/state/{done,sends.json,sending/,connector.pid}`. One adapter per work dir
(`state/connector.pid`, liveness-checked; exit 3 if another holds it), one turn queued at a time,
taken in the task protocol's priority order (`priority:` urgent, normal, low; oldest first within a
priority). The loop, payload layout, result lines and hourly cap are the shared
[`ag2_sparrow/relay_loop.py`](../../relay_loop.py), which the routine adapter uses too; this adapter keeps only the
CLI call, the pre-send marker and the pacing gap.

## Setup (once)

1. Create the session interactively: `claude --cloud "AG2 Space agent"`. It prints
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
[sparrow task] Follow your standing AG2 Space instructions for this message: …
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

**Non-owner tiers are refused by default**, with the routine adapter's plain line ("This agent only
answers its owner here."): the session holds the owner's connector and there is no narrower scope.
`--allow-non-owner` queues them anyway, bounded only by the note and the standing instructions.

## Results

| outcome | result file |
|---|---|
| any JSON record with `"ok": true` anywhere in stdout or stderr, whatever else the output or exit code says | `[no-send]` + `# claude cloud session: <url>`; with `--result-mode echo`, `Sent to the cloud agent: <url>` (owner tasks only; non-owner rooms always get `[no-send]`) |
| no receipt, and a non-zero exit whose output proves the CLI never reached the service (DNS failure, refused connection), or the executable failed to start | retried after `--backoff` × 2ⁿ (cap `--backoff-max`), up to `--max-retries` (default 2), then "could not be reached … after several tries" |
| no receipt otherwise: any other non-zero exit, exit 0 without one, or a timeout (`--timeout`, default 60 s; a receipt printed before the timeout still counts as queued) | "may or may not have reached the cloud agent …; it was not retried" — never resubmitted |

A non-owner task's failure line never carries the exit code. Before each call the adapter writes
`state/sending/<task_id>` (`sending`), rewrites it to `outcome` once the outcome is counted or to
`unsent` before releasing the reservation of a proven-unsent call, and removes it only with the
result. A crash mid-send is answered on restart with the "may or may not have reached" line instead
of a second queued turn (at most once); a crash during a proven-unsent cleanup is retried once.

Pacing: at least `--min-gap-seconds` (default 2) between queued turns and at most `--max-per-hour`
(default 60) in a sliding hour, kept in `state/sends.json` so a restart does not reset either. The cap
counts turns that were queued or may have been; an attempt proven unsent does not use it. Each turn
is counted before the CLI call, keyed by its task id, and dropped again only when the call proves it
was never sent, so a crash right after a send cannot lose the count and a restart cannot count the
same task twice. Once the outcome is known (queued or ambiguous) the entry moves to that time, so the
gap counts from when the turn was queued, not from when the call started. On restart, a task that
already holds an entry or a pre-send marker is settled first and skips both gates. A marked task gets
its "may or may not have reached" result at once; if the crash came before its outcome was recorded,
its one entry moves to the restart time, the latest the turn can have been queued, so the next turn
still waits the full gap and the hour. A reserved but unmarked task, or one whose marker says
`unsent`, is sent at once. None of them gains a second entry.

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
shared loop's `test_adapter_relay_loop.py` (under the repository coverage gate through
`tests/ag2-sparrow-claude-cloud-session.test.py` and `tests/ag2-sparrow-relay-loop.test.py`).
