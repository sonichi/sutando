# Antigravity adapter

Runs Google's Antigravity CLI (`agy`) on this computer as an s.space agent, the way
`ag2-sparrow-claude-local` and `ag2-sparrow-codex-local` run Claude Code and Codex: people @-mention the
agent in a room and Antigravity answers there. For each task file Sparrow queues, the connector wakes
Antigravity over the Agent Client Protocol: it spawns `agy acp`, sends `initialize`, resumes the room's
Session with `session/load` (or opens one with `session/new`) and sends one `session/prompt`; a
guest's turn runs isolated instead (see Tiers and permissions). Stdlib only;
opt-in; the core never imports it.

Status: the handshake and the sign-in check were run against a real `agy` 1.5.9 (signed out); a full turn
(prompt, permission request, answer) against a signed-in `agy` has not been run yet.

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash      # Google's installer: ~/.local/bin/agy
agy                                                               # once: sign in with Google
ag2-sparrow-antigravity-local install --agent <@agent:server> --token-stdin [--cwd <dir>]
ag2-sparrow-antigravity-local install --work <dir> [--acp-command '<cmd>'] [--allow-non-owner] [--allow-owner-shell-web]
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
The owner's text keeps its delivery markers, as a Claude Code or Codex result does: a first line
`[channel: !room:server]` posts in that room, `[thread]` answers in a thread of its own, and
`[file: <path>]` attaches a file, but only a regular file under the working directory (rewritten to its
absolute path; any other is neutralised and said not attached). Anyone else's text is posted with every
delivery marker (attachment, redirect, skip, thread) neutralised, so a non-owner cannot make the agent post
in another room or attach a file. When a
turn fails, a message gets a line saying why (timeout, Antigravity stopped, not signed in) with any
partial text; an event posts nothing. There is no reply command: the final message is the only answer
path.

