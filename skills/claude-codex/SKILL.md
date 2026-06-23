---
name: claude-codex
description: "Bash wrapper around the local Codex CLI for non-interactive runs from inside Sutando (bridges, cron, scripts). For interactive code review or task hand-off from this Claude Code session, prefer the official `/codex:*` plugin commands; this skill is the file-bridge-compatible path that `discord-bridge.py` invokes for team-tier sandboxed delegation."
user-invocable: true
---

# Claude Codex

Delegate work from Claude Code to the local `codex` CLI. This skill assumes Codex is already authenticated on this machine. It does not mint, extract, or transfer credentials.

**Usage**: `/claude-codex [prompt]`

ARGUMENTS: $ARGUMENTS

## When to Use

- "Ask Codex to review this change"
- "Use my Codex subscription from Claude Code"
- Need a second model to inspect a bug, review a diff, or propose an implementation
- Need a Codex result saved or streamed from the current repo
- Spec-driven one-shot build of a self-contained artifact (HTML/CSS/JS prototype, single-file
  demo) — use `--goal` to invoke Codex's `/goal` mode

## When NOT to Use

- **Interactive review from this Claude Code session** → use `/codex:review`, `/codex:adversarial-review`, or `/codex:rescue` from the openai/codex-plugin-cc plugin. They're the discoverable, versioned path.
- **Anything that needs a Codex session ID / job tracking / status polling** → plugin's `/codex:status` is the right surface.

If you don't see `/codex:*` slash-commands available, install the plugin in this Claude Code session:

```
/plugin marketplace add openai/codex-plugin-cc
/plugin install codex@openai-codex
/reload-plugins
/codex:setup
```

This skill stays as the **non-interactive bash-wrapper** path — invoked by `discord-bridge.py`'s team-tier `===SUTANDO SYSTEM INSTRUCTIONS===` block (codex exec --sandbox read-only on Discord tasks from non-owner senders), cron-fired jobs that need a one-shot codex call, and similar file-bridge workflows where a plugin slash-command can't reach.

## Guardrails

- Prefer `codex review --uncommitted` for code review.
- Prefer `codex exec` for analysis, planning, or implementation prompts.
- Keep Codex pointed at the same repo with `-C "$PWD"` unless the user asked for another directory.
- Default to `workspace-write` sandbox or stricter. Do not use bypass flags unless the user explicitly asks.

## Quick Checks

```bash
codex login status
bash "$SKILL_DIR/scripts/codex-run.sh" --check
```

## Common Commands

```bash
# General delegation
bash "$SKILL_DIR/scripts/codex-run.sh" -- "Inspect src/task-bridge.ts for race conditions"

# Safer review of current uncommitted changes
bash "$SKILL_DIR/scripts/codex-run.sh" --review --uncommitted -- "Prioritize bugs and missing tests"

# Review against a base branch
bash "$SKILL_DIR/scripts/codex-run.sh" --review --base main -- "Focus on regressions and security"

# Save the last Codex message to a file
bash "$SKILL_DIR/scripts/codex-run.sh" --output-last-message results/codex-review.txt -- "Review the current workspace"

# Spec-driven one-shot build (Codex /goal mode)
bash "$SKILL_DIR/scripts/codex-run.sh" --goal -- "$(cat path/to/spec.md)"
```

## PR review — `scripts/review-pr.sh <N>` (read-only, bounded)

The safe, read-only way to get a Codex review of a GitHub PR. Fetches the diff with
`gh pr diff <N>` (READ-ONLY — no checkout, never mutates git state or fails on a dirty
tree), inlines it into `codex exec --sandbox read-only`, and wraps the whole thing in
`codex-bounded.sh` so a slow/wedged review can't grind unbounded.

```bash
bash "$SKILL_DIR/scripts/review-pr.sh" 1754            # default --max 240 --stall 60
bash "$SKILL_DIR/scripts/review-pr.sh" 1754 --max 300  # longer cap for a big diff
```

Prints Codex's verdict to stdout. Exit `0` = verdict produced; `124` = hit the `--max`
cap; `125` = stalled (no output for `--stall` s); other non-zero = gh/codex error.

**Timing:** `codex exec` is agentic — even with the diff inlined it may explore related
code, so a real review often takes 100s+ (147s observed on #1754). Keep `--max` ≥180;
do NOT drop it near 120 or you'll kill legitimate reviews. This is the path the team-tier
`===SUTANDO SYSTEM INSTRUCTIONS===` block runs to auto-review a non-owner PR-review request
(owner-ping only on failure).

## Bounded runner — `scripts/codex-bounded.sh`

Generic stall-watchdog + wall-clock cap for any (sandboxed) delegation. `--stall N` kills the
process tree after N seconds of total output SILENCE (the "never going to finish" signal —
a healthy codex streams as it works); `--max M` is the absolute backstop (`--max 0` disables
it, stall-only). Exit `125` = stalled, `124` = max cap. Used by `review-pr.sh` and by the
discord-bridge team/other-tier `codex exec` delegation so a sandboxed run can't grind forever.

```bash
bash "$SKILL_DIR/scripts/codex-bounded.sh" --stall 90 --max 240 -- <command...> < /dev/null
```

## `/goal` mode (`--goal`)

Wraps Codex's interactive `/goal` slash-command for non-interactive use. The flag prepends
`/goal ` to the prompt and forces `--full-auto` so Codex can write files unattended.

Reach for it when:

- The task is a self-contained artifact (single HTML file, one-off demo, isolated script).
- You have a tight written spec and want a one-shot build with a known cost ceiling.
- A second-model take on a prototype is useful (run `--goal` in parallel with Claude's own
  build for cross-check).

Skip it when:

- The task touches in-repo code or needs memory/context Claude already has.
- You expect to iterate — `/goal` is one-shot and does not self-correct on failure.

## If Invoked As A Slash Command

- If ARGUMENTS is empty, explain the available modes and suggest `--review --uncommitted` for diffs or a plain prompt for general delegation.
- If ARGUMENTS starts with `--goal ` (e.g., `/claude-codex --goal Build a self-playing demo`), strip the prefix and route the remaining text through `--goal` mode:

```bash
PROMPT="${ARGUMENTS#--goal }"
bash "$SKILL_DIR/scripts/codex-run.sh" --goal -- "$PROMPT"
```

- Otherwise (plain prompt), run:

```bash
bash "$SKILL_DIR/scripts/codex-run.sh" -- "$ARGUMENTS"
```

