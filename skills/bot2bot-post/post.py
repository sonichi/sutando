#!/usr/bin/env python3
"""Post a coordination message from this bot to the #bot2bot channel.

Usage:
    python3 skills/bot2bot-post/post.py [--to <peer|id>] <kind> <text>
    python3 skills/bot2bot-post/post.py claim "refactor X, ETA 20m"
    python3 skills/bot2bot-post/post.py --to pro ping "your take on the WIRE topic?"
    python3 skills/bot2bot-post/post.py --to lucy opinion "disagreement axis below"
    python3 skills/bot2bot-post/post.py done "shipped PR #472"

Kinds: claim | blocked | done | ping | opinion
Peers (for --to): a name from ~/.claude/channels/discord/peers.json, or a raw numeric id

The target channel ID is read from `$CLAUDE_CONFIG_DIR/channels/discord/access.json`:
entries tagged with `{"role": "bot2bot", ...}` in `groups` are candidates. We
pick the first such channel. If none is tagged, we fall back to the first
group whose value is just `true` (legacy convention), or error out.

Recipient targeting: pass `--to <peer|id>` to @-mention a specific peer. With
`--to`, the target channel is derived FROM the recipient — we post to a channel
whose `allowFrom` contains them (recipient-first routing), and REFUSE rather
than dead-letter if they are in no accessible channel. This fixes the 2026-07-27
mis-route where contributor pings (qingyun/Rui/john) were sent to the fleet-only
#bot2bot they aren't members of, so they silently went nowhere; and it guards
the bot-vs-human-owner id confusion (different ids → different channels). Without
`--to`, this is a fleet broadcast to the `role:bot2bot` channel, and the other
bot's id is read from that channel's `allowFrom` (fine only while exactly one
peer is allowlisted there — mis-fires with 3+, so prefer `--to`). The resulting
`<@id>` mention is prepended so the receiving bot's bridge processes it as a
task (discord-bridge.py line 244 exception).

Requires DISCORD_BOT_TOKEN in $CLAUDE_CONFIG_DIR/channels/discord/.env.
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Claude Code per-user home. Mirrors src/util_paths.py `claude_home_path()`
# (the workspace-revamp resolver). Resolution order, per that branch:
#   1. $CLAUDE_CONFIG_DIR — M2 workspace-scoped path (set by claude-sutando /
#      start-cli.sh so bridges see the workspace's .claude-sutando/)
#   2. $CLAUDE_HOME — legacy alt-host override, kept for tests
#   3. ~/.claude — vanilla default
# Replicated inline (not imported) so this standalone skill stays dependency-free.
def _claude_home() -> Path:
    for env in ("CLAUDE_CONFIG_DIR", "CLAUDE_HOME"):
        v = os.environ.get(env)
        if v:
            return Path(os.path.expanduser(v))
    return Path.home() / ".claude"


_DISCORD_DIR = _claude_home() / "channels" / "discord"
ACCESS_JSON = _DISCORD_DIR / "access.json"
ENV_FILE = _DISCORD_DIR / ".env"
VALID_KINDS = {"claim", "blocked", "done", "ping", "opinion"}


def load_token() -> str:
    """Load DISCORD_BOT_TOKEN from the Discord channel's .env."""
    if not ENV_FILE.exists():
        sys.exit(f"ERROR: {ENV_FILE} not found")
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip("'\"")
    sys.exit("ERROR: DISCORD_BOT_TOKEN not in env")


def load_access() -> dict:
    if not ACCESS_JSON.exists():
        sys.exit(f"ERROR: {ACCESS_JSON} not found")
    return json.loads(ACCESS_JSON.read_text())


def resolve_bot2bot_channel(access: dict) -> str:
    """Pick the bot2bot channel from access.json.

    Preferred: groups entries with `{"role": "bot2bot", ...}`.
    Fallback: groups entries whose value is literal `true` (legacy).
    """
    groups = access.get("groups", {})
    # Preferred: explicitly tagged
    for cid, cfg in groups.items():
        if isinstance(cfg, dict) and cfg.get("role") == "bot2bot":
            return cid
    # Fallback: first `true`-valued group (legacy — likely the bot2bot one)
    for cid, cfg in groups.items():
        if cfg is True:
            return cid
    sys.exit("ERROR: no bot2bot channel found in access.json.groups")


USER_AGENT = "DiscordBot (https://github.com/sonichi/sutando, 1.0)"


def get_self_id(token: str) -> str:
    """Discord GET /users/@me → this bot's user ID."""
    req = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["id"]


def resolve_other_bot(access: dict, self_id: str, channel_id: str):
    """Find the other bot's user ID from the bot2bot CHANNEL's allowFrom.

    The top-level `allowFrom` is owner-only by the tier-isolation invariant
    (see `scripts/validate-access-tiers.py`) — sibling bots must not appear
    there or they'd be classified as access_tier=owner instead of team.
    The sibling-bot ID lives in the #bot2bot channel's allowFrom.

    Falls back to the top-level allowFrom for older configs that haven't
    migrated to channel-level allowFrom yet.
    """
    ch_cfg = access.get("groups", {}).get(channel_id)
    allow: list = []
    if isinstance(ch_cfg, dict):
        allow = list(ch_cfg.get("allowFrom", []))
    # Fallback: legacy configs that only have top-level allowFrom
    if not allow:
        allow = list(access.get("allowFrom", []))
    others = [uid for uid in allow if uid != self_id]
    if not others:
        return None
    # Heuristic: the sibling-bot ID will not match self_id. The owner's
    # user_id may also appear in the channel allowFrom; to pick the bot,
    # prefer the ID that is NOT in the top-level allowFrom (owner-only).
    global_allow = set(str(x) for x in access.get("allowFrom", []))
    bot_candidates = [uid for uid in others if str(uid) not in global_allow]
    if bot_candidates:
        return bot_candidates[0]
    # Last resort: any non-self ID (legacy configs where owner+bot share the
    # top-level allowFrom).
    return others[0]