The gateway ends an owner's task with a `===SKILL INSTRUCTIONS===` prelude written for a core that has a
shell: run a room-read command, then write the result to `results/<task>.txt`. Neither is granted here, and
a signed-in agy told to write that file asked to, was refused and ended its turn with no text (measured
2026-11-19/20: three owner turns, each one refused `File results/<task>.txt` request, each posted "finished
without an answer"). So the prelude's steps are not passed to agy; only its ADDRESSING step is, with
`NO_REPLY` in place of `[no-send]`.

When a message's turn completes with no text, the line posted says what the turn did instead, in fixed
words and counts only: the refused permission requests by kind (file change, file read, command, web
fetch, other), the tool calls it made, and a stop reason other than `end_turn` (`max_tokens` or
`max_turn_requests`, else "other"). No text from agy's request reaches the room: the policy's reasons quote
it (a key, a kind, a path), so they are kept locally. Each task's record in `state/metrics.jsonl` carries
`detail` (the stop reason), `permissions` (each request's title, category and why it was allowed or
refused), `updates` (the turn's `session/update`s counted by type) and `answer_chars`.

## What people send

The files the bridge downloaded for the message (its attested `attachments:` header) go with the turn:
each PNG, JPEG, GIF or WebP of at most 5 MB (known by its first bytes) as an ACP image block in the
prompt, when agy declares `promptCapabilities.image` (agy 1.5.13 does). On the owner's turn (Team's too) each file is
also copied, read-only (0400 in a 0500 folder of the turn's own, removed after it), and the folder is
passed as `additionalDirectories` when agy declares it; the policy grants reads there and refuses every
change. Only that task's files are copied, never the bridge's media folder. A non-owner's isolated turn gets
the images only: no folder, and its deny-all rules stay.

## Tiers and permissions

The tier is the shared `task_io.turn_policy`'s, from the attested headers only (a tier claimed in the
message body changes nothing); the adapter only maps its verdict to a turn (`TURNS`):

| `turn_policy` verdict | Who | Turn |
| --- | --- | --- |
| owner | the owner | the owner's turn |
| collaborator | an attested collaborator (Team: `team` + the broker's `collaborator: true`) | the owner's turn, with a Session of their own per room |
| restricted | a guest, plain team (no collaborator stamp), any unknown tier | the isolated read-only turn below |
| refused | (not reached here: every non-owner who is not a collaborator is served restricted, as Sutando answers them) | one refusal line, agy not started |

No flag is needed for any of them; `--allow-non-owner` still parses and changes nothing.

Every turn runs on the owner's agy sign-in (the Google account and quota in the Keychain): giving someone
Collaborator access to this agent lets them spend that quota, and their turns may read and edit the
working directory as the owner's do. A turn whose tool call is denied can end with no reply.

The gateway writes a Sutando tier block into every non-owner task (`team_guardrail.py`). It tells a core to
write `results/<task>.txt` and, for a guest, to delegate to `codex exec`; agy can do neither, and its final
message is what Sparrow posts. So the adapter says those steps as "your final message" and "this turn is
already the read-only sandbox", and keeps the scope, the DO / DO NOT lists, the trust boundary and the
hand-off to the owner verbatim (`tier_block_lines`). The final reply is scanned by the shared bridge
(`_guarded_result_body`) for every tier but the owner, as for any adapter.

**The owner's turn** (the owner and Team, decided by the owner on 2026-11-21) runs in the working
directory with agy's own home (`~/.gemini`: its settings, rules and context) under the owner's permission
policy below. The owner resumes the room's Session; each collaborator resumes a Session of their own for
the room (keyed by their user id), so no one resumes another's conversation. A collaborator is still framed
as not the owner.

**A restricted turn** (a guest, plain team, an unknown tier) runs isolated:

- a fresh temporary directory per task holds the Session's working directory and a separate agy home,
  passed as `agy acp --gemini-dir <dir>`; that home has only a `settings.json` whose permission rules deny
  every tool (`read_file(*)`, `write_file(*)`, `read_url(*)`, `execute_url(*)`, `download_url(*)`,
  `command(*)`, `mcp(*)`) and no global rules or context; the directory is removed after the turn. `HOME`
  and the rest of the environment are unchanged, so agy finds the sign-in in the Keychain as for the owner;
- the sender's own attachments to this task (the attested `attachments:` header only, never a path in the
  message) are copied read-only into `attachments/` in that working directory, and the prompt lists the
  copies. Only then are file reads moved from deny to ask in that home's rules, and the adapter grants a
  `read` of exactly those copies, nothing else: not the original under the bridge's media directory, not
  another task's file, not any other path, and never an edit. A locator that is a symlink, not a regular
  file, over 25 MB, not the size the header says, or not a local absolute path is not copied;
- a new Session for every task, never resumed and never recorded in the session store;
- the Session is put in a read-only mode with `session/set_mode` (`plan`, or a mode named `read-only`),
  picked from the `modes` that `session/new` returns. If agy offers no such mode, or refuses it, the turn
  is not run: a message gets a line saying so ("Antigravity did not take this: it answers non-owners only
  in a read-only session mode, and no read-only session mode is offered (offered: …)."), an event posts
  nothing.

## The model

agy's ACP server has a model per Session. Each of the owner's turns (Team's included) hands the connector
the Session's `models` (`availableModels`, `currentModelId`), which it keeps in `state/model.json`; when
the owner has picked one (`wanted` there), the turn first calls `session/set_model`, so the pick applies
from the next message with nothing restarted. A refused `session/set_model` is logged, told to the owner
(below) and the turn runs on the Session's own model; the model a turn ran on is `model` in
`metrics.jsonl`, and `model_switch` there is `switched` or `not-switched` on the turn that settled a
pick. Guest turns are not switched.

The owner picks from a card, and asks for it in their own words. Each of the owner's turns (not a
collaborator's) ends with one more line telling agy to answer a question about its model with the mark
`[model-card]` alone. When a completed turn's whole answer is that mark, the connector posts the card in
place of the answer if the ask came from the owner's DM, and otherwise says "I can switch models from
our direct message. Ask me there." The mark from anyone else, or inside a longer answer, is ordinary
text. The fixed message `/model` in that DM does the same without a turn: the
connector queues an agent panel card (`connector_cards`; the bridge posts it) with one reply button per
model, at most six. A tap comes back as a task with `card_click`; it is judged by the core's own
`agent_card.click_verdict` against this agent's ledger of the cards it posted, so it acts only when the
owner tapped a card this agent posted, in that DM. It then records the pick and posts a card with no
buttons saying the switch comes with the next message, which retires the first card's buttons. After
the next owner turn that opens a Session, one more card in that DM says what happened: "Switched. I'm
now using X.", or that the switch was refused and which model is still in use (the refused pick is
dropped, not retried). The owner is never left to infer a switch from silence. Any other tap
gets the fixed "couldn't match that button" line and reaches neither agy nor the record. `/model` before
any turn has run answers that the models are not known yet; in a shared room it is ordinary words.

Not measured against a real agy: `models` on `session/new` and `session/set_model {sessionId, modelId}`
are the Agent Client Protocol's shapes, and agy 1.5.14's ACP server carries handlers named for them
(`handleSessionSetModel`, `currentModelID`, `SessionModelState`), read from the executable, not called.
Whether `session/load` also returns `models` is not known; if it does not, a resumed Session is still
switched, and the list refreshes on the next new Session. That the bridge's room call carries
`space.ag2.card` to the client is likewise untested against a real gateway.

`--gemini-dir` is agy's flag: a custom `--acp-command` must accept it, or guest turns fail.

agy's `session/request_permission` is answered by `acp_host`'s policy, default deny:

- the owner's turn only (the owner and Team), and only on an install with `--allow-owner-shell-web`
  (recorded as `ALLOW_OWNER_SHELL_WEB=1`; off by default, so an install that never turned it on does not
  change on update; one that did changes, see "Upgrading with the switch on" below):
  any shell command and any `execute`, `fetch` or `search` call, as a local Claude Code agent runs commands
  and uses the web for its owner, and as claude-local and codex-local give an attested collaborator the
  owner's tools. Plain team never gets it. `verify` prints `INFO owner shell and web: on|off`;
- the owner's turn only (the owner and Team): a `read` or `edit` whose every path resolves (after `..`, `~`
  and symlinks) under the working directory; never when the working directory is the home directory or above
  it. agy's own argument names (`Path`, `Content`, `Overwrite`, `Append`, `Target`, `Replacement`,
  `AllowMultiple`, `Edits`, `StartLine`, `EndLine`, and the older `TargetFile`, `CodeContent`,
  `AbsolutePath`, ...) are recognised; any other key refuses the request;
