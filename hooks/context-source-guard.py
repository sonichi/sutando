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

Fail-OPEN for pre-check errors (parsing, tool detection, serving/blacklist lookup) and
for unknown-serving-channel (interactive/diagnostic reads outside task processing).
Once a non-empty blacklist is confirmed, any subsequent error is fail-CLOSED — never
let an exception bypass an active guard.

Deploy: copy this file and _shell_scan.py to the Claude hooks folder ($CLAUDE_CONFIG_DIR/hooks/),
then register under PreToolUse for BOTH the "Bash" and "Read" matchers in settings.json.
See hooks/README.md. Config paths are
env-overridable (SUTANDO_DISCORD_ACCESS_FILE / SUTANDO_DISCORD_ENV_FILE) for testing.
"""
import sys
import json
import os
import re
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import _shell_scan  # noqa: E402  (sibling module; path set above)
except ImportError:
    _shell_scan = None

# Resolve the Claude config dir via $CLAUDE_CONFIG_DIR (set by Claude Code),
# matching read_discord_channel.py/discord-bridge's claude_home_path — NOT a
# hardcoded ~/.claude. On a relocated install the hardcode read a DIFFERENT,
# stale access.json than the bridge writes, so a configured contextNotFrom was
# invisible to the hook → it silently failed OPEN (the one component you least
# want failing open). Flagged by Sutando-Pro on PR #1698, 2026-06-18.
# Mirrors util_paths.claude_home_path ($CLAUDE_CONFIG_DIR -> $CLAUDE_HOME -> ~/.claude); standalone hook.
_CFG = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_HOME") or os.path.expanduser("~/.claude")
ACCESS_FILE = os.environ.get("SUTANDO_DISCORD_ACCESS_FILE",
                             os.path.join(_CFG, "channels", "discord", "access.json"))
ENV_FILE = os.environ.get("SUTANDO_DISCORD_ENV_FILE",
                          os.path.join(_CFG, "channels", "discord", ".env"))
# Workspace resolution (M0/#1440 residual, #1698): SUTANDO_WORKSPACE was dropped
# for resolution in v0.8 and its old default was the pre-M0 legacy home-dir
# location — so this hook stranded active-serving-channel.json in the LEGACY
# workspace, a different dir than discord-bridge.py (M0 resolve_workspace) reads,
# and the guard then couldn't find the serving channel → silently failed open.
# Derive the workspace from CLAUDE_CONFIG_DIR instead: the Claude Code project
# tree lives at `<workspace>/.claude-sutando` per the workspace contract, so the
# parent of _CFG IS the workspace (and it moves with a config-relocated
# workspace). No subprocess — this hook fires on every Bash/Read; no __file__
# walk — that's the bundled-symlink anti-pattern in src/workspace_default.py.
# Walk up to the nearest `.claude-sutando` ANCESTOR (not just an exact-leaf
# basename match): src/startup.sh floats narrowing CLAUDE_CONFIG_DIR to a per-host
# subdir (`<workspace>/.claude-sutando/hosts/<host>`); there the leaf is the
# hostname and an exact match would wrongly fall through to the fallback.
_cfg_norm = os.path.normpath(_CFG)
WS = None
_p = _cfg_norm
while True:
    if os.path.basename(_p) == ".claude-sutando":
        WS = os.path.dirname(_p)
        break
    _parent = os.path.dirname(_p)
    if _parent == _p:  # reached filesystem root, no `.claude-sutando` ancestor
        break
    _p = _parent
if WS is None:
    # CLAUDE_CONFIG_DIR unset (→ _CFG=~/.claude, no `.claude-sutando` ancestor) or
    # otherwise unresolvable. Fail open to the SAME canonical last-ditch the rest of
    # the system uses — workspace_default.default_workspace_dir() returns
    # ~/sutando-workspace (_DEFAULT_SUBPATH=("sutando-workspace",)). NOT ~/.sutando
    # (pre-v0.8). Not imported here: this hook is standalone-deployed to ~/.claude/hooks/.
    WS = os.path.expanduser("~/sutando-workspace")
STATE = os.path.join(WS, "state", "active-serving-channel.json")
API = "https://discord.com/api/v10"
UA = "DiscordBot (https://github.com/sonichi/sutando, 1.0)"
_TASK_RE = re.compile(r"task-[^\s/\\'\"]+\.txt$")
_TASKPATH_RE = re.compile(r"([^\s'\"]*task-[^\s/\\'\"]+\.txt)")
_CH_READ_RE = re.compile(r"channels/(\d+)/messages")
_GUILD_CACHE = os.path.join(WS, "state", ".channel-guild-cache.json")
_READER_VALUE_OPTIONS = {
    "discord-read.py": {"--limit", "--after", "--before", "--until", "--serving"},
    "read_discord_channel.py": {"--serving", "--target", "-n"},
}
_READER_FLAG_OPTIONS = {
    "discord-read.py": {"--full", "--jsonl", "--operator"},
    "read_discord_channel.py": set(),
}


_SHELL_WRAPPERS = {"sh", "bash", "zsh", "dash", "ksh"}
_PREFIX_COMMANDS = {"env", "exec", "command", "builtin", "nohup", "setsid", "sudo"}


def _unwrap_segments(tokens, depth=0):
    """Peel `bash -c '...'` wrappers and prefix commands; None = unparseable."""
    if depth > 4:
        return [tokens]
    stripped = list(tokens)
    while stripped and os.path.basename(stripped[0]) in _PREFIX_COMMANDS:
        head = os.path.basename(stripped.pop(0))
        if head == "env":
            while stripped and (stripped[0].startswith("-") or "=" in stripped[0]):
                stripped.pop(0)
    if not stripped:
        return [tokens]
    if os.path.basename(stripped[0]) in _SHELL_WRAPPERS:
        for i in range(1, len(stripped)):
            arg = stripped[i]
            if arg == "-c":
                if i + 1 >= len(stripped):
                    return [tokens]
                try:
                    inner = _shell_scan.segments(stripped[i + 1])
                except Exception:
                    return None
                out = []
                for inner_segment in inner:
                    sub = _unwrap_segments([w.text for w in inner_segment], depth + 1)
                    if sub is None:
                        return None
                    out.extend(sub)
                return out or [tokens]
            if not arg.startswith("-"):
                break  # a script file argument, not a -c payload
    return [stripped]


# Python flags taking an argument while allowing a script operand after them.
# `-c`/`-m` are omitted because they consume all subsequent tokens as script/module.
_PYTHON_FLAGS_WITH_ARG = {"-W", "-X", "--check-hash-based-pycs"}


def _is_python_invocation(tokens, i):
    """tokens[i] is a reader script word: invoked at argv[0], or after a python
    executable (its own flags allowed, plus `uv run`/`env` launcher prefixes)."""
    if i == 0:
        return True
    j = i - 1
    while j > 0:
        if tokens[j - 1] in _PYTHON_FLAGS_WITH_ARG:
            j -= 2
        elif tokens[j].startswith("-"):
            j -= 1
        else:
            break
    if not os.path.basename(tokens[j]).startswith("python"):
        return False
    prefix = tokens[:j]
    return not prefix or prefix == ["uv", "run"]


def _reader_cli_targets(cmd):
    try:
        segments = _shell_scan.segments(cmd)
    except Exception:
        if re.search(r"(?:^|[/\s])(?:discord-read|read_discord_channel)\.py(?:\s|$)", cmd):
            return True, [], True
        return False, [], False
    token_segments = []
    unparseable_inner = False
    for segment in segments:
        unwrapped = _unwrap_segments([word.text for word in segment])
        if unwrapped is None:
            unparseable_inner = True
            continue
        token_segments.extend(unwrapped)
    if unparseable_inner and re.search(
            r"(?:^|[/\s])(?:discord-read|read_discord_channel)\.py(?:\s|$)", cmd):
        return True, [], True
    if not token_segments and re.search(
            r"(?:^|[/\s])(?:discord-read|read_discord_channel)\.py(?:\s|$)", cmd):
        return True, [], True
    invocations = []
    for tokens in token_segments:
        for i, token in enumerate(tokens):
            if os.path.basename(token) not in _READER_VALUE_OPTIONS:
                continue
            if _is_python_invocation(tokens, i):
                invocations.append((tokens, i))
    if not invocations:
        return False, [], False
    targets = []
    for tokens, index in invocations:
        script = os.path.basename(tokens[index])
        value_options = _READER_VALUE_OPTIONS[script]
        flag_options = _READER_FLAG_OPTIONS[script]
        args = tokens[index + 1:]
        positional = []
        i = 0
        target_option = "--target" if script == "read_discord_channel.py" else None
        target_values = []
        while i < len(args):
            arg = args[i]
            if arg.startswith("--") and "=" in arg:
                option, value = arg.split("=", 1)
                if option not in value_options or not value:
                    return True, [], True
                if option == target_option:
                    target_values.append(value)
                i += 1
                continue
            if arg in flag_options:
                i += 1
                continue
            if arg in value_options:
                if i + 1 >= len(args) or args[i + 1].startswith("-"):
                    return True, [], True
                if arg == target_option:
                    target_values.append(args[i + 1])
                i += 2
                continue
            if arg.startswith("-"):
                return True, [], True
            positional.append(arg)
            i += 1
        if target_option:
            if positional or len(target_values) != 1:
                return True, [], True
            targets.extend(target_values)
        else:
            if len(positional) != 1:
                return True, [], True
            targets.extend(positional)
    return True, targets, False


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

    # A missing _shell_scan sibling means the deploy copied the hook alone —
    # deny any command that could be a channel read rather than crashing open.
    if _shell_scan is None:
        if _CH_READ_RE.search(cmd) or re.search(
                r"(?:^|[/\s])(?:discord-read|read_discord_channel)\.py(?:\s|$)", cmd):
            _deny("CONTEXT-SOURCE GUARD: the _shell_scan.py sibling is missing — deploy "
                  "hooks/_shell_scan.py next to this hook. [context-source-guard]")
        sys.exit(0)

    # Serving detection is NOT only via the Read tool — agents frequently `cat`/
    # `grep`/`head` the task file in Bash. Whenever a Bash command references a
    # task-file path, record its channel_id as the serving channel too.
    mt = _TASKPATH_RE.search(cmd)
    if mt:
        cid = _read_channel_id_from_task(mt.group(1).strip("'\""))
        if cid:
            _record_serving(cid)

    targets = _CH_READ_RE.findall(cmd)
    reader_present, reader_targets, reader_parse_error = _reader_cli_targets(cmd)
    targets.extend(reader_targets)
    if not targets and not reader_present:
        sys.exit(0)  # not a channel read

    serving = _active_serving()
    if not serving:
        sys.exit(0)  # unknown serving channel -> fail-open (interactive/diagnostic)
    blacklist = _blacklist(serving)
    if not blacklist:
        sys.exit(0)  # serving channel pulls from anywhere
    if reader_parse_error or any(not re.fullmatch(r"\d+", str(cid)) for cid in reader_targets):
        _deny("CONTEXT-SOURCE GUARD: a Discord reader target could not be verified; "
              "use a literal numeric channel ID while a contextNotFrom blacklist is active. "
              "[context-source-guard]")

    # Non-empty blacklist confirmed — fail-CLOSED on any exception from here.
    # An exception that escapes the outer try/except would fail-open; we must
    # not allow that once we know a guard is in effect.
    try:
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
    except SystemExit:
        raise  # let _deny()'s sys.exit(0) and the normal exit propagate
    except Exception as e:
        _deny(f"CONTEXT-SOURCE GUARD: internal error while checking channel(s) {targets} against "
              f"blacklist for serving channel {serving} — refusing the read rather than risk leaking "
              f"blacklisted content. Error: {type(e).__name__} [context-source-guard]")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # fail-open for pre-check errors (parsing, tool detection, serving lookup)
        print(f"[context-source-guard] non-fatal pre-check error, allowing: {e}", file=sys.stderr)
        sys.exit(0)
