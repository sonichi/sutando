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

Config paths are env-overridable for testing: `SUTANDO_DISCORD_ACCESS_FILE`,
`SUTANDO_DISCORD_ENV_FILE`, `SUTANDO_WORKSPACE`. Test: `python3 tests/context-source-guard.test.py`.
