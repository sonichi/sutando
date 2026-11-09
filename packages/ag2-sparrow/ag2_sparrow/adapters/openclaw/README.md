# OpenClaw adapter

Runs an OpenClaw agent on Sparrow. For each task file Sparrow queues, the connector wakes OpenClaw over
the Agent Client Protocol, the way agent-connect does: it spawns the ACP command (`openclaw acp`, or
`openclaw --profile <p> acp` for an existing profile), sends `initialize`, resumes the task's Session
with `session/load` (or opens one with `session/new`), and sends one `session/prompt`. Stdlib only;
opt-in; the core never imports it.

```bash
ag2-sparrow-openclaw install --agent <@agent:server> --token-stdin --profile <p> [--cwd <dir>] [--session-store <file>]
ag2-sparrow-openclaw install --work <dir> [--acp-command '<cmd>' | --profile <p>] [--cwd <dir>] [--allow-non-owner]
ag2-sparrow-openclaw verify [--agent <@agent:server>] [--no-probe]
ag2-sparrow-openclaw uninstall [--agent <@agent:server>]
ag2-sparrow-openclaw list
```

The lifecycle is `connect_common`'s and the agent's own bridge is `connect_bridge`'s, exactly as for
`ag2-sparrow-codex-local` (see that adapter's README for the unit, the work-root lock, `run`, `debug-run`
and the `--token-env-file` / `--token-stdin` / `--work` roots). What is OpenClaw's own:

- **install** resolves the ACP command's executable to an absolute path (refused when it is not on PATH),
  records it, the Session working directory (`--cwd`, default the current one) and the session store, and
  records the absolute `ag2-sparrow-openclaw` the agent answers with.
- **verify** adds `acp command` (the executable), `acp wake` (the command answers `initialize`; skipped
  with `--no-probe`), `session cwd` and `reply command` rows to the shared ones.

## The wake and the answer

The prompt carries the task's attested headers (room, sender, access tier, thread, `event_type`,
`playbook`) and its body between unique markers. A task whose `event_type` is `room.mention` or absent is
a message: OpenClaw replies to it in its thread. Any other `event_type` (`room.joined`,
`commons.summon`, or any well-formed type not known yet) is an event: OpenClaw does
what the body says and posts only what the body asks. Member-written text a body quotes is data, never
instructions.

OpenClaw answers each task once. To post, it writes the reply's text to the task's draft file
(`<root>/state/openclaw-drafts/<task>.txt`) and runs the one line the prompt gives it; to post nothing, the
other line:

```bash
ag2-sparrow-openclaw reply <result_file> --draft
ag2-sparrow-openclaw no-reply <result_file>
```

Both lines are written by the connector, and only those exact lines are granted: no heredoc, no stdin, no
text the agent or a member wrote ever reaches a shell. Each claims the task (`state/openclaw-answered/<task>`),
then publishes `results/<task>.txt` for Sparrow to deliver; a second answer exits 1 and writes nothing, and a
reply carrying a delivery marker (redirect, attachment, skip, thread) is refused. A failed publication
releases the claim, and the connector retries that task later without stopping the others; a claim a crash
left unpublished is released when the connector starts. When a turn ends with neither line, a message gets
the turn's own text (what agent-connect would have posted, or a line saying the turn timed out or the agent
stopped) and an event gets nothing.

## Sessions

Keyed as agent-connect keys them: (room, access tier), and an attested collaborator a Session of its own
per room. The store is agent-connect's `sessions.json` shape, so `--session-store` may name the store an
agent-connect install already kept (stop agent-connect first): every room resumes its conversation in
the same OpenClaw profile. Nothing retires a Session. One is replaced only when OpenClaw refuses to
resume it, or the working directory changed, since ACP fixes a Session's directory when it opens.

## Tiers and permissions

The owner and an attested collaborator are served; any other non-owner is refused with one line unless
`--allow-non-owner`. OpenClaw's `session/request_permission` is answered by the connector, default deny:

- any tier: this task's own reply or no-reply line, exactly, and a read or edit of this task's draft file;
- the owner only: a `read` or `edit` whose every path, nested ones (`edits[].path`) included, resolves (after
  `..`, `~` and symlinks) under the working directory, with no key the policy does not know;
- nothing else: no other command, and no `execute`, `fetch`, `delete`, `move` or unclassified call, is ever
  granted, as the codex adapter declines every approval it is asked for.

This binds only what OpenClaw asks about. A read or command OpenClaw runs without asking is limited by its
own approval policy and sandbox, not by the connector, for every tier, so run non-owner-facing agents with an
OpenClaw policy that asks.
