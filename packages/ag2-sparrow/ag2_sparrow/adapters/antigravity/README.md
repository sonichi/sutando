# Antigravity adapter

Runs Google's Antigravity CLI (`agy`) on this computer as an s.space agent, the way
`ag2-sparrow-claude-local` and `ag2-sparrow-codex-local` run Claude Code and Codex: people @-mention the
agent in a room and Antigravity answers there. For each task file Sparrow queues, the connector wakes
Antigravity over the Agent Client Protocol: it spawns `agy acp`, sends `initialize`, resumes the room's
Session with `session/load` (or opens one with `session/new`) and sends one `session/prompt`; a
non-owner's turn runs isolated instead (see Tiers and permissions). Stdlib only;
opt-in; the core never imports it.

Status: the handshake and the sign-in check were run against a real `agy` 1.5.9 (signed out); a full turn
(prompt, permission request, answer) against a signed-in `agy` has not been run yet.

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash      # Google's installer: ~/.local/bin/agy
agy                                                               # once: sign in with Google
ag2-sparrow-antigravity-local install --agent <@agent:server> --token-stdin [--cwd <dir>]
ag2-sparrow-antigravity-local install --work <dir> [--acp-command '<cmd>'] [--allow-non-owner]
ag2-sparrow-antigravity-local verify [--agent <@agent:server>] [--no-probe]
ag2-sparrow-antigravity-local uninstall [--agent <@agent:server>]
ag2-sparrow-antigravity-local list
```

The lifecycle is `connect_common`'s and the agent's own bridge is `connect_bridge`'s, exactly as for
`ag2-sparrow-codex-local` (see that adapter's README for the unit, the work-root lock, `run`, `debug-run`
and the `--token-env-file` / `--token-stdin` / `--work` roots). The ACP turn, the Session store, the
permission policy and the task framing are `ag2_sparrow/acp_host.py`'s, shared with the OpenClaw adapter.
What is Antigravity's own:

- **Sign-in.** Sign in once in a terminal (`agy`, then sign in with Google), or run `agy acp --login`
  (paste a code; works over SSH). agy keeps the sign-in in the OS keyring (the macOS Keychain) and
  `agy acp` reuses it; nothing here copies it. agy lists its sign-in methods at every `initialize`, signed
  in or not, so only `session/new` tells (it answers -32000 "Authentication required"): `install` opens a
  throwaway Session and refuses when agy is signed out; `verify` shows the same check as its `sign-in`
  row; a task that arrives after the sign-in lapsed gets a line saying Antigravity is not signed in.
- **install** resolves the ACP command's executable to an absolute path (refused when it is not on PATH)
  and records it with the Session working directory (`--cwd`, default `<work root>/workspace`, made when
  the connector starts) and the session store. `--acp-command` adds `agy acp` flags (for example
  `'agy acp --disable-telemetry'`); never add `--dangerously-skip-permissions` to an agent others can
  reach.

Why not Gemini CLI: Google stopped serving Gemini CLI for Google AI Pro, Ultra and free accounts on
2026-06-18 and points those users to Antigravity CLI. `--acp-command` takes any ACP agent's command line,
but only `agy acp` is supported here.

## The answer

The turn's final message is what Sparrow posts: as the reply in the message's thread for a message
(`event_type` `room.mention` or absent), or as the post an event's body asks for (`room.joined`,
`commons.summon`, any other well-formed type). A final message of exactly `NO_REPLY` posts nothing.
The text is posted with every delivery marker (attachment, redirect, skip, thread) neutralised. When a
turn fails, a message gets a line saying why (timeout, Antigravity stopped, not signed in) with any
partial text; an event posts nothing. No shell command is granted, so no reply command is either: the
final message is the only answer path.

## Tiers and permissions

Only the owner is served by default. Every other sender, an attested collaborator included, is refused
with one line unless `--allow-non-owner`, which admits them all. A non-owner's turn runs on the owner's agy
sign-in (the Google account and quota in the Keychain), and a turn whose tool call is denied can end with
no reply.

**The owner's turns** resume the room's Session in the working directory, with agy's own home
(`~/.gemini`: its settings, rules and context).

**A non-owner's turn** (anyone `--allow-non-owner` admits, collaborators included) runs isolated:

- a fresh temporary directory per task holds the Session's working directory and a separate agy home,
  passed as `agy acp --gemini-dir <dir>`; that home has only a `settings.json` whose permission rules deny
  every tool (`read_file(*)`, `write_file(*)`, `read_url(*)`, `execute_url(*)`, `download_url(*)`,
  `command(*)`, `mcp(*)`) and no global rules or context; the directory is removed after the turn. `HOME`
  and the rest of the environment are unchanged, so agy finds the sign-in in the Keychain as for the owner;
- a new Session for every task, never resumed and never recorded in the session store;
- the Session is put in a read-only mode with `session/set_mode` (`plan`, or a mode named `read-only`),
  picked from the `modes` that `session/new` returns. If agy offers no such mode, or refuses it, the turn
  is not run: a message gets a line saying so, an event posts nothing.

`--gemini-dir` is agy's flag: a custom `--acp-command` must accept it, or non-owner turns fail.

agy's `session/request_permission` is answered by `acp_host`'s policy, default deny:

- the owner only: a `read` or `edit` whose every path resolves (after `..`, `~` and symlinks) under the
  working directory; never when the working directory is the home directory or above it;
- the owner and team members only: one of the S.space MCP tools below, named by the server and tool the
  request carries;
- nothing else: no shell command, no other MCP tool, and no `execute`, `fetch`, `delete`, `move` or
  unclassified call.

For the owner's turns, agy applies its own permission rules first (an action they already allow runs
without asking), and `--dangerously-skip-permissions` makes it run tools without asking; never add that
flag to an agent others can reach.

Measured against a real agy 1.5.9 (signed out): with `--gemini-dir` an absolute path, agy keeps its app
data, `settings.json`, logs, conversations and `config/` under that directory, reads the settings file
there, and nothing is written to `~/.gemini`; `initialize` and the -32000 "Authentication required" answer
to `session/new` are the same as without it. Not yet measured, because no signed-in agy was available:
that a signed-in agy finds its sign-in with `--gemini-dir`, which modes `session/new` lists, and that
global rules are read only from the `--gemini-dir` home.

## S.space tools (Room Commons pages and room actions)

An owner's or a team member's turn (attested `access_tier` `owner` or `team`; the owners' rule of
2026-11-21) can create and edit Room Commons pages and run room actions, as the agent itself. Its Session
gets one MCP server, `ag2-space`, in `session/new` and `session/load`. A team turn stays isolated as above
(its own temporary agy home and directory, no file, command or fetch grant), and its agy home's deny rules
leave out `mcp(*)` only, so agy asks and the policy decides:

- **Identity.** The server is AG2 Space MCP, authorized as this agent by `ag2-sparrow-mcp acp` (see the
  package README, "Acting in AG2 Space"): the relay token in the agent's own token file is exchanged for a
  15-minute MCP access token for this agent, and every Action runs under the agent's own room membership and
  grants. Never the owner's sign-in and never another agent's. The exchange runs in a child process per
  turn, with only `AG2_DEVICE_ENV` (the token file `install` recorded as `TOKEN_ENV_FILE`, passed to the
  connector as `--token-env-file`) and a seat's own keys; the relay token stays in that child, and agy receives
  only the short-lived access token, as the server's `Authorization` header. agy's own environment is
  unchanged (the install's allowlist): no relay token, no token file path.
- **Transport.** HTTP when agy's `initialize` declares it (`agentCapabilities.mcpCapabilities.http`). agy
  1.5.9 and 1.5.13 declare no `mcpCapabilities` at all, so for them the server is **stdio**: the desktop app's
  `ag2-mcp-proxy` (`engine/ag2-mcp-proxy.mjs`, the same stdio proxy Claude Code and Codex cores run as their
  `s-space` server), named at install with `--mcp-proxy-command '<node> <path>/ag2-mcp-proxy.mjs'`. Each turn
  the connector runs `ag2-sparrow-mcp descriptor` in the same isolated child and writes the proxy's
  non-secret descriptor (0600, `state/ag2-mcp-descriptor.json`: mint and MCP URLs, the agent's token file and
  key, the secret's sha256 and the agent id). agy gets `{name: ag2-space, command, args, env:
  [AG2_MCP_DESCRIPTOR, AG2_MCP_LOG]}`: paths only, exactly what Claude Code's stdio entry carries. The proxy,
  agy's child, reads the relay token from the token file, mints as the agent, sends the relay token only to
  the mint and the access token only to the MCP endpoint, and scrubs its own environment.
- **Token file shape (stdio).** The proxy reads a `REMOTE_TASK_TOKEN=<relay>|<secret>` line, so the stdio
  path works with an agent `.env` passed as `--token-env-file`. A seat made by `--token-stdin` or `connect`
  keeps the token as a raw line in a separate file the proxy cannot read: `descriptor` refuses it and the
  turn runs without the tools (logged).
- **When none.** No token file (`--work` with a bridge you run), no HTTP and no `--mcp-proxy-command`, or a
  failed exchange: the turn runs without the tools and the log says why.
- **Which tools.** agy asks before each MCP call; the policy grants, to owner and team turns only, the
  `ag2-space` server's `ag2.whoami`, `room.list`, `room.inspect`, `room.actions.search`,
  `room.actions.describe`, `room.action.read`, `room.action.execute`, `room.action.execute_safe`,
  `room.task.list`, `room.task.create`, `room.task.update`, `operation.inspect`, `approval.inspect` and
  `space.action.read` (`mcp_auth.OWNER_TOOLS`; a name reported with `_` for `.` matches too). Never granted:
  `agent.link`, `agent.unlink`, `agent.setup.steps`, `relay.token.issue`, event subscriptions,
  `space.action.execute` and `ui.*`, any other server's tool, and any request that carries a command.
- **By identity, never by text.** The request is matched by the MCP server and tool name it carries in
  structured fields (`serverName`/`toolName` or `mcpServer`/`mcpTool`, on the tool call or in its `_meta`;
  `acp_host.MCP_IDENTITY_KEYS`), compared exactly; the title is never read. Which fields agy uses has not been
  measured: a request in another shape is denied, and its reason in `state/metrics.jsonl` lists its key names
  (never values), so it can be mapped.

**Guest turns get no S.space tools**: no MCP server in their Session, `mcp(*)` denied in their agy home, and
every MCP request refused. Read-only Actions are not offered to guests either: a read can still return another
room's Commons or tasks to someone the agent's owner never shared them with, so "no mutations" is kept by
giving no tools at all. A sender with no admitted tier is refused before agy starts, as before.
