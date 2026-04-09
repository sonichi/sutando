#!/usr/bin/env python3
"""
Discord bridge for Sutando — listens for DMs, writes to tasks/, sends replies from results/.
Same file-based architecture as the Telegram and voice bridges.

Usage: python3 src/discord-bridge.py
"""

import asyncio
import json
import os
import time
from pathlib import Path

import discord

REPO = Path(__file__).resolve().parent.parent

# Load token from channels config
TOKEN = ""
channels_env = Path.home() / ".claude" / "channels" / "discord" / ".env"
if channels_env.exists():
    for line in channels_env.read_text().splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            TOKEN = line.split("=", 1)[1].strip()

if not TOKEN:
    print("DISCORD_BOT_TOKEN not set in ~/.claude/channels/discord/.env")
    exit(1)

TASKS_DIR = REPO / "tasks"
RESULTS_DIR = REPO / "results"
INBOX_DIR = Path("/tmp/discord-inbox")
TASKS_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)
INBOX_DIR.mkdir(exist_ok=True)

# Dedup: skip duplicate messages (Discord gateway can replay events on reconnect)
seen_message_ids = set()  # Discord message IDs already processed


# Load access config
ACCESS_FILE = Path.home() / ".claude" / "channels" / "discord" / "access.json"
def load_allowed():
    try:
        data = json.loads(ACCESS_FILE.read_text())
        return set(data.get("allowFrom", []))
    except:
        return set()  # empty = allow all DMs during pairing

def load_policy():
    try:
        data = json.loads(ACCESS_FILE.read_text())
        return data.get("dmPolicy", "pairing")
    except:
        return "pairing"

def load_channel_config(channel_id):
    """Load channel config. Returns (requireMention, allowFrom set) or None if not configured."""
    try:
        data = json.loads(ACCESS_FILE.read_text())
        groups = data.get("groups", {})
        if channel_id in groups:
            cfg = groups[channel_id]
            if cfg is True:
                return (False, None)  # no mention required, all allowed
            return (cfg.get("requireMention", True), set(cfg.get("allowFrom", [])))
        return None  # not configured
    except:
        return None

def load_channel_allowed(channel_id):
    """Load channel-specific allowlist. Returns None if channel not configured (open to all)."""
    cfg = load_channel_config(channel_id)
    if cfg is None:
        return None
    return cfg[1]

# Track pending replies: task_id -> channel
pending_replies = {}

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Discord bridge ready: {client.user}")
    # Start polling loops
    client.loop.create_task(poll_results())
    client.loop.create_task(poll_approved())
    client.loop.create_task(poll_proactive())


def _message_mentions_bot(message):
    """True if this message explicitly addresses this bot via @user or
    a role mention the bot holds. Used by both on_message and on_message_edit."""
    if client.user in message.mentions:
        return True
    if message.role_mentions and message.guild:
        if any(role.name.lower() in ("sutando", "sutando bot") for role in message.role_mentions):
            return True
        bot_member = message.guild.get_member(client.user.id)
        if bot_member:
            bot_role_ids = {r.id for r in bot_member.roles}
            if any(r.id in bot_role_ids for r in message.role_mentions):
                return True
    return False


@client.event
async def on_message(message):
    await _handle_discord_message(message)


@client.event
async def on_message_edit(before, after):
    """Handle edited messages that add a mention the bot didn't have before.
    Scenario: user sends a message, then edits to add @Sutando mention later.
    Without this handler, Discord fires on_message once on CREATE and the edit
    is invisible to the bridge."""
    if after.author == client.user:
        return
    if after.author.bot and client.user not in after.mentions:
        return
    # Only reprocess if the edit introduced a mention that wasn't there before
    if _message_mentions_bot(after) and not _message_mentions_bot(before):
        print(f"  [edit] mention added to msg {after.id} — reprocessing", flush=True)
        await _handle_discord_message(after, force=True)