- the owner's turn only (the owner and Team): one of the S.space MCP tools below, named by the server
  and tool the request carries;
- nothing else: no command or web call for plain team or a guest, no other MCP tool, and no
  `delete`, `move` or unclassified call for anyone.

| Turn | Commands, fetch, search | Read or edit under the working directory | Anything else |
| --- | --- | --- | --- |
| owner | granted with `--allow-owner-shell-web`, else refused | granted | refused |
| Team (an attested collaborator, in the owner's turn) | as the owner: granted with `--allow-owner-shell-web`, else refused | granted | refused |
| guest (isolated) | refused | refused | refused |

With `--allow-owner-shell-web`, the working-directory bound on `read` and `edit` is no longer a security
boundary for the owner's turns: a command can read or write anything the owner's account can.
That holds for an attested collaborator's turn too: once a collaborator has shell commands, nothing stops
them from reading the agent's relay token file directly (mode 600 only keeps out other accounts, not
the owner's), the owner's other credentials, or any file the owner can read. Turn the switch on
only if you trust every attested collaborator of this agent with the owner's account and its secrets.

**Upgrading with the switch on.** Before this release the switch was for the owner's turns only. An install
that already has `ALLOW_OWNER_SHELL_WEB=1` gives the same shell commands and web to every attested
collaborator of the agent as soon as it is upgraded, without asking the owner again. If that is not what you
want, turn the switch off before upgrading: `uninstall`, then `install` again without
`--allow-owner-shell-web` (a plain `install --agent <id>` reuses the recorded switch and keeps it on). Or
review who is an attested collaborator of this agent first and remove anyone you would not hand the owner's
account to.

For the owner's turns (Team's included), agy applies its own permission rules first (an action they already allow runs
without asking), and `--dangerously-skip-permissions` makes it run tools without asking; never add that
flag to an agent others can reach.