def resolve_channel_for_recipient(access: dict, recipient_id: str):
    """Return a channel whose allowFrom contains `recipient_id`, or None.

    RECIPIENT-FIRST routing (the dead-letter fix, 2026-07-27). Instead of always
    posting to the fixed `role:bot2bot` channel and *hoping* the recipient reads
    it, resolve the channel FROM the recipient: post only to a channel they are
    actually a member of. If the recipient is in NO channel's allowFrom, the
    caller MUST refuse rather than dead-letter.

    Why: `--to <contributor>` used to post to the fleet-only #bot2bot, whose
    allowFrom is Air/Mini/Pro only — so pings to qingyun/Rui/john silently went
    nowhere. Deriving the channel from `allowFrom` membership makes that
    mis-route impossible by construction.

    A recipient may be in several channels (any of them reaches them, so the
    no-dead-letter guarantee holds); pick deterministically (lowest channel id).
    """
    rid = str(recipient_id)
    matches = [
        cid
        for cid, cfg in access.get("groups", {}).items()
        if isinstance(cfg, dict)
        and rid in {str(x) for x in cfg.get("allowFrom", [])}
    ]
    return sorted(matches)[0] if matches else None


def post(channel_id: str, text: str, token: str):
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    body = json.dumps({"content": text}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: Discord API {e.code}: {e.read().decode()}")


# Peer roster lives in per-host config, NOT hardcoded here: this script is
# shared repo code, so baking one fleet's Discord IDs in would couple it to a
# single roster (and the IDs already live in the bot2bot channel's allowFrom).
# Format: { "<name>": "<discord-user-id>", ... } at the path below. Absent file
# → empty roster (raw numeric --to still works; no-name --to auto-resolves off
# allowFrom). SELF is never listed — it's resolved per-host via GET /users/@me.
PEERS_CONFIG_PATH = str(_DISCORD_DIR / "peers.json")


def load_peer_roster() -> dict:
    """Load {name: id} from PEERS_CONFIG_PATH. Empty dict if missing/malformed."""
    try:
        with open(PEERS_CONFIG_PATH) as f:
            data = json.load(f)
        return {str(k).lower(): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (FileNotFoundError, OSError, ValueError):
        return {}


def resolve_to_target(value: str) -> str:
    """Resolve a --to value (a roster name or a raw numeric ID) to a user ID."""
    v = value.strip().lstrip("@")
    if v.isdigit():
        return v
    roster = load_peer_roster()
    key = v.lower()
    if key in roster:
        return roster[key]
    known = ", ".join(sorted(roster)) if roster else f"none configured in {PEERS_CONFIG_PATH}"
    sys.exit(
        f"ERROR: --to {value!r} is neither a numeric ID nor a known peer ({known})"
    )


def main():
    argv = sys.argv[1:]
    # Optional explicit recipient: --to <name|id>. When given, the @-mention
    # targets exactly that peer instead of guessing the sole other bot in the
    # channel's allowlist (the old behavior mis-fired when >1 peer existed).
    to_target = None
    if "--to" in argv:
        i = argv.index("--to")
        if i + 1 >= len(argv):
            sys.exit("ERROR: --to requires a value (peer name or numeric ID)")
        to_target = argv[i + 1]
        del argv[i : i + 2]

    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)
    kind = argv[0]
    text = " ".join(argv[1:])
    if kind not in VALID_KINDS:
        sys.exit(f"ERROR: kind must be one of {sorted(VALID_KINDS)}, got {kind!r}")

    token = load_token()
    access = load_access()
    self_id = get_self_id(token)

    if to_target is not None:
        other_id = resolve_to_target(to_target)
        if other_id == self_id:
            sys.exit("ERROR: --to resolves to this bot itself; pick a peer")
        # RECIPIENT-FIRST routing: post to a channel the recipient is actually
        # in, NOT the fixed bot2bot channel. Refuse rather than dead-letter — the
        # 2026-07-27 mis-route where contributor pings went to the fleet-only
        # #bot2bot they aren't members of.
        channel_id = resolve_channel_for_recipient(access, other_id)
        if channel_id is None:
            sys.exit(
                f"ERROR: recipient {other_id} is in no channel's allowFrom in "
                f"{ACCESS_JSON} — refusing to post (it would be a dead letter). "
                "Add them to the target channel's allowFrom, or check the id "
                "(a bot vs its human owner are different ids)."
            )
    else:
        # No explicit recipient → fleet broadcast to the bot2bot channel.
        channel_id = resolve_bot2bot_channel(access)
        other_id = resolve_other_bot(access, self_id, channel_id)

    prefix = f"<@{other_id}> " if other_id else ""
    message = f"{prefix}{kind}: {text}"

    result = post(channel_id, message, token)
    print(f"Posted to #{channel_id} (msg_id {result.get('id')}): {message[:80]}")


if __name__ == "__main__":
    main()
