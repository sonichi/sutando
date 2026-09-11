# Gemini as the non-owner sandbox

Non-owner Discord tasks (`access_tier: team` or `other`) are never answered by the
owner's core directly. The bridge's tier rulebook tells the core to delegate them to
a read-only sandbox in two stages: Stage 1 runs the sandbox and writes its final
answer to a staging file, Stage 2 moves that file into `results/` so the bridge
posts it. If Stage 1 fails, the core writes a fallback sentinel instead:

```
Sandbox unavailable (codex exit <rc>) — no reply generated.
```

The sandbox is Codex CLI by default. On an install with only Claude Code, Codex
is absent, so every non-owner task ends in that sentinel and teammates and
outsiders are never answered. `sandbox.runtime` selects the Gemini CLI instead.

## Enable

1. Install the Gemini CLI: `npm i -g @google/gemini-cli`.
2. Put `GEMINI_API_KEY=...` in `.env` (any auth the gemini CLI supports works,
   this is the simplest). The same key serves the voice agent if you use it.
3. In `sutando.config.json` (or the gitignored `sutando.config.local.json`):

```json
"sandbox": { "runtime": "gemini" }
```

`SUTANDO_SANDBOX_RUNTIME=gemini` overrides the file for one invocation. Restart
the Discord bridge: the rulebook is rendered when the bridge starts.

## What changes, and what does not

Only Stage 1 of the team and other rulebooks changes. Instead of

```
codex-bounded.sh --stall 45 --max 240 -- codex exec --sandbox read-only ... -o <staging> -- <prompt>
```

the core runs

```
codex-bounded.sh --stall 45 --max 240 -- bash skills/claude-gemini/scripts/gemini-sandbox.sh --cd <dir> -o <staging> -- <prompt>
```

`gemini-sandbox.sh` keeps codex's contract exactly: headless, stdin closed, the
final answer and nothing else written to `-o`, nothing written on failure, and
gemini's own exit status forwarded. So Stage 2, the fallback sentinel (now worded
`gemini exit <rc>`), the bounded runner's stall and cap guards, and the NO-REPLY
handling are unchanged. Team tasks run in the workspace, other tasks in `/tmp`,
as with Codex.

Read-only and sandboxed means `--approval-mode plan` (Gemini makes no edits) and
`--sandbox`: macOS seatbelt with `SEATBELT_PROFILE=restrictive-open` by default
(strict file restrictions, network allowed since the model call needs it), Docker
or Podman on Linux. The wrapper sets `GEMINI_CLI_TRUST_WORKSPACE=true` because a
headless run has nobody to answer the trust prompt.

The PR auto-review branch of the team rulebook still uses Codex (it inlines the
diff into `codex exec`). On a gemini-only install it takes its documented failure
path, an owner ping, rather than a review.

## What the sandbox can and cannot see

Checked on macOS with the real CLI, both directions, so a null result means something:

- From `/tmp`, a sandboxed run asked to list `~/.gemini` and print
  `~/.gemini/projects.json` was refused by the CLI's workspace boundary
  (`Path not in workspace ... allowed workspace directories: /private/tmp`).
- The same run asked to create a file in `/tmp` was refused (`plan` mode) and the
  file did not exist afterwards. So the confinement engages.

The CLI itself still reads user-level state from `~/.gemini` at startup (a
`GEMINI.md` there, history, extensions), whatever the working directory, and before
the sandbox confines anything. So the wrapper decides HOME by how the CLI is
authenticated:

| auth in the environment | HOME the CLI sees |
| --- | --- |
| `GEMINI_API_KEY`, `GOOGLE_API_KEY` or `GOOGLE_APPLICATION_CREDENTIALS` | a fresh empty directory, deleted after the run |
| none of those, `GEMINI_SANDBOX_AUTH_HOME` set to a directory | that directory, meant to hold only the CLI's OAuth credentials |
| none of those | the run is refused, exit 2 |

A non-owner task never runs with the owner's own HOME. If you sign in to the CLI with
OAuth, put a copy of `~/.gemini/oauth_creds.json` in a directory of its own and point
`GEMINI_SANDBOX_AUTH_HOME` at it.

`GEMINI_CLI_TRUST_WORKSPACE=true` is set because a headless run has nobody to
answer the trust prompt. Trust means the CLI may load context files from the
working directory: `/tmp` for other-tier tasks, the owner's workspace for
team-tier ones. It does not widen what the sandbox may read.

Off macOS, `--sandbox` needs Docker or Podman. The wrapper refuses to run when
neither is on PATH rather than letting a non-owner task run unconfined.

One thing to know when reading answers: the bridge's sandbox prompt tells the
model it is answering as Sutando and to refer to Sutando's skills, so an answer
may name skills from the model's own knowledge of this public repository even
when it could read nothing. That is the prompt, not a leak.

## The stall guard, and what it means for this runtime

Stage 1 is wrapped by `codex-bounded.sh --stall 45 --max 240`. The stall guard kills a
command that writes nothing to stdout or stderr for 45 seconds, on the reasoning that a
working codex streams events as it goes. In plain JSON mode the Gemini CLI prints one
object at the end and nothing before, so a healthy run looked wedged and was killed: 8
of 36 scenarios in one run came back as `Sandbox unavailable (gemini exit 125)`, which
is the guard's own kill code.

The wrapper therefore asks for `--output-format stream-json`, writes one line to stderr
per event, and writes a heartbeat line whenever nothing has arrived for
`GEMINI_SANDBOX_HEARTBEAT` seconds (default 10). Two consequences, stated plainly:

- For this runtime the stall guard cannot fire on a quiet but live process. Only the
  `--max 240` cap ends a run that never finishes. The guard's setting is unchanged and
  its effect is now that of a cap.
- Gaps between events while the model thinks were measured at up to 36 seconds on a
  heavy prompt, so streaming alone would leave a thin margin against 45. The heartbeat
  is what closes it.

## When the bridge refuses to start

`sandbox.runtime` is read once when the Discord bridge starts. Two things stop the
bridge there, loudly, rather than failing later on a non-owner message:

- a value that is not `codex` or `gemini`
- a runtime the tier rulebooks cannot be rendered for, which happens if the rulebook
  text in `discord-bridge.py` drifts from the Stage-1 templates the renderer matches,
  or the PR-review paragraph's headings are reworded

The message names which of the two it was. This is fail closed on purpose: a setting
that only affects non-owner sandboxing can keep the whole bridge down, because the
alternative is a bridge that runs and hands non-owner tasks a command that does not
exist. Owner traffic is otherwise untouched by the setting: each message renders only
the rulebook it will carry, and owner and collaborator books are never rewritten.

## Verify

```
bash skills/claude-gemini/scripts/gemini-sandbox.sh --cd /tmp -o /tmp/answer.txt -- "Reply with the single word ok"
cat /tmp/answer.txt
```

Then post as a team-tier sender in a channel the bridge serves and watch
`results/` for `task-<id>.txt` rather than the sentinel.