**What the policy above covers, and what it does not.** The table governs only the requests agy sends over
ACP (`session/request_permission`). Native operations agy runs without such a request are decided by the
owner's own agy settings in `~/.gemini`, not by this adapter. Its built-in web search is one of them: in a
signed-in check it ran with no ACP request. So the owner and Team turns may use built-in search whatever
`--allow-owner-shell-web` says (Chi OK'd this scope). The guest turn runs with its own agy home, whose
deny-all rules are agy's own settings rather than this adapter's ACP policy; whether they also stop built-in
search has not been verified on a signed-in agy.

Measured against a real agy 1.5.9 (signed out): with `--gemini-dir` an absolute path, agy keeps its app
data, `settings.json`, logs, conversations and `config/` under that directory, reads the settings file
there, and nothing is written to `~/.gemini`; `initialize` and the -32000 "Authentication required" answer
to `session/new` are the same as without it. Not yet measured, because no signed-in agy was available:
that a signed-in agy finds its sign-in with `--gemini-dir`, which modes `session/new` lists, and that
global rules are read only from the `--gemini-dir` home.

## A Gemini API key instead of the Google sign-in

agy ignores `GEMINI_API_KEY` in its environment while a Google sign-in exists. When the owner saves a Gemini
API key for this agent in the app (its Antigravity connection, "Use a Gemini API key"), the app keeps it in
the macOS Keychain (service `space.ag2.app.agy-gemini-key.<agent id>`, account `GEMINI_API_KEY`, the value
as is or base64 JSON `{"tag", "value"}`). After `initialize`, when agy lists `gemini-api-key` in
`authMethods`, the owner's and a collaborator's turns (and `verify`'s sign-in check) send ACP
`authenticate {"methodId": "gemini-api-key", "_meta": {"apiKey": <key>}}`: that one agy process runs on the
key, the Google sign-in is left untouched. Guest turns (`--gemini-dir`) never get the key. The key goes only
into that request on agy's stdin: never argv, environment or logs. Each saved Session records its auth mode
(`auth`: `sign-in` or `gemini-api-key`, none = `sign-in`); a Session opened under the other mode is not
resumed (resuming a sign-in Session still bills the Google account's quota), a new one is opened.
`AG2_AGY_GEMINI_KEY=0` in the connector's environment turns this off. See `gemini_key.py`.

## S.space tools (Room Commons pages and room actions)

The owner's turns and Team's (an attested collaborator: `access_tier: team` with `collaborator: true`, as
#6645 serves them; the owners' rule of 2026-11-21) can create and edit Room Commons pages and run room
actions, as the agent itself. Their Session gets one MCP server, `ag2-space`, in `session/new` and
`session/load`. Team runs in the owner's turn (no command or fetch grant); agy asks before each MCP call
and the policy decides:

- **Identity.** The server is AG2 Space MCP, authorized as this agent by `ag2-sparrow-mcp acp` (see the
  package README, "Acting in AG2 Space"): the relay token in the agent's own token file is exchanged for a
  15-minute MCP access token for this agent, and every Action runs under the agent's own room membership and
  grants. Never the owner's sign-in and never another agent's. The exchange runs in a child process per
  turn, with only `AG2_DEVICE_ENV` (the token file `install` recorded as `TOKEN_ENV_FILE`, passed to the
  connector as `--token-env-file`) and a seat's own keys; the relay token stays in that child, and agy receives
  only the short-lived access token, as the server's `Authorization` header. agy's own environment is
  unchanged (the install's allowlist): no relay token, no token file path.
- **Transport.** Always **stdio**, never HTTP, even when agy's `initialize` declares HTTP MCP: over HTTP
  agy would reach the server directly and nothing would filter its tools (agy 1.5.9 and 1.5.13 declare no
  `mcpCapabilities` at all). The server is the desktop app's
  `ag2-mcp-proxy` (`engine/ag2-mcp-proxy.mjs`, the same stdio proxy Claude Code and Codex cores run as their
  `s-space` server), named at install with `--mcp-proxy-command "'<node>' '<path>/ag2-mcp-proxy.mjs'"`: quote each path, since the desktop's engine sits under `Application Support`. `install` refuses a command whose first word is not an executable, and records the line with every word quoted. Each turn
  the connector runs `ag2-sparrow-mcp descriptor --env-file <TOKEN_ENV_FILE>` (that file only, whatever
  else holds a token) in the same isolated child and writes the proxy's
  non-secret descriptor (0600, `state/ag2-mcp-descriptor.json`: mint and MCP URLs, the agent's token file and
  key, the secret's sha256 and the agent id). agy gets `{name: ag2-space, command, args, env:
  [AG2_MCP_DESCRIPTOR, AG2_MCP_LOG]}`: paths only, exactly what Claude Code's stdio entry carries. The proxy,
  agy's child, reads the relay token from the token file, mints as the agent, sends the relay token only to
  the mint and the access token only to the MCP endpoint, and scrubs its own environment.
- **Tool names (stdio).** agy drops every MCP tool whose name has a dot. Measured on 2026-11-21: of AG2
  Space's tools it offered only `ui_describe`, `ui_request` and `ui_result`, so `ag2.whoami` (empty schema)
  and every `room.*` tool were missing. The proxy therefore runs under `ag2_sparrow.mcp_name_shim`, which
  only renames: `tools/list` answers `room_action_execute` for `room.action.execute` (any character outside
  `[A-Za-z0-9_-]` becomes `_`, cut to 50), and a `tools/call` for that name reaches the server as
  `room.action.execute`. The policy matches both spellings. agy checks the prefixed name
  (`mcp_ag2-space_<tool>`, at most 64), so aliases stop at 50 characters. The entry's `command` is
  `state/ag2-mcp-shim` (0700, written each turn), a two-line `sh` launcher, and its `args` are just the
  proxy's command line; the shim writes `start` and `exit` lines to `state/ag2-mcp-proxy.log`.
- **A launch that failed is said.** Each turn the connector deletes `state/ag2-mcp-status.json` and writes a
  fresh turn id into the launcher; the shim records that id and how far it got (`started`, `listed` with a
  tool count, or `exited` with a code). At turn end, a message whose turn offered the stdio entry gets one
  line when the tools did not come up: "S.space tools didn't start this turn (the helper did not start | the
  helper exited | no tool list), so I couldn't use Room Commons." Only those fixed words reach the room; the
  code, argv and paths stay in the log and `state/metrics.jsonl` (`sspace_tools_down`). A resumed Session
  (`session/load`) with no launch reports its own last launch, kept per Session id in
  `ag2-mcp-launches.json` beside the session store (on the 18:44 live run agy launched no server on resume and
  kept the tools it had when the Session started): a line when that launch failed, nothing when it worked.
- **Token file shape (stdio).** An agent `.env` with a `REMOTE_TASK_TOKEN=<relay>|<secret>` line gets the
  plain descriptor (`env_file` that `.env`, `env_key` its key). A seat made by `--token-stdin` or `connect` has
  an `.env` (`channels/ag2space/.env`) holding only `REMOTE_TASK_TOKEN_FILE`, and its token as a raw line in
  `channels/ag2space/token`: it gets `token_format: "raw"` with `env_file` that token file's realpath,
  `relay` the file's own relay half (never a `REMOTE_TASK_URL` override) and no `env_key`. That needs the
  desktop proxy from ag2space-cinny-desktop#1246; an older proxy refuses it at start ("descriptor missing
  env_key") and agy reports the tools unavailable.
- **When none.** No token file (`--work` with a bridge you run), no `--mcp-proxy-command`, or a
  failed exchange: the turn runs without the tools and the log says why.
- **Which tools.** agy asks before each MCP call; the policy grants, to owner and team turns only, the
  `ag2-space` server's `ag2.whoami`, `room.list`, `room.inspect`, `room.actions.search`,
  `room.actions.describe`, `room.action.read`, `room.action.execute`, `room.action.execute_safe`,
  `room.task.list`, `room.task.create`, `room.task.update`, `operation.inspect`, `approval.inspect` and
  `space.action.read` (`mcp_auth.OWNER_TOOLS`; a name reported with `_` for `.` matches too). Never granted:
  `agent.link`, `agent.unlink`, `agent.setup.steps`, `relay.token.issue`, event subscriptions,
  `space.action.execute` and `ui.*`, any other server's tool, and any request that carries a command.
- **Enforced in the shim too.** The policy only sees what agy asks about, and an agy that auto-allows MCP
  (an `mcp(*)` allow, an earlier "always allow") asks nothing. So the launcher hands the shim the same grant
  (`AG2_MCP_SHIM_TOOLS`, from `mcp_auth.OWNER_TOOLS`, for owner and team turns only): its `tools/list` answer
  keeps only those tools, and a `tools/call` for any other name (by alias or server name, or malformed) gets
  a JSON-RPC error and never reaches the server. A launcher without a grant lists and forwards nothing.
- **By identity, never by text.** The request is matched by the MCP server and tool name it carries in
  structured fields (`serverName`/`toolName` or `mcpServer`/`mcpTool`, on the tool call or in its `_meta`;
  `acp_host.MCP_IDENTITY_KEYS`), compared exactly; the title is never read. Which fields agy uses has not been
  measured: a request in another shape is denied, and its reason in `state/metrics.jsonl` lists its key names
  (never values), so it can be mapped.

**Guest turns get no S.space tools** (a guest, or a team-tier sender without the attested collaborator flag):
no MCP server in their Session, `mcp(*)` denied in their agy home, and
every MCP request refused. Read-only Actions are not offered to guests either: a read can still return another
room's Commons or tasks to someone the agent's owner never shared them with, so "no mutations" is kept by
giving no tools at all. A sender with no admitted tier is refused before agy starts, as before.
