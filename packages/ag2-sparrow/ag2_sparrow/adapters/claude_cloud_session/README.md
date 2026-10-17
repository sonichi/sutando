# Claude cloud session adapter

Feeds Sparrow/Sutando inbox tasks into **one persistent** Claude Code cloud session, as turns. The
session runs in Anthropic's cloud with the AG2 Space connector, keeps its memory across messages, has
the full Claude Code toolset, and posts each reply into the room itself. Unlike the
[routine adapter](../claude_routine/README.md), which starts a fresh one-shot session per message,
every message here lands in the same session. This relay still has to run on a machine. Stdlib only;
opt-in; the core never imports it.

```
python3 -m ag2_sparrow.adapters.claude_cloud_session.session_adapter \
  --work <dir> --session-id session_... [--claude <path>] [options]
```

`<dir>/tasks/task-*.txt` in, `<dir>/results/task-<id>.txt` out,
`<dir>/state/{done,sends.json,sending/,connector.pid}`. One adapter per work dir
(`state/connector.pid`, liveness-checked; exit 3 if another holds it), one turn queued at a time.

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
4. Run the adapter with `--session-id session_…`. No token goes in the vault: the `claude` CLI's own
   login on this machine is the credential.

## Wire

Per task: `claude -p "<text>" --cloud <session_id> --output-format json`, run as an argv list (never a
shell; the text is one argv element), with only `PATH`, `HOME`, `USER` (the macOS Keychain login needs
it) and `LANG` in the child's environment. `--claude` picks the executable (default `claude` on PATH;
the adapter refuses to start, exit 2, when it does not resolve). The CLI returns in about a second with
`{"ok":true,"session_id":"…","url":"…"}` and exit 0 once the turn is **queued**; it does not return the
reply.

Text:

```
[sparrow task] Follow your standing AG2 Space instructions for this message: …
room=<channel_id> thread=<thread_root or none> tier=<owner|team|guest> collaborator=<true|false> task_id=<id>
note: <NON_OWNER_NOTE>            (non-owner tiers only)
<UNTRUSTED_TASK_<nonce>>
<message body, untrusted>
</UNTRUSTED_TASK_<nonce>>
<bridge-written guardrail lines, when the task file has them>
```

Everything after the first line is the routine adapter's payload, built by the same function: fresh
nonce per turn, attested tier and collaborator, header whitespace removed, the Hatch `NON_OWNER_NOTE`
for non-owner tiers. The whole text is at most 65,536 characters; only the body is cut, ending in
`[truncated]`. A task with no room is not queued.

**Non-owner tiers are refused by default**, with the routine adapter's plain line ("This agent only
answers its owner here."): the session holds the owner's connector and there is no narrower scope.
`--allow-non-owner` queues them anyway, bounded only by the note and the standing instructions.

## Results

| outcome | result file |
|---|---|
| exit 0 and `"ok": true` | `[no-send]` + `# claude cloud session: <url>`; with `--result-mode echo`, `Sent to the cloud agent: <url>` (owner tasks only; non-owner rooms always get `[no-send]`) |
| non-zero exit whose output proves the CLI never reached the service (DNS failure, refused connection), or the executable failed to start | retried after `--backoff` × 2ⁿ (cap `--backoff-max`), up to `--max-retries` (default 2), then "could not be reached … after several tries" |
| any other non-zero exit, exit 0 without `"ok": true`, or a timeout (`--timeout`, default 60 s) | "may or may not have reached the cloud agent …; it was not retried" — never resubmitted |

A non-owner task's failure line never carries the exit code. Before each call the adapter writes
`state/sending/<task_id>` and removes it only with the result, so a crash mid-send is answered on
restart with the "may or may not have reached" line instead of a second queued turn (at most once).

Pacing: at least `--min-gap-seconds` (default 2) between queued turns and at most `--max-per-hour`
(default 60) in a sliding hour, kept in `state/sends.json` so a restart does not reset either.

## Limits

- No reply readback: the CLI only queues; the session posts itself, and its `posted` / `not posted`
  line is visible only on the session page.
- A session the owner closes or archives makes every turn fail, so every task gets a failure result
  until the adapter is pointed at a new session.
- Turns queue in one session in order; a long turn delays the ones behind it.
- Cost comes from the owner's Claude plan.

## Measured

By hand on Claude Code 2.1.302, not by this adapter: queuing a turn returned in about 1 s; in the probe
the session's `ag2_whoami` plus room post took 16 s. This adapter was tested only against a fake
`claude` executable: `python3 packages/ag2-sparrow/tests/test_claude_cloud_session_adapter.py` (under the
repository coverage gate through `tests/ag2-sparrow-claude-cloud-session.test.py`).