async def _handle_discord_message(message, force=False):
    if message.author == client.user:
        return
    # Skip messages from other bots (e.g. another Sutando node) to avoid
    # double-processing in shared channels like the inter-machine bridge.
    # EXCEPTION: if the bot @mentions this bot specifically, treat it as a
    # legitimate cross-machine task (e.g. MacBook bot asking Mini bot to do X).
    if message.author.bot and client.user not in message.mentions:
        return

    sender_id = str(message.author.id)
    username = str(message.author)
    text = message.content or ""
    is_dm = isinstance(message.channel, discord.DMChannel)
    channel_name = getattr(message.channel, 'name', 'DM')

    print(f"  [msg] #{channel_name} @{username}: {text[:80]} (mentions: {[str(m) for m in message.mentions]}, is_dm: {is_dm}, embeds: {len(message.embeds)}, type: {message.type}, ref: {message.reference is not None})", flush=True)
    # Debug: log message snapshots for forwarded messages
    if hasattr(message, 'message_snapshots') and message.message_snapshots:
        print(f"  [debug] message_snapshots: {message.message_snapshots}", flush=True)
    if message.type != discord.MessageType.default and message.type != discord.MessageType.reply:
        print(f"  [debug] non-default message type: {message.type}", flush=True)

    # In channels, check if mention is required
    if not is_dm:
        channel_cfg = load_channel_config(str(message.channel.id))
        require_mention = True  # default
        if channel_cfg is not None:
            require_mention = channel_cfg[0]

        bot_mentioned = client.user in message.mentions
        role_mentioned = any(role.name.lower() in ("sutando", "sutando bot") or str(client.user.id) in str(role.id) for role in message.role_mentions)
        # Also check if any role mention exists and the bot has that role
        if not role_mentioned and message.role_mentions and message.guild:
            bot_member = message.guild.get_member(client.user.id)
            if bot_member:
                bot_role_ids = {r.id for r in bot_member.roles}
                role_mentioned = any(r.id in bot_role_ids for r in message.role_mentions)

        if require_mention and not bot_mentioned and not role_mentioned:
            print(f"  [skip] not mentioned (requireMention=true)", flush=True)
            return

        # In shared channels (require_mention=False), if there ARE other bot
        # @mentions but THIS bot isn't mentioned, skip — let the addressed bot handle it
        if not require_mention and message.mentions and not bot_mentioned:
            other_bot_mentions = [m for m in message.mentions if m.bot]
            if other_bot_mentions:
                print(f"  [skip] message addressed to other bot(s): {[str(m) for m in other_bot_mentions]}", flush=True)
                return

        # Strip mentions from the text
        text = text.replace(f"<@{client.user.id}>", "")
        for role in message.role_mentions:
            text = text.replace(f"<@&{role.id}>", "")
        text = text.strip()

    # Access control — applies to both DMs and channel mentions
    policy = load_policy()
    allowed = load_allowed()
    channel_allowed = load_channel_allowed(str(message.channel.id)) if not is_dm else None

    if policy == "disabled":
        return

    if is_dm:
        if policy == "allowlist" and sender_id not in allowed:
            return
    else:
        # Channel access control
        channel_cfg = load_channel_config(str(message.channel.id))
        if channel_cfg is not None:
            _, ch_allowed = channel_cfg
            if ch_allowed is None:
                pass  # channel set to true — open to all, skip access check
            elif len(ch_allowed) > 0 and sender_id not in ch_allowed:
                print(f"  [skip] @{username} not in channel allowlist", flush=True)
                return
            # empty allowFrom with requireMention = anyone who mentions can use
        else:
            # Channel not configured — fall back to global allowlist
            if allowed and sender_id not in allowed:
                print(f"  [skip] @{username} not in global allowlist", flush=True)
                return

    if policy == "pairing" and sender_id not in allowed:
        # Generate pairing code — user must approve via /discord:access pair <code>
        import random, string
        try:
            access = json.loads(ACCESS_FILE.read_text())
        except:
            access = {"dmPolicy": "pairing", "allowFrom": [], "pending": {}}
        code = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
        pending = access.get("pending", {})
        # Clean expired codes
        now_ms = int(time.time() * 1000)
        pending = {k: v for k, v in pending.items() if v.get("expiresAt", 0) > now_ms}
        pending[code] = {
            "senderId": sender_id,
            "chatId": str(message.channel.id),
            "createdAt": now_ms,
            "expiresAt": now_ms + 3600000,  # 1 hour
        }
        access["pending"] = pending
        ACCESS_FILE.write_text(json.dumps(access, indent=2))
        await message.channel.send(f"Pairing required. Ask the owner to run:\n`/discord:access pair {code}`")
        print(f"  Pairing requested: @{username} ({sender_id}) code={code}")
        return

    # Handle forwarded messages (message_snapshots) — Discord's forwarding feature
    if hasattr(message, 'message_snapshots') and message.message_snapshots:
        for snapshot in message.message_snapshots:
            snap_msg = snapshot.message if hasattr(snapshot, 'message') else snapshot
            parts = []
            # Extract text content
            snap_content = getattr(snap_msg, 'content', '') or ''
            if snap_content:
                parts.append(snap_content)
            # Extract snapshot embeds
            for embed in getattr(snap_msg, 'embeds', []):
                if embed.title: parts.append(embed.title)
                if embed.description: parts.append(embed.description)
            # Extract snapshot attachment names
            for att in getattr(snap_msg, 'attachments', []):
                parts.append(f"[Attachment: {att.filename}]")
            if parts:
                fwd_text = "\n".join(parts)
                text = (text + "\n" + fwd_text).strip() if text else fwd_text.strip()
                print(f"  [forward] extracted: {text[:100]}", flush=True)

    # Handle embeds (link previews, rich content)
    embed_text = ""
    for embed in message.embeds:
        parts = []
        if embed.author and embed.author.name:
            parts.append(f"[From {embed.author.name}]")
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            parts.append(f"{field.name}: {field.value}")
        if parts:
            embed_text += "\n".join(parts) + "\n"
    if embed_text:
        text = (text + "\n" + embed_text).strip() if text else embed_text.strip()

    # Handle attachments
    attachment_note = ""
    for att in message.attachments:
        local_path = INBOX_DIR / f"{int(time.time()*1000)}_{att.filename}"
        try:
            await att.save(local_path)
            attachment_note += f"\n[File attached: {local_path}]"
        except Exception as e:
            print(f"  Download failed: {e}")

    if not text and not attachment_note:
        # Bare mention — user deliberately pinged the bot with no content.
        # Don't drop: fetch the last few messages of channel history so the
        # core agent can understand the implicit question (owner's model:
        # "I asked a question, forgot to ping, then pinged as a follow-up").
        # Without this, editing a message to add a mention OR sending a
        # follow-up bare-ping gets silently filtered.
        if is_dm or _message_mentions_bot(message):
            context_lines = []
            try:
                async for prev in message.channel.history(limit=5, before=message):
                    prev_author = str(prev.author)
                    prev_content = (prev.content or "").strip()
                    # Strip mentions so they don't pollute the context snippet
                    for u in prev.mentions:
                        prev_content = prev_content.replace(f"<@{u.id}>", f"@{u.name}")
                    for r in prev.role_mentions:
                        prev_content = prev_content.replace(f"<@&{r.id}>", f"@&{r.name}")
                    if not prev_content and not prev.attachments:
                        continue
                    # Truncate each message and collapse newlines
                    snippet = prev_content[:200].replace("\n", " ")
                    if prev.attachments:
                        snippet += f" [+{len(prev.attachments)} attachment(s)]"
                    context_lines.append(f"  {prev_author}: {snippet}")
            except Exception as e:
                print(f"  [bare-mention] history fetch failed: {e}", flush=True)
            if context_lines:
                # Oldest-first for natural reading
                context_block = "\n".join(reversed(context_lines))
                text = (
                    "(empty mention — treat as ping. Recent channel history "
                    "below; look for an implicit question or task the owner "
                    f"was waiting on a response to.)\n\nRecent messages:\n{context_block}"
                )
            else:
                text = "(empty mention — treat as ping/status request)"
        else:
            return

    print(f"  @{username}: {text}{attachment_note}")

    # Determine access tier
    access_tier = "other"
    if sender_id in allowed:
        access_tier = "owner"
    else:
        # Check if team member (from channel allowlists)
        try:
            data = json.loads(ACCESS_FILE.read_text())
            team_ids = set()
            for ch_cfg in data.get("groups", {}).values():
                if isinstance(ch_cfg, dict):
                    team_ids.update(ch_cfg.get("allowFrom", []))
            if sender_id in team_ids:
                access_tier = "team"
        except:
            pass

    # Dedup: skip if we've already processed this Discord message ID.
    # EXCEPTION: force=True means on_message_edit is reprocessing because the
    # edit added a new mention — re-queue even though the ID is seen.
    if message.id in seen_message_ids and not force:
        print(f"  [dedup] skipping already-processed message {message.id} from @{username}")
        return
    seen_message_ids.add(message.id)
    # Cap set size to prevent unbounded growth
    if len(seen_message_ids) > 10000:
        seen_message_ids.clear()

    # Write as task
    ts = int(time.time() * 1000)
    task_id = f"task-{ts}"
    task_file = TASKS_DIR / f"{task_id}.txt"
    task_file.write_text(
        f"id: {task_id}\n"
        f"timestamp: {time.strftime('%Y-%m-%dT%H:%M:%S')}Z\n"
        f"task: [Discord @{username}] {text}{attachment_note}\n"
        f"source: discord\n"
        f"channel_id: {message.channel.id}\n"
        f"user_id: {message.author.id}\n"
        f"access_tier: {access_tier}\n"
    )
    pending_replies[task_id] = message.channel
    save_pending_replies()

    # Typing indicator
    async with message.channel.typing():
        await asyncio.sleep(0.5)


