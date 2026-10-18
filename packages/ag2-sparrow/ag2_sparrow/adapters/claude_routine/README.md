# Claude routine adapter

Forwards Sparrow/Sutando inbox tasks to a [Claude Code routine](https://code.claude.com/docs/en/routines)
through its API trigger. The routine runs in Anthropic's cloud with the AG2 Space connector and posts
the reply into the room itself. Stdlib only; opt-in; the core never imports it.

```
python3 skills/secret-vault/secret-vault.py env CLAUDE_ROUTINE_TOKEN -- \
  python3 -m ag2_sparrow.adapters.claude_routine.routine_adapter --work <dir> --trigger-id trig_... [options]
```

`<dir>/tasks/task-*.txt` in, `<dir>/results/task-<id>.txt` out, `<dir>/state/{done,fires.json,hold.json,collaborator-turns.json}`.
One process per work dir, through the package's `holder_lock` (its first act; exit 3 naming the holder if another adapter of any kind holds the dir, exit 2 for any other lock failure; released on exit and on SIGTERM), one fire in flight.
Tasks are taken in the task protocol's priority order (`priority:` urgent, normal, low; oldest first
within a priority). The loop, payload layout, result lines and hourly cap live in the shared
[`ag2_sparrow/relay_loop.py`](../../relay_loop.py), which the cloud session adapter uses too.

## Setup

1. Create a routine at claude.ai/code/routines with the AG2 Space connector (remove every connector
   it does not need), and paste the saved prompt from [`routine-prompt.md`](routine-prompt.md).
2. Add an **API** trigger, copy the `trig_...` id from its URL, generate the token.
3. Store the token in the vault: `vault set CLAUDE_ROUTINE_TOKEN <token>` over a bridge, then run the
   adapter under `secret-vault.py env` as above. The token is read only from the env var named by
   `--token-env` (default `CLAUDE_ROUTINE_TOKEN`), never from argv, and never logged. With the var unset
   or empty the adapter refuses to start (exit 2).

## Wire

`POST https://api.anthropic.com/v1/claude_code/routines/<trig_id>/fire` with `Authorization: Bearer <token>`,
`anthropic-version: 2023-06-01`, `Content-Type: application/json`, body `{"text": "<payload>"}`
(at most 65,536 characters; longer payloads are cut and end in `[truncated]`). The API reference says the
`anthropic-beta: experimental-cc-routine-2026-04-01` header is accepted but not required, so it is not sent.
A 200 returns `claude_code_session_id` and `claude_code_session_url`. Inside the session the text arrives
wrapped as an untrusted `<routine-fire-payload>` block; the saved prompt is what opts in to acting on it.

Payload:

```
room=<channel_id> thread=<thread_root or none> tier=<owner|team|guest> collaborator=<true|false> task_id=<id>
note: <NON_OWNER_NOTE>            (non-owner tiers only)
<UNTRUSTED_TASK_<nonce>>
<message body, untrusted>
</UNTRUSTED_TASK_<nonce>>
<bridge-written guardrail lines, when the task file has them>
```

The nonce is fresh per fire, so a body cannot close the span early; the bridge-written lines sit after
the closing marker and stay operator text. Only the body is cut to fit the length limit.

Tier and collaborator come from the attested headers (`task_io.attested_headers`, via
the package-level `task_io.parse_task` every adapter shares), never from message text; unknown tiers become `guest`, and
`collaborator=true` only at `team`. Header values have whitespace removed, so a value cannot add a line.
The note is the shared `ag2_sparrow.task_io.NON_OWNER_NOTE`, the same text every adapter renders; `routine-prompt.md`
carries the same text so the rules do not depend on the payload. A task with no room is not fired.

