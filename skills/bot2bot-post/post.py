#!/usr/bin/env python3
"""Post a coordination message from this bot to the #bot2bot channel.

Usage:
    python3 skills/bot2bot-post/post.py [--to <peer|id>] <kind> <text>
    python3 skills/bot2bot-post/post.py [--to <peer|id>] <kind> --body-file <path>
    python3 skills/bot2bot-post/post.py claim "refactor X, ETA 20m"
    python3 skills/bot2bot-post/post.py --to pro ping "your take on the WIRE topic?"
    python3 skills/bot2bot-post/post.py --to lucy opinion "disagreement axis below"
    python3 skills/bot2bot-post/post.py done "shipped PR #472"

Kinds: claim | blocked | done | ping | nack | opinion
--body-file <path>: read the body from a FILE instead of argv. Use it for any
prose containing backticks or apostrophes — an apostrophe closes a
single-quoted shell argument and re-arms the backticks, truncating the message
at that point while the send still reports success.
Peers (for --to): a name from ~/.claude/channels/discord/peers.json, or a raw numeric id

The target channel ID is read from `$CLAUDE_CONFIG_DIR/channels/discord/access.json`:
entries tagged with `{"role": "bot2bot", ...}` in `groups` are candidates. We
pick the first such channel. If none is tagged, we fall back to the first
group whose value is just `true` (legacy convention), or error out.

Recipient targeting: pass `--to <peer|id>` to @-mention a specific peer. This
is the correct way when more than one peer exists — the old auto-resolve
assumed a SINGLE other bot and silently mis-fired otherwise (it mentioned Mini
for a post addressed to Pro, 2026-06-06; on 2026-07-29 both Pro and Mini
mispinged Air the same way, each stray ping triggering the target's team-tier
auto-refusal). Without `--to`, the peer id is auto-resolved from the bot2bot
CHANNEL's `allowFrom` (excluding this bot, via GET /users/@me) ONLY when
exactly one peer exists; with 2+ peers the post goes out WITHOUT a mention
(peers ingest the channel anyway) and a stderr NOTE says to use `--to`. The
resulting `<@id>` mention, when present, makes the receiving bot's bridge
process the post as a task (discord-bridge.py line 244 exception).

SCOPE GUARD (2026-07-27): bot2bot-post only ever posts to the one bot2bot channel
(resolved from access.json's `role: bot2bot` tag — NOT hardcoded). A `--to <X>`
where X is NOT a member of that channel is REFUSED with a loud error, instead of
silently posting where X will never see it (the dead-letter, and the
bot-vs-human-owner id mix-up — a bot and its owner are different ids). Where to
route a message that ISN'T bot2bot coordination is the caller's judgment (it
depends on the message + recipient + topic-home), so the guard does NOT
prescribe a destination — it just refuses and says the recipient isn't a member.

Requires DISCORD_BOT_TOKEN in $CLAUDE_CONFIG_DIR/channels/discord/.env.
"""
import json
import os
import pathlib
import sys
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
VALID_KINDS = {"claim", "blocked", "done", "ping", "opinion", "nack"}


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


def get_self_id(token: str) -> str:
    """Discord GET /users/@me → this bot's user ID (via the shared client)."""
    return str(_client(token).get_json("/users/@me")["id"])


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
    if len(bot_candidates) == 1:
        return bot_candidates[0]
    if len(bot_candidates) > 1:
        # Ambiguous multi-peer fleet: NEVER guess. 2026-07-29 double misfire:
        # with three bots allowlisted, the old `[0]` pick sent Pro's ping to
        # Air (meant for Mini) and Mini's to Air (meant for Pro) — and every
        # stray ping triggers the target's team-tier sandbox auto-refusal, a
        # reply-noise cascade. An unaddressed post is strictly better: peers
        # ingest the channel anyway, and callers who need a specific peer's
        # task-queue attention say so with --to.
        print(
            f"NOTE: {len(bot_candidates)} peer bots in the bot2bot allowlist — "
            "posting WITHOUT a mention (use --to <peer|id> to address one).",
            file=sys.stderr,
        )
        return None
    # Legacy configs where owner+bot share the top-level allowFrom leave no
    # bot_candidates. Same rule: exactly one non-self id → safe; else no guess.
    if len(others) == 1:
        return others[0]
    print(
        f"NOTE: {len(others)} non-self ids in allowFrom and none identifiable "
        "as the sole peer bot — posting WITHOUT a mention (use --to).",
        file=sys.stderr,
    )
    return None


def _recipient_in_channel(access: dict, channel_id: str, recipient_id: str) -> bool:
    """Whether `recipient_id` is in `channel_id`'s allowFrom (the scope guard).

    bot2bot-post only ever posts to the bot2bot channel; this checks the
    recipient is actually a member there, so a `--to` for someone who isn't
    (someone only in a different channel, or a bot's human owner) fails loudly
    instead of dead-lettering into a channel they can't see.
    """
    cfg = access.get("groups", {}).get(channel_id)
    if not isinstance(cfg, dict):
        return False
    return str(recipient_id) in {str(x) for x in cfg.get("allowFrom", [])}