def save_to_allowlist(sender_id):
    """Add sender to access.json allowFrom."""
    try:
        data = json.loads(ACCESS_FILE.read_text())
    except:
        data = {"dmPolicy": "pairing", "allowFrom": [], "groups": {}, "pending": {}}

    if sender_id not in data.get("allowFrom", []):
        data.setdefault("allowFrom", []).append(sender_id)
        ACCESS_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACCESS_FILE.write_text(json.dumps(data, indent=2))


async def poll_approved():
    """Poll approved/ dir and send 'you're in' confirmations."""
    approved_dir = ACCESS_FILE.parent / "approved"
    while True:
        try:
            if approved_dir.exists():
                for f in approved_dir.iterdir():
                    sender_id = f.name
                    chat_id = f.read_text().strip()
                    try:
                        channel = await client.fetch_channel(int(chat_id))
                        await channel.send(f"You're in! Access approved.")
                        print(f"  Sent approval confirmation to {sender_id} in {chat_id}")
                    except Exception as e:
                        print(f"  Failed to send approval to {sender_id}: {e}")
                    f.unlink(missing_ok=True)
        except Exception as e:
            print(f"  Approved poll error: {e}")
        await asyncio.sleep(3)


PENDING_REPLIES_FILE = REPO / "src" / ".discord-pending-replies.json"

