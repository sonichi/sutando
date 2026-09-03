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

`~/.claude` is NOT always the config dir. Claude Code honours `$CLAUDE_CONFIG_DIR`, and the
Sutando core sets it (e.g. to `<workspace>/.claude-sutando`). Registering into
`~/.claude/settings.json` on such a node edits a file the core never reads — the JSON is valid,
the hook is present, and the guard is still not armed. Resolve the dir first:

```bash
CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "$CFG/hooks"
cp hooks/context-source-guard.py "$CFG/hooks/"
# register under BOTH the Bash and Read PreToolUse matchers:
CFG="$CFG" python3 - <<'PY'
import json, os, shlex
cfg = os.environ["CFG"]
sp = os.path.join(cfg, "settings.json")
s = json.load(open(sp)) if os.path.isfile(sp) else {}
# Quote: the stored command is re-parsed as a shell word list, and this
# recipe's target dirs routinely contain a space (e.g. Application Support).
cmd = f"python3 {shlex.quote(os.path.join(cfg, 'hooks', 'context-source-guard.py'))}"
pre = s.setdefault("hooks", {}).setdefault("PreToolUse", [])
for m in ("Bash", "Read"):
    blk = next((b for b in pre if b.get("matcher") == m), None)
    if blk is None: pre.append({"matcher": m, "hooks": [{"type": "command", "command": cmd}]})
    elif cmd not in [h.get("command") for h in blk["hooks"]]: blk["hooks"].append({"type": "command", "command": cmd})
json.dump(s, open(sp, "w"), indent=2)
PY
```

Verify. Registration is the only thing that proves the guard is armed — the file being
present in `hooks/` does not. And registration alone does not prove it *runs*: the
command is stored as a string and re-parsed by a shell, so an unquoted path splits on
the space in `Application Support` and the hook dies before reading its input. Execute
what is registered, don't just look for it:

```bash
CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}" python3 - <<'PY'
import json, os, subprocess
h = json.load(open(os.path.join(os.environ["CFG"], "settings.json"))).get("hooks", {})
cmds = [k["command"] for m in h.get("PreToolUse", []) for k in m.get("hooks", [])
        if "context-source-guard" in k.get("command", "")]
print("registered:", len(cmds))
for c in cmds:
    # The guard fail-opens on a Read event, so 0 is the expected exit. A split
    # path exits 2 — which is PreToolUse's BLOCK code, so the broken recipe
    # denies every Bash and Read call rather than merely failing to guard.
    r = subprocess.run(c, shell=True, input='{"tool_name":"Read","tool_input":{}}',
                       text=True, capture_output=True)
    print(f"exit {r.returncode}  {'OK' if r.returncode == 0 else 'BROKEN: ' + r.stderr.strip()[:90]}")
PY
```

Both matchers must appear (`Bash` and `Read`), and every line must read `exit 0`.

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

## `human-action-bridge.py`

Upgrades the `AskUserQuestion` hard-deny into a **remote ask** (human-action
bridge v1 step 1 — design: `workspace notes/tasks-events/human_action_bridge_design.md`).
On an `AskUserQuestion` call it writes a durable pending-action file
(`<workspace>/state/human-actions/ha_*.json`), drops a question card for the
owner (`results/proactive-ha-*.txt` — the sanctioned proactive path), and
polls the action file for a bounded window. A resolved decision returns
PreToolUse `allow` with `updatedInput.answers` (Claude continues as if answered
locally); **timeout or cancellation denies** with the same decide-autonomously
guidance `skip-ask-user-question.py` ships — so with no resolver present the
behavior is exactly today's. Timeout NEVER approves; fail-**open** for the
session, fail-**closed** for the decision. Decisions are written by the sparrow
`DecisionHandler` (bridge v1 step 3) or by the core when the owner's answer
arrives as a normal task.

Register under `PreToolUse` matcher `"AskUserQuestion"` **instead of**
`skip-ask-user-question.py` (the timeout branch subsumes it). Not yet
auto-registered — flipping `build-core-settings.mjs` over is a follow-up once
the decision path is live end-to-end.

Test: `python3 tests/human-action-bridge.test.py`. Test-only env overrides:
`SUTANDO_HA_DIR`, `SUTANDO_HA_CARD_DIR`, `SUTANDO_HA_TIMEOUT`, `SUTANDO_HA_POLL`.

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

### Registration

