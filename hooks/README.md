# Sutando Claude Code hooks

PreToolUse hooks deployed into each node's `~/.claude/` (they are user-level Claude
Code config, not loaded from the repo at runtime — this dir is the version-controlled
**source**; deployment copies the file out and registers it in `settings.json`).

## `context-source-guard.py`

Enforces the **contextNotFrom** rule on the agent's own Discord channel reads:
serving a channel whose `contextNotFrom` (in `~/.claude/channels/discord/access.json`)
lists a channel/guild → a raw `curl …/channels/<id>/messages` of that channel/guild is
**DENIED** before any content enters context. Serving-relative (serving the private
channel can still read it), fail-closed when a target guild can't be verified. This is
the enforcement layer behind `src/read_discord_channel.py` + the bridge prefetch gate —
the part an instruction alone can't guarantee, since a raw curl bypasses an instruction.

### Deploy (per node)

```bash
cp hooks/context-source-guard.py ~/.claude/hooks/
# register under BOTH the Bash and Read PreToolUse matchers:
python3 - <<'PY'
import json, os
sp = os.path.expanduser("~/.claude/settings.json"); s = json.load(open(sp))
cmd = "python3 ~/.claude/hooks/context-source-guard.py"
pre = s.setdefault("hooks", {}).setdefault("PreToolUse", [])
for m in ("Bash", "Read"):
    blk = next((b for b in pre if b.get("matcher") == m), None)
    if blk is None: pre.append({"matcher": m, "hooks": [{"type": "command", "command": cmd}]})
    elif cmd not in [h.get("command") for h in blk["hooks"]]: blk["hooks"].append({"type": "command", "command": cmd})
json.dump(s, open(sp, "w"), indent=2)
PY
```

`settings.json` registration is read at **session start**; once registered, the script
file itself is executed fresh on every tool call, so updating `context-source-guard.py`
takes effect immediately. Adding a *new* registration requires the core session to restart.

## `skip-ask-user-question.py`

Blocks the built-in interactive **AskUserQuestion** tool in the headless core.
The core runs non-interactively (`start-cli.sh` launches `claude` with
`--dangerously-skip-permissions` inside tmux, driven over `--remote-control`, no
human at the terminal), so an `AskUserQuestion` tool call has no UI to answer it
and **blocks the session indefinitely**. This hook returns a PreToolUse `deny`
for `AskUserQuestion` — Claude Code short-circuits the call before it can render
and feeds the reason back to the model, which then proceeds autonomously. It is a
no-op (exit 0) for every other tool, and fails **open** on any error.

Unlike `context-source-guard.py`, this hook is **auto-registered** for every core
session — no per-node deploy step. `src/agent/claude/cli/start-cli.sh` always
composes it into the core's `--settings` JSON (via
`src/agent/claude/cli/build-core-settings.mjs`, which also merges in the obs
collector hooks when capture is enabled), under a `PreToolUse` matcher scoped to
`AskUserQuestion`. To register it manually elsewhere, add a `PreToolUse` entry
with matcher `"AskUserQuestion"` and command `python3 <path>/skip-ask-user-question.py`.

Test: `python3 tests/skip-ask-user-question.test.py` (hook) and
`tsx --test tests/agent/claude/cli/build-core-settings.test.ts` (registration/merge).

Config paths are env-overridable for testing: `SUTANDO_DISCORD_ACCESS_FILE`,
`SUTANDO_DISCORD_ENV_FILE`, `SUTANDO_WORKSPACE`. Test: `python3 tests/context-source-guard.test.py`.

## `activity-emitter.py`

Journals the core's activity as AWP activity objects (Activity outbox Phase 2,
step 1). Async command hook for SessionStart / UserPromptSubmit / PreToolUse /
PostToolUse / PostToolUseFailure / Notification / Stop / SessionEnd — each fires
this emitter, which normalizes the hook JSON to an activity object and appends
it to `<workspace>/state/activity-journal/YYYY-MM-DD.jsonl`. Attribution rides
in from the Execution Binding Registry when present. Secret hygiene: tool input
reduces to a display hint (description/file_path/pattern/url — deliberately
never the raw `command`). Fail-OPEN + fast; register every entry with
`"async": true`. Upstream HTTP delivery is a later step (broker `/v1/activities`);
until then the journal is the local activity feed.

Not yet auto-registered. Manual registration: async command-hook entries for the
events above, argv[1] = hook name as a stdin fallback. Test:
`python3 tests/activity-emitter.test.py`. Test-only env override:
`SUTANDO_ACTIVITY_DIR`.

## `gmail-write-guard.py`

Denies the **claude.ai Gmail MCP connector's WRITE-scoped tools** (create_draft,
label_thread, unlabel_thread, create_label, apply_sensitive_*_label, archive,
trash, send, …) and routes the model to the app-password IMAP/SMTP path
(docs/built-in-tools.md → Email). Field report 05cb849a: the connector's OAuth
flow doesn't actually grant Gmail write scopes (label/archive fail with a raw
"insufficient authentication scopes" error) and `create_draft` caused 7
documented incidents incl. a wrong-recipient send — while READ tools work fine
and remain allowed. The guard matches only `mcp__…` tools whose name mentions
gmail AND carries a write verb (`list_labels` stays allowed; `label_thread` is
denied); non-Gmail tools are a no-op, so it is safe under a broad matcher.

Escape hatch: `SUTANDO_ALLOW_GMAIL_CONNECTOR_WRITES=1` lifts the guard (for
if/when the connector's scopes are fixed upstream). Fail-OPEN on hook errors.

### Deploy (per node)

```bash
cp hooks/gmail-write-guard.py ~/.claude/hooks/
python3 - <<'PY'
import json, os
sp = os.path.expanduser("~/.claude/settings.json"); s = json.load(open(sp))
cmd = "python3 ~/.claude/hooks/gmail-write-guard.py"
pre = s.setdefault("hooks", {}).setdefault("PreToolUse", [])
blk = next((b for b in pre if b.get("matcher") == "mcp__.*[Gg][Mm][Aa][Ii][Ll].*"), None)
if blk is None: pre.append({"matcher": "mcp__.*[Gg][Mm][Aa][Ii][Ll].*", "hooks": [{"type": "command", "command": cmd}]})
elif cmd not in [h.get("command") for h in blk["hooks"]]: blk["hooks"].append({"type": "command", "command": cmd})
json.dump(s, open(sp, "w"), indent=2)
PY
```

Test: `python3 tests/gmail-write-guard.test.py`.
