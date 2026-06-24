#!/usr/bin/env python3
"""Context-source guard — PreToolUse hook enforcing the contextNotFrom rule on the
agent's OWN channel reads (the path the gated reader + CLAUDE.md instruction can't
force, because a raw `curl` bypasses an instruction).

Rule: "if a channel is in contextNotFrom, skip its content when building context."
A private channel's content can't be un-seen once it's in the model's context, so
this blocks the READ before the content ever lands.

SERVING-RELATIVE: the blacklist is the `contextNotFrom` of the channel currently
being SERVED — not a global ban on the target. We learn the serving channel from
the task the agent is processing:
  * PreToolUse[Read] of a tasks/task-*.txt        -> record its channel_id as "serving"
  * PreToolUse[Bash] that reads a task-file path  -> same (agents often `cat` the task)
  * PreToolUse[Bash] curling `…/channels/<id>/messages` -> if <id> (or its guild) is
    in the serving channel's contextNotFrom -> DENY (nothing fetched).
So serving a private channel can still read that private channel; serving a public
channel whose contextNotFrom lists the private guild cannot.

Fail-OPEN on any error / unknown serving channel — EXCEPT once a non-empty blacklist
applies and the target guild can't be resolved, where it fails CLOSED.

Deploy: copy to ~/.claude/hooks/ and register under PreToolUse for BOTH the "Bash"
and "Read" matchers in ~/.claude/settings.json. See hooks/README.md. Config paths are
env-overridable (SUTANDO_DISCORD_ACCESS_FILE / SUTANDO_DISCORD_ENV_FILE) for testing.
"""
import sys, json, os, re, time, urllib.request

# Resolve the Claude config dir via $CLAUDE_CONFIG_DIR (set by Claude Code),
# matching read_discord_channel.py/discord-bridge's claude_home_path — NOT a
# hardcoded ~/.claude. On a relocated install the hardcode read a DIFFERENT,
# stale access.json than the bridge writes, so a configured contextNotFrom was
# invisible to the hook → it silently failed OPEN (the one component you least
# want failing open). Flagged by Sutando-Pro on PR #1698, 2026-06-18.
_CFG = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
ACCESS_FILE = os.environ.get("SUTANDO_DISCORD_ACCESS_FILE",
                             os.path.join(_CFG, "channels", "discord", "access.json"))
ENV_FILE = os.environ.get("SUTANDO_DISCORD_ENV_FILE",
                          os.path.join(_CFG, "channels", "discord", ".env"))
WS = os.path.expanduser(
    os.environ.get("SUTANDO_WORKSPACE", "~/.sutando/workspace").replace("~", os.path.expanduser("~"))
)
STATE = os.path.join(WS, "state", "active-serving-channel.json")
API = "https://discord.com/api/v10"
UA = "DiscordBot (https://github.com/sonichi/sutando, 1.0)"
_TASK_RE = re.compile(r"task-\d+\.txt$")
_TASKPATH_RE = re.compile(r"([^\s'\"]*task-\d+\.txt)")  # a task-file path inside a Bash command
_CH_READ_RE = re.compile(r"channels/(\d+)/messages")
_GUILD_CACHE = os.path.join(WS, "state", ".channel-guild-cache.json")


def _read_channel_id_from_task(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("channel_id:"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


def _record_serving(cid):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            json.dump({"channel_id": str(cid), "ts": int(time.time())}, f)
    except Exception:
        pass


def _active_serving():
    try:
        with open(STATE) as f:
            return str(json.load(f).get("channel_id") or "") or None
    except Exception:
        return None


def _blacklist(serving_cid):
    try:
        data = json.load(open(ACCESS_FILE))
        grp = data.get("groups", {}).get(str(serving_cid))
        if isinstance(grp, dict):
            return {str(c) for c in (grp.get("contextNotFrom") or [])}
    except Exception:
        pass
    return set()


def _token():
    t = os.environ.get("DISCORD_BOT_TOKEN")
    if t:
        return t
    try:
        for line in open(ENV_FILE):
            line = line.strip()
            if line.startswith("DISCORD_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def _cached_guild(cid):
    try:
        return json.load(open(_GUILD_CACHE)).get(str(cid))
    except Exception:
        return None


def _cache_guild(cid, gid):
    try:
        d = {}
        if os.path.exists(_GUILD_CACHE):
            d = json.load(open(_GUILD_CACHE))
        d[str(cid)] = gid
        os.makedirs(os.path.dirname(_GUILD_CACHE), exist_ok=True)
        json.dump(d, open(_GUILD_CACHE, "w"))
    except Exception:
        pass


def _resolve_guild(cid, token):
    g = _cached_guild(cid)
    if g is not None:
        return g
    if not token:
        return None
    try:
        req = urllib.request.Request(f"{API}/channels/{cid}",
                                     headers={"Authorization": f"Bot {token}", "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            gid = json.loads(r.read().decode("utf-8")).get("guild_id")
        gid = str(gid) if gid is not None else None
        if gid:
            _cache_guild(cid, gid)
        return gid
    except Exception:
        return None


def _deny(reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def main():
    data = json.loads(sys.stdin.read())
    tool = data.get("tool_name", "")
    ti = data.get("tool_input", {}) or {}

    # --- Read of a task file: record which channel we're now serving ---
    if tool == "Read":
        fp = (ti.get("file_path", "") or "")
        if _TASK_RE.search(fp.replace("\\", "/")):
            cid = _read_channel_id_from_task(fp)
            if cid:
                _record_serving(cid)
        sys.exit(0)  # Read is never denied

    if tool != "Bash":
        sys.exit(0)

    cmd = ti.get("command", "") or ""

    # Serving detection is NOT only via the Read tool — agents frequently `cat`/
    # `grep`/`head` the task file in Bash. Whenever a Bash command references a
    # task-file path, record its channel_id as the serving channel too.
    mt = _TASKPATH_RE.search(cmd)
    if mt:
        cid = _read_channel_id_from_task(mt.group(1).strip("'\""))
        if cid:
            _record_serving(cid)

    targets = _CH_READ_RE.findall(cmd)
    if not targets:
        sys.exit(0)  # not a channel read

    serving = _active_serving()
    if not serving:
        sys.exit(0)  # unknown serving channel -> fail-open (interactive/diagnostic)
    blacklist = _blacklist(serving)
    if not blacklist:
        sys.exit(0)  # serving channel pulls from anywhere

    token = _token()
    for cid in targets:
        if str(cid) in blacklist:
            _deny(f"CONTEXT-SOURCE GUARD: serving channel {serving} forbids pulling from "
                  f"#{cid} (channel-level contextNotFrom). Blocked before any content was read "
                  f"into context. [context-source-guard]")
        guild = _resolve_guild(cid, token)
        if guild is None:
            _deny(f"CONTEXT-SOURCE GUARD: serving channel {serving} has a contextNotFrom "
                  f"blacklist and the guild of #{cid} could not be verified — refusing the read "
                  f"rather than risk pulling a blacklisted guild (fail-closed). [context-source-guard]")
        if guild in blacklist:
            _deny(f"CONTEXT-SOURCE GUARD: serving channel {serving} forbids pulling from guild "
                  f"{guild} (contextNotFrom); #{cid} is in it. Blocked before any content was read "
                  f"into context. [context-source-guard]")
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail-open: never wedge normal operation
        print(f"[context-source-guard] non-fatal error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