def save_pending_replies():
    """Persist pending_replies channel IDs to disk for crash recovery."""
    try:
        data = {k: str(v.id) for k, v in pending_replies.items()}
        PENDING_REPLIES_FILE.write_text(json.dumps(data))
    except Exception:
        pass

def load_pending_replies_from_disk():
    """Load pending_replies from disk on startup (channel IDs only — resolved lazily)."""
    try:
        if PENDING_REPLIES_FILE.exists():
            return json.loads(PENDING_REPLIES_FILE.read_text())
    except Exception:
        pass
    return {}

# Recovered replies: task_id → channel_id (str) — not yet resolved to channel objects
_recovered_replies = load_pending_replies_from_disk()

async def poll_results():
    """Poll results/ for replies to send back to Discord."""
    global _recovered_replies
    heartbeat_file = REPO / "src" / "discord-bridge.heartbeat"
    last_heartbeat = 0
    while True:
        # Write heartbeat at most once per 60 seconds
        now = time.time()
        if now - last_heartbeat >= 60:
            try:
                heartbeat_file.write_text(str(int(now)))
                last_heartbeat = now
            except Exception:
                pass

        # Merge recovered replies into pending_replies (resolve channel objects)
        for task_id, channel_id_str in list(_recovered_replies.items()):
            if task_id not in pending_replies:
                try:
                    channel = await client.fetch_channel(int(channel_id_str))
                    pending_replies[task_id] = channel
                except Exception as e:
                    print(f"  [recovery] failed to resolve channel {channel_id_str}: {e}")
            del _recovered_replies[task_id]

        for task_id in list(pending_replies.keys()):
            result_file = RESULTS_DIR / f"{task_id}.txt"
            if result_file.exists():
                import re
                reply_text = result_file.read_text().strip()
                channel = pending_replies.pop(task_id)
                save_pending_replies()
                # Skip sending if already replied directly (core agent used MCP)
                if reply_text.startswith('[no-send]') or reply_text.startswith('[REPLIED]'):
                    print(f"  Skipped (already replied): {task_id}")
                    continue
                try:
                    # Extract file paths: [file: /path] or [send: /path]
                    file_pattern = re.compile(r'\[(?:file|send|attach):\s*([^\]]+)\]')
                    files = file_pattern.findall(reply_text)
                    clean_text = file_pattern.sub('', reply_text).strip()

                    # Send text
                    if clean_text:
                        for i in range(0, len(clean_text), 1900):
                            await channel.send(clean_text[i:i+1900])

                    # Send files
                    for fpath in files:
                        fpath = fpath.strip()
                        if os.path.isfile(fpath):
                            await channel.send(file=discord.File(fpath))
                            print(f"  Sent file: {fpath}")
                        else:
                            await channel.send(f"(file not found: {fpath})")

                    print(f"  Replied: {reply_text[:80]}...")
                except Exception as e:
                    print(f"  Reply failed: {e}")
                # Clean up
                result_file.unlink(missing_ok=True)
                task_file = TASKS_DIR / f"{task_id}.txt"
                task_file.unlink(missing_ok=True)
        await asyncio.sleep(1)