**Auto-registered** for every core session: `start-cli.sh` passes this hook to
`src/agent/claude/cli/build-core-settings.mjs`, which registers it under
`PreToolUse` with matcher `mcp__.*[Gg][Mm][Aa][Ii][Ll].*` in the `--settings`
JSON. Nothing to install per node.

The registration rides `--settings` rather than a written `settings.json`, so it
survives an app update that replaces the engine tree (the failure mode issue
#3221 describes for the `install-claude-hooks.sh` set).

To register it in a non-core session (e.g. an interactive Claude Code), add the
same `PreToolUse` entry to `~/.claude/settings.json` by hand:

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

## `review-authority-guard.py`

Denies a **formal GitHub review** filed from Bash — `gh pr review --approve` /
`--request-changes` (and `--comment` under `hold`), or `gh api .../pulls/N/reviews`
carrying `APPROVE` / `REQUEST_CHANGES` — while the owner's standing answer on
review authority is unresolved. An APPROVE moves a merge gate, and merges are
the owner's; verifying a change carefully is not authorization to vote on it.
The mode lives in `<workspace>/state/authority.json`:
An owner who ruled *verbally* has no file yet, so that ruling reads as `hold` until someone writes it — register the file on the node whose owner already answered.

```json
{"github_formal_review": "hold" | "findings-only" | "allow"}
```

A missing file means `findings-only`: the votes stay denied until the owner
rules, while a COMMENTED review — which moves no gate and is the durable place
a finding lives — stays possible. A file that is present but unreadable, or
carries an unknown mode, means `hold` (a ruling was written and cannot be read,
so the restrictive reading applies). Never gated: review dismissals (a
reduction of standing), `gh pr comment`, `--comment` under `findings-only`, and
every non-review command. Compound commands are split per segment so an earlier
benign `gh` cannot shadow a later review; `bash -c "..."` / `sh -c` / `eval`
wrappers are re-classified on their quoted command.

Escape hatch: `SUTANDO_ALLOW_FORMAL_GH_REVIEWS=1`. Fail-OPEN on hook errors.

### Registration

Not auto-registered. Deploy per node into `$CLAUDE_CONFIG_DIR` and add a
`PreToolUse` entry with matcher `Bash`, the same way as the manual block under
`gmail-write-guard.py` above (command: `python3 <deployed path>/review-authority-guard.py`).
In-repo the hook resolves the workspace through `workspace_default.resolve_workspace`;
a deployed copy searches upward for `state/authority.json`. Set
`SUTANDO_HOOK_WORKSPACE=<workspace>` to pin it.

Test: `python3 tests/review-authority-guard.test.py`.

## `release-target-guard.py`

DENIES `gh release create|edit` whose `--target` is an abbreviated commit SHA
(7-39 hex characters). GitHub answers `Release.target_commitish is invalid` and
creates nothing, so the release reads as cut at the moment it did not happen.
A full 40-character SHA and a branch/tag name both pass.

It exists because the rule is easy to know and useless to know: the value is not
chosen, it is pasted from whatever printed last, and every tool prints the
abbreviated form. Measured twice in fourteen hours on one host, with the
correction written into the build log between the two occurrences.

### Deploy (per node)

```bash
CFG="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "$CFG/hooks"
cp hooks/release-target-guard.py "$CFG/hooks/"
```

Register it under the `Bash` PreToolUse matcher exactly as the guards above do
(same `shlex.quote` recipe — these paths routinely contain a space).

Escape hatch: `SUTANDO_SKIP_RELEASE_TARGET_GUARD=1`. Fail-open on any internal
error, like every guard here.

Scope: it sees only literal text. `--target "$(git rev-parse --short HEAD)"`
is allowed, because the value is unknowable before execution — and that is a
very plausible way to produce this bug. The guard bounds pasted values, not
computed ones.

Tests: `python3 tests/release-target-guard.test.py`

## `result-file-marker-guard.py`

Denies a **Write/Edit into `<workspace>/results/`** whose body carries a
`[file:|send:|attach:]` marker pointing at a path the delivering bridge will refuse
to send (policy: `src/send_allowlist.py`). Parsing and the verdict both come from
the modules the delivery path itself uses (`result_markers` + `send_allowlist`), so
the guard cannot drift from what it enforces.

Why: on 2026-08-04 a finished video was attached from `skill-repos/`, which is not
on the allowlist. The bridge posted a literal `(file not allowed: ...)` into the
owner's channel and the task archived as delivered -- the file existed, the marker
parsed, the write succeeded, nothing errored. Every signal available to the author
said "sent". The owner found it: *"Can't see this file. And I don't want to
babysit."* The check therefore has to run where the marker is **authored**.

**Destination-aware.** The allowlist is not global: Slack extends it with its
adapter-local `<workspace>/slack-inbox/` so an uploaded file can be echoed back
(`src/slack-bridge.py:153-158`). The guard resolves the destination from the task
the result answers (`results/task-<id>.txt` -> `tasks/task-<id>.txt` -> `source:`)
and applies that adapter's policy. When the destination can't be established --
a `results/proactive-*.txt` body, or any result with no originating task -- it uses
the **canonical roots only, never the union**. An earlier version did use the union,
reasoning that a false deny for a destination nobody can name is unsatisfiable; that
is unsound. A proactive body has no task to name a source, and Discord, Telegram and
Slack all claim proactive files with no deterministic winner (`slack-bridge.py`
race-renames them), so the union would authorize a provider-local root such as
`slack-inbox/` for a body a different adapter then refuses -- reproducing the exact
silent failure this guard exists to prevent, behind a clean pass.

Canonical-only inverts that: every adapter accepts these roots, so an allow here is
an allow everywhere. The cost is one deny -- a provider-local path in a proactive
body -- and it is satisfiable in one step: stage the file into a canonical sendable
root such as `results/` and point the marker there. The reason text says so.

**The repo root is CONFIGURED, never discovered.** The hook needs `src/` on
`sys.path`, and it must not find it by walking up from `__file__`: deploying copies
the file out of the checkout, and that pattern is banned repo-wide
(`scripts/lint-workspace-resolution.sh`) because it breaks under symlinked/bundled
layouts. Pass `--repo <path>` (the snippet below does) or set `$SUTANDO_REPO_ROOT`.
If neither resolves, the hook prints `INERT: repo root not configured` **to stderr**
and allows -- an unresolvable root must never be indistinguishable from a clean pass.

Denies rather than warns -- a warning still puts a broken message in the owner's
channel. The reason names the offending path and the allowed roots, so the fix
(re-encode or copy into `results/`, then point the marker there) is one step.

Fails **open** on any internal error, unlike `context-source-guard.py`, which fails
closed. That one prevents blacklisted content entering context, where being wrong
means a leak; here being wrong means a message the owner can see and re-request,
so wedging the core would be the larger harm.

Escape hatch: `SUTANDO_SKIP_FILE_MARKER_GUARD=1`.

### Deploy (per node)

Registration embeds this node's checkout path, so it is correct per host and the
hook never has to guess:

```bash
REPO="$(git rev-parse --show-toplevel)"
cp hooks/result-file-marker-guard.py ~/.claude/hooks/
REPO="$REPO" python3 - <<'PY'
import json, os, shlex
sp = os.path.expanduser("~/.claude/settings.json"); s = json.load(open(sp))
# SHELL-QUOTE BOTH PATHS. Claude Code stores `command` as a shell string and
# reparses it when the hook fires, so an unquoted path containing a space --
# e.g. the app install shape ~/Library/Application Support/.../sutando -- is
# split before _repo_root() ever sees it, and the hook goes silently INERT.
# Same class the repo already guards in src/install-claude-hooks.sh.
hook = shlex.quote(os.path.expanduser("~/.claude/hooks/result-file-marker-guard.py"))
cmd = f"python3 {hook} --repo {shlex.quote(os.environ['REPO'])}"
pre = s.setdefault("hooks", {}).setdefault("PreToolUse", [])
for m in ("Write", "Edit", "MultiEdit"):
    blk = next((b for b in pre if b.get("matcher") == m), None)
    if blk is None: pre.append({"matcher": m, "hooks": [{"type": "command", "command": cmd}]})
    elif cmd not in [h.get("command") for h in blk["hooks"]]: blk["hooks"].append({"type": "command", "command": cmd})
json.dump(s, open(sp, "w"), indent=2)
PY
```

**Verify it is not inert after deploying.** An unconfigured hook only complains on
stderr, so confirm it actually denies:

```bash
WS="$(bash scripts/sutando-config.sh workspace)"
printf '{"tool_name":"Write","tool_input":{"file_path":"%s/results/task-probe.txt","content":"x [file: /etc/hosts]"}}' "$WS" \
  | python3 ~/.claude/hooks/result-file-marker-guard.py --repo "$REPO"
# expect a JSON object with "permissionDecision": "deny"
```

Tests: `python3 tests/result-file-marker-guard.test.py`