**Who is served.** Admission is the shared `task_io.admission` gate over the attested headers: the owner,
an attested collaborator (Team), nobody else. A non-owner without collaborator access (Guest, and any
collaborator claim the broker did not attest) is refused before anything is sent, with a plain-text result
("This agent only answers its owner here."), unless `--allow-non-owner` is passed — which fires such tasks
at the same routine and connector scope as the owner, bounded only by the prompt's note, and should only be
true when this specific routine's connector scope is meant to be reachable by them. An attested collaborator
(the broker's `collaborator: true` on the team tier, from the parsed headers, never the message text) never
reaches the routine at all: with `--allow-collaborator` it is served on the isolated local turn below;
without it, it gets the same refusal line. `--allow-non-owner` does not imply `--allow-collaborator`. The
owner path is unchanged.

**The isolated collaborator turn.** `<--claude> -p --safe-mode --restricted --strict-mcp-config
--no-session-persistence --tools Read,Grep,Glob --output-format json`, the same payload on stdin (one lead
line naming the situation, then the note, `collaborator=true`, never `owner_dm`), in a fresh empty temp
directory removed afterwards, with an environment of only `PATH`, `HOME`, `USER`, `LANG` and
`CLAUDE_CONFIG_DIR`, and the adapter's `--timeout`. What it can reach: the CLI's login and the three read
tools over an empty directory. What it cannot: the routine and its connector, any session or history (no
`--cloud`, no `--resume`, and the turn itself is never saved, so nothing can resume it), Bash or
code-running tools, the web, MCP servers, CLAUDE.md, hooks, skills, settings, or the work dir. The JSON
`result` string becomes the task's result as **inert text**: every result-delivery marker the shared
parser knows (`[no-send]`, `[REPLIED]`, `[deduped:]`, `[channel:]`, `[thread:]`, `[thread]`, `[reply:]`,
`[dm-only]`, `[file:]`/`[send:]`/`[attach:]`) is quoted in the parser's own form (`[ no-send]`) before
publication and checked by the parser again, so the bridge executes nothing from it and posts it once in
the originating room. Any failure (non-zero exit, no `result`, timeout, no temp dir, missing CLI) gets the
generic "could not be sent" line — one try, no retry. These turns have their own cap,
`--max-collaborator-per-hour` (1 or more, default 10, `state/collaborator-turns.json`), and their own gate:
a collaborator waits only on that cap, never on `--max-per-hour` or a 429 hold, and an owner task waits
only on its own gates, never on the collaborator cap.

## Results: why `[no-send]`

The routine posts the reply, so the local bridge must not post a second one. A successful fire writes:

```
[no-send]
# claude routine session: https://claude.ai/code/session_...
```

`--result-mode echo` writes `Sent to the cloud agent: <url>` instead, which the bridge does deliver; use
it only to debug the wiring. Echo applies to owner tasks only: a non-owner room still gets `[no-send]`,
so the session URL is never posted where others read it. A fire refused with a 4xx other than 429 writes
a one-line plain-text failure for an owner task (status and error type); a non-owner task (only reachable
at all with `--allow-non-owner`) gets a generic "could not be sent" line instead — the HTTP status and
error type are the owner's connector's state, not something a non-owner sender learns.

## Errors and limits

| response | action |
|---|---|
| 200 | result written, task done (even if the body is unreadable: the session exists) |
| 429 | every fire held for `Retry-After` (seconds or HTTP date, clamped 1 s–1 h, default 60 s; kept in `state/hold.json`, so a restart honours it), then the same task is retried; after `--max-rate-limited` (default 5) 429s the task gets a plain-text failure result |
| other 4xx | plain-text failure result, task done |
| connection refused, DNS failure, 503 with `Retry-After` | the fire provably was not accepted: retried after `--backoff` × 2ⁿ (cap `--backoff-max`, at least `Retry-After`), up to `--max-retries` (default 2), then a failure result |
| timeout, reset, any other 5xx or network error | the fire may have been accepted: never retried; the result says the message may or may not have reached the cloud agent |

The endpoint has no idempotency key, so a retry after an accepted fire would start a second session
and a second room post; that is why only provably unsent fires are retried. One window remains: a crash
between a 200 and the result write leaves the task unanswered locally, and it is fired again on restart.

Hourly caps on the server: 30 fires per routine (shared with **Run now**), 100 API fires per account.
Locally `--max-per-hour` (default 25, at most 30) caps fires in a sliding hour kept in `state/fires.json`,
so a restart does not reset it; entries dated in the future (a clock stepped back) are dropped.
`--api-base` must be https (`--allow-insecure-api-base` exists for the local test server only).

## Lock, platform and upgrades

This adapter's single-instance check is the package's one-process lock, `ag2_sparrow.holder_lock` (see the package README). So:

- **POSIX only:** Linux and macOS; on Windows the adapter refuses to start with `LockError("unsupported platform: holder_lock is POSIX-only")`.
- **Network-mounted home:** if the service user's home directory is on a network or FUSE filesystem, the adapter refuses to start with `LockError("not a local filesystem")`. Remedy: run it as a user with a local home directory.
- **Upgrade order: stop → install → start.** Stop the adapter on its work root, install, then start it. A start while the old adapter still runs there is refused (exit 3) with the pid to stop.
- **Supported upgrade paths: the Hatch installer and the keepawake self-upgrade**, both stop → install → start. Anything else (a direct `pip` or `pipx` install or upgrade, a rollback, a process supervisor restarting a unit, or a work root the upgrader does not know) must stop the adapters by hand before installing; `holder_lock.status(<work root>)` reports what is running there.

## Trust

The bearer token is the boundary: anyone holding it can fire the routine with any first line, including
`tier=owner`. Keep it in the vault, give the routine only the connectors it needs, and rotate it from the
trigger modal if it leaks.

## Measured

Live, by the routine proof that preceded this adapter (not by this adapter): fire to reply posted in the
room 19.3 s; run done +21 s. This adapter was tested only against a local fake server:
`python3 packages/ag2-sparrow/tests/test_claude_routine_adapter.py` (under the repository coverage gate
through `tests/ag2-sparrow-claude-routine.test.py`).
