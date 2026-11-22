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
The text is posted with every delivery marker (attachment, redirect, skip, thread) neutralised. When a
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

- the owner's turns only, and only on an install with `--allow-owner-shell-web` (recorded as
  `ALLOW_OWNER_SHELL_WEB=1`; off by default, so an existing install does not change on update): any shell
  command and any `execute`, `fetch` or `search` call, as a local Claude Code agent runs commands and uses
  the web for its owner. `verify` prints `INFO owner shell and web: on|off`;
- the owner only: a `read` or `edit` whose every path resolves (after `..`, `~` and symlinks) under the
  working directory; never when the working directory is the home directory or above it. agy's own
  argument names (`Path`, `Content`, `Overwrite`, `Append`, `Target`, `Replacement`, `AllowMultiple`,
  `Edits`, `StartLine`, `EndLine`, and the older `TargetFile`, `CodeContent`, `AbsolutePath`, ...) are
  recognised; any other key refuses the request;
- nothing else: no command or web call for a collaborator or a guest, and no `delete`, `move` or
  unclassified call for anyone.

| Turn | Commands, fetch, search | Read or edit under the working directory | Anything else |
| --- | --- | --- | --- |
| owner | granted with `--allow-owner-shell-web`, else refused | granted | refused |
| Team (an attested collaborator) | refused (granting it is the owner's decision) | as the turn it runs in | refused |
| guest (isolated) | refused | refused | refused |

With `--allow-owner-shell-web`, the working-directory bound on `read` and `edit` is no longer a security
boundary for the owner's turns: a command can read or write anything the owner's account can.

For the owner's turns, agy applies its own permission rules first (an action they already allow runs
without asking), and `--dangerously-skip-permissions` makes it run tools without asking; never add that
flag to an agent others can reach.

Measured against a real agy 1.5.9 (signed out): with `--gemini-dir` an absolute path, agy keeps its app
data, `settings.json`, logs, conversations and `config/` under that directory, reads the settings file
there, and nothing is written to `~/.gemini`; `initialize` and the -32000 "Authentication required" answer
to `session/new` are the same as without it. Not yet measured, because no signed-in agy was available:
that a signed-in agy finds its sign-in with `--gemini-dir`, which modes `session/new` lists, and that
global rules are read only from the `--gemini-dir` home.
