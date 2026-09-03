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
`GEMINI.md` there, history, extensions), whatever the working directory. With
`GEMINI_API_KEY` set the wrapper therefore runs the CLI with a fresh, empty `HOME`
that is deleted when the run ends. Without the key the user's own `~/.gemini` is
where OAuth credentials live, so `HOME` is kept and the wrapper says so on stderr.

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

## Verify

```
bash skills/claude-gemini/scripts/gemini-sandbox.sh --cd /tmp -o /tmp/answer.txt -- "Reply with the single word ok"
cat /tmp/answer.txt
```

Then post as a team-tier sender in a channel the bridge serves and watch
`results/` for `task-<id>.txt` rather than the sentinel.