async def poll_proactive():
    """Poll results/ for proactive messages and send to owner's DM."""
    import re
    while True:
        try:
            for f in RESULTS_DIR.iterdir():
                if f.name.startswith("proactive-") and f.suffix == ".txt":
                    text = f.read_text().strip()
                    if not text:
                        f.unlink(missing_ok=True)
                        continue
                    # Send to first owner in allowFrom
                    allowed = load_allowed()
                    if not allowed:
                        print(f"  [proactive] no owner in allowFrom, skipping {f.name}")
                        f.unlink(missing_ok=True)
                        continue
                    owner_id = next(iter(allowed))
                    try:
                        user = await client.fetch_user(int(owner_id))
                        dm = await user.create_dm()
                        # Extract files
                        file_pattern = re.compile(r'\[(?:file|send|attach):\s*([^\]]+)\]')
                        files = file_pattern.findall(text)
                        clean_text = file_pattern.sub('', text).strip()
                        if clean_text:
                            for i in range(0, len(clean_text), 1900):
                                await dm.send(clean_text[i:i+1900])
                        for fpath in files:
                            fpath = fpath.strip()
                            if os.path.isfile(fpath):
                                await dm.send(file=discord.File(fpath))
                        print(f"  [proactive] sent to {owner_id}: {clean_text[:80]}")
                    except Exception as e:
                        print(f"  [proactive] failed to DM {owner_id}: {e}")
                    f.unlink(missing_ok=True)
        except Exception as e:
            print(f"  [proactive] poll error: {e}")
        await asyncio.sleep(3)


if __name__ == "__main__":
    client.run(TOKEN, log_handler=None)
