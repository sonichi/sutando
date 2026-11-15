# Gemini adapter

Runs Gemini CLI on this computer as an s.space agent, the way `ag2-sparrow-claude-local` and
`ag2-sparrow-codex-local` run Claude Code and Codex. For each task file Sparrow queues, the connector wakes
Gemini over the Agent Client Protocol: it spawns `gemini --acp`, sends `initialize`, resumes the room's
Session with `session/load` (or opens one with `session/new`) and sends one `session/prompt`. Stdlib only;
opt-in; the core never imports it. `--runtime antigravity` runs Antigravity's `agy acp` the same way;
that runtime has not been checked against a real `agy` yet.

```bash
ag2-sparrow-gemini-local install --agent <@agent:server> --token-stdin [--cwd <dir>]
ag2-sparrow-gemini-local install --work <dir> [--runtime antigravity | --acp-command '<cmd>'] [--allow-non-owner]
ag2-sparrow-gemini-local verify [--agent <@agent:server>] [--no-probe]
ag2-sparrow-gemini-local uninstall [--agent <@agent:server>]
ag2-sparrow-gemini-local list
```

The lifecycle is `connect_common`'s and the agent's own bridge is `connect_bridge`'s, exactly as for
`ag2-sparrow-codex-local` (see that adapter's README for the unit, the work-root lock, `run`, `debug-run`
and the `--token-env-file` / `--token-stdin` / `--work` roots). The ACP turn, the Session store, the
permission policy and the task framing are `ag2_sparrow/acp_host.py`'s, shared with the OpenClaw adapter.
What is Gemini's own:

- **Sign-in.** Sign in once in a terminal (`gemini`, then Log in with Google), or put `GEMINI_API_KEY` in
  `~/.gemini/.env`. Gemini lists its sign-in methods at every `initialize`, signed in or not, so only
  `session/new` tells: `install` opens a throwaway Session and refuses when Gemini answers "not signed in";
  `verify` shows the same check as its `sign-in` row; a task that arrives after the sign-in lapsed gets a
  line saying Gemini is not signed in. The sign-in stays in `~/.gemini`; nothing copies it.
- **No relaunch.** By default Gemini CLI starts a second copy of itself (with more memory) on the same
  stdio, and a request the first copy already read is lost, so `initialize` can go unanswered. The
  connector always starts it with `GEMINI_CLI_NO_RELAUNCH=true`, which keeps it one process.
- **install** resolves the ACP command's executable to an absolute path (refused when it is not on PATH)
  and records it with the runtime, the Session working directory (`--cwd`, default `<work root>/workspace`,
  made when the connector starts) and the session store.

## The answer

The turn's final message is what Sparrow posts: as the reply in the message's thread for a message
(`event_type` `room.mention` or absent), or as the post an event's body asks for (`room.joined`,
`commons.summon`, any other well-formed type). A final message of exactly `NO_REPLY` posts nothing.
The text is posted with every delivery marker (attachment, redirect, skip, thread) neutralised. When a
turn fails, a message gets a line saying why (timeout, Gemini stopped, not signed in) with any partial
text; an event posts nothing.

Gemini does not say which command it wants to run in a permission request (no `rawInput`), so no reply
command can be granted exactly, as the OpenClaw adapter grants its own: the final message is the only
answer path.

## Tiers and permissions

The owner and an attested collaborator are served; any other non-owner is refused with one line unless
`--allow-non-owner`. Gemini's `session/request_permission` is answered by `acp_host`'s policy, default deny:

- the owner only: a `read` or `edit` whose every path resolves (after `..`, `~` and symlinks) under the
  working directory; never when the working directory is the home directory or above it;
- nothing else: no shell command, and no `execute`, `fetch`, `delete`, `move` or unclassified call.

This binds only what Gemini asks about. Gemini reads files in its workspace without asking, and a
`yolo` approval mode or an allow-list in `~/.gemini/settings.json` makes it run tools without asking, for
every tier. Keep Gemini's default approval mode for agents that serve anyone but the owner.