#: Discord rejects a message whose `content` exceeds this many characters.
DISCORD_MAX_CONTENT = 2000


def check_length(text: str, overhead: int = 0) -> "str | None":
    """The refusal message if `text` is over the limit, else None.

    Measures the FINAL content — the routing prefix and `kind:` tag are part of
    what Discord counts, and they are the part callers forget. A body sized to
    exactly 2000 is already over once `<@id> done: ` is prepended.

    Returned rather than raised so the caller decides how to fail, and so a test
    can assert the wording without trapping SystemExit.
    """
    n = len(text)
    if n <= DISCORD_MAX_CONTENT:
        return None
    over = n - DISCORD_MAX_CONTENT
    room = DISCORD_MAX_CONTENT - overhead
    detail = (
        f" The count INCLUDES the {overhead}-char routing prefix, so the body "
        f"itself must be <= {room} chars."
        if overhead else ""
    )
    return (
        f"ERROR: content is {n} chars — {over} over Discord's "
        f"{DISCORD_MAX_CONTENT} limit. NOTHING WAS SENT.{detail} "
        f"Cut ~{over} chars and retry."
    )


def post(channel_id: str, text: str, token: str, overhead: int = 0):
    # Refuse locally rather than spending a round trip to be told the same thing
    # by a 400 that reports no numbers. The API's error names the limit but not
    # the actual length or the overage, so the caller cannot tell how much to
    # cut without measuring again.
    problem = check_length(text, overhead)
    if problem:
        sys.exit(problem)
    receipt, _status, body = _client(token).send_message_with_response(
        channel_id, {"content": text})
    if receipt.outcome is DeliveryOutcome.CONFIRMED and isinstance(body, dict):
        return body
    hint = (" The post MAY have landed — check the channel before retrying."
            if receipt.outcome is DeliveryOutcome.OUTCOME_UNKNOWN
            else " NOTHING WAS SENT.")
    sys.exit(f"ERROR: Discord send {receipt.outcome.value}: {receipt.detail}.{hint}")


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


_repo = next(p for p in pathlib.Path(__file__).resolve().parents
             if (p / "src" / "body_file.py").is_file())
sys.path.insert(0, str(_repo / "src"))
from body_file import MAX_BODY_BYTES, read_body_file as _read_body_file  # noqa: E402
from channels.discord.post_gate import make_client  # noqa: E402  — the one Discord POST chokepoint
from outbox import DeliveryOutcome  # noqa: E402


def _client(token: str):
    """Shared-DiscordRestClient seam for tests; keeps the pre-client 10s cap."""
    return make_client(token, timeout=10)


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

    # --body-file must be the FIRST body token: recognising it anywhere would
    # turn ordinary prose ("document --body-file usage") into a file read.
    if argv and argv[0] in VALID_KINDS and len(argv) > 1 and argv[1] == "--body-file":
        if len(argv) < 3:
            sys.exit("ERROR: --body-file requires a path")
        if len(argv) > 3:
            sys.exit(f"ERROR: --body-file takes the body; drop {argv[3:]!r}")
        kind = argv[0]
        text = _read_body_file(argv[2])
        if not text.strip():
            sys.exit(f"ERROR: --body-file {argv[2]!r} is empty — refusing to post")
    else:
        if len(argv) < 2:
            print(__doc__, file=sys.stderr)
            sys.exit(1)
        kind = argv[0]
        text = " ".join(argv[1:])
    if kind not in VALID_KINDS:
        sys.exit(f"ERROR: kind must be one of {sorted(VALID_KINDS)}, got {kind!r}")

    token = load_token()
    access = load_access()
    channel_id = resolve_bot2bot_channel(access)
    self_id = get_self_id(token)

    if to_target is not None:
        other_id = resolve_to_target(to_target)
        if other_id == self_id:
            sys.exit("ERROR: --to resolves to this bot itself; pick a peer")
        # SCOPE GUARD: bot2bot-post only posts to the one bot2bot channel
        # (resolved from access.json). If the recipient isn't a member of that
        # channel, refuse LOUDLY instead of silently posting where they'll never
        # see it (the 2026-07-27 dead-letter + bot-vs-human-owner id mix-up).
        # Where to route a non-bot2bot message is the caller's judgment, so the
        # guard doesn't prescribe a destination.
        if not _recipient_in_channel(access, channel_id, other_id):
            sys.exit(
                f"ERROR: recipient {other_id} is not a member of the bot2bot "
                f"channel ({channel_id}) that bot2bot-post posts to — refusing "
                "(it would be a dead letter). Send to a channel where "
                f"{other_id} is allowlisted instead (see access.json groups). "
                "Note: a bot and its human owner are different ids; verify which "
                "you mean."
            )
    else:
        other_id = resolve_other_bot(access, self_id, channel_id)

    prefix = f"<@{other_id}> " if other_id else ""
    message = f"{prefix}{kind}: {text}"

    result = post(channel_id, message, token, overhead=len(message) - len(text))
    print(f"Posted to #{channel_id} (msg_id {result.get('id')}): {message[:80]}")


if __name__ == "__main__":
    main()
