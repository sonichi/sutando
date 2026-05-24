#!/usr/bin/env python3
"""
discord-voice join-trigger helper — the "magic word" summon.

The discord-bridge (`src/discord-bridge.py`) is core infrastructure and must
stay thin (CLAUDE.md "Architecture rules"). So the bridge only does the cheap
detection — "is this the owner + does the trimmed message match the join
phrase" — via `message_is_join_phrase()`, and then hands the whole join off
to `handle_join_trigger()` here. All the feature logic lives in this skill:

  - resolve the join phrase from the discord-voice config
  - look up which voice channel the owner is currently in
  - guard against double-launching a server already running for that channel
  - spawn `discord-voice-server.ts` as a detached background subprocess

Pure-ish + import-light so the bridge can import it without pulling in
discord.py-only machinery; the only discord.py touch is reading attributes
off the message object the bridge already has.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Repo root: skills/discord-voice/scripts/join_trigger.py → up 3 → repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_DISCORD_VOICE_SERVER = (
    _REPO_ROOT / "skills" / "discord-voice" / "scripts" / "discord-voice-server.ts"
)

# Default join phrase. NOT the source of truth — the config is. This is only
# the fallback when the workspace config is missing or has no `join_phrase`
# key. Keep in sync with skills/discord-voice/config.json.example.
DEFAULT_JOIN_PHRASE = "za warudo"


def _resolve_workspace() -> Path:
    """Resolve the Sutando workspace dir the same way the rest of the fleet
    does. Prefers the canonical helper; falls back to the documented default
    (`$SUTANDO_WORKSPACE` env, else `~/.sutando/workspace/`)."""
    try:
        sys.path.insert(0, str(_REPO_ROOT / "src"))
        from workspace_default import resolve_workspace  # type: ignore

        return resolve_workspace()
    except Exception:
        env = os.environ.get("SUTANDO_WORKSPACE", "").strip()
        if env:
            return Path(env).expanduser()
        return Path.home() / ".sutando" / "workspace"


def _config_path() -> Path:
    """Path to the per-user discord-voice config (workspace, not the repo)."""
    return _resolve_workspace() / "config" / "discord-voice.json"


def load_join_phrase() -> str:
    """Return the configured join phrase, lower-cased + stripped for matching.

    Resolution: `join_phrase` in `$SUTANDO_WORKSPACE/config/discord-voice.json`,
    else the committed `config.json.example` template's value, else
    `DEFAULT_JOIN_PHRASE`. Never hardcodes the live phrase in bridge code —
    the operator can change the magic word by editing the workspace config.
    """
    for candidate in (
        _config_path(),
        _REPO_ROOT / "skills" / "discord-voice" / "config.json.example",
    ):
        try:
            data = json.loads(candidate.read_text())
        except Exception:
            continue
        phrase = data.get("join_phrase")
        if isinstance(phrase, str) and phrase.strip():
            return phrase.strip().lower()
    return DEFAULT_JOIN_PHRASE


def message_is_join_phrase(text: str, join_phrase: str | None = None) -> bool:
    """True iff `text` is the join-phrase command.

    Match rule: case-insensitive against the *trimmed* message — either an
    exact match, OR the message starts with the phrase followed by a
    non-alphanumeric boundary (so trailing punctuation like "ZA WARUDO!" /
    "za warudo, please" / "za warudo\n…" matches, while "za warudonow"
    doesn't). A bare empty string never matches.

    The boundary check prevents a short phrase like "go" from matching
    "google ..." while still tolerating the natural exclamation marks and
    punctuation users add. Exact-match above covers the no-trailing-content
    case.

    Pure function — no I/O when `join_phrase` is supplied; tests pass it
    explicitly.
    """
    if not text:
        return False
    phrase = (join_phrase if join_phrase is not None else load_join_phrase())
    phrase = (phrase or "").strip().lower()
    if not phrase:
        return False
    trimmed = text.strip().lower()
    if trimmed == phrase:
        return True
    if trimmed.startswith(phrase):
        next_ch = trimmed[len(phrase):len(phrase) + 1]
        # Word-boundary: any non-alphanumeric, non-underscore character
        # counts. Mirrors `\W` in regex.
        return not next_ch.isalnum() and next_ch != "_"
    return False


def _owner_voice_channel(message):
    """Return the discord VoiceChannel the *message author* is currently in
    within the message's guild, or None if they aren't in one.

    Reads `message.author`'s voice state. The author must be looked up as a
    guild Member (a DM `message.author` is a bare User with no `.voice`); we
    resolve the member via the guild the bot shares with them.
    """
    author = getattr(message, "author", None)
    if author is None:
        return None

    # Fast path: the message came from a guild text channel — the author is
    # already a Member there with a `.voice` attribute.
    member = author if hasattr(author, "voice") else None
    voice = getattr(member, "voice", None) if member is not None else None
    if voice is not None and getattr(voice, "channel", None) is not None:
        return voice.channel

    # DM path: the author is a User, not a Member. Walk the guilds the bot is
    # in and find one where this user has an active voice state.
    author_id = getattr(author, "id", None)
    if author_id is None:
        return None
    # `message._state` exposes the connected client; prefer an explicit guilds
    # iterable if the caller attached one.
    guilds = getattr(message, "_join_trigger_guilds", None)
    if guilds is None:
        client = getattr(message, "_state", None)
        guilds = getattr(getattr(client, "_get_client", lambda: None)(), "guilds", None) \
            if client is not None else None
    if not guilds:
        return None
    for guild in guilds:
        m = guild.get_member(author_id)
        v = getattr(m, "voice", None) if m is not None else None
        ch = getattr(v, "channel", None) if v is not None else None
        if ch is not None:
            return ch
    return None


def _server_already_running(channel_id) -> bool:
    """True iff a discord-voice-server process is already running for this
    voice channel. Detected via `pgrep -f` matching both the server script
    name AND the channel id on the same command line, so two servers in
    different channels don't false-positive on each other."""
    try:
        proc = subprocess.run(
            ["pgrep", "-fa", "discord-voice-server.ts"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    needle = f"--channel {channel_id}"
    for line in proc.stdout.splitlines():
        if needle in line:
            return True
    return False


def _spawn_voice_server(guild_id, channel_id) -> bool:
    """Launch discord-voice-server.ts detached for guild+channel. Returns True
    on a successful spawn (the subprocess was started — not that it connected).

    Mirrors the run command from SKILL.md:
      env -u GEMINI_API_KEY DISCORD_VOICE_SERVER=1 \
        npx tsx skills/discord-voice/scripts/discord-voice-server.ts \
        --guild <GUILD_ID> --channel <VC_ID>

    `GEMINI_API_KEY` is unset for the child (the voice server uses its own
    GEMINI_VOICE_API_KEY path) — matches the documented invocation. Detached
    via start_new_session so it survives the bridge restarting; stdout/stderr
    go to the workspace log so it isn't lost.
    """
    if not _DISCORD_VOICE_SERVER.exists():
        return False
    env = dict(os.environ)
    env.pop("GEMINI_API_KEY", None)
    env["DISCORD_VOICE_SERVER"] = "1"

    log_path = _resolve_workspace() / "logs" / "discord-voice-server.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(log_path, "ab")
    except Exception:
        log_fh = subprocess.DEVNULL  # type: ignore[assignment]

    argv = [
        "npx",
        "tsx",
        str(_DISCORD_VOICE_SERVER),
        "--guild",
        str(guild_id),
        "--channel",
        str(channel_id),
    ]
    try:
        subprocess.Popen(
            argv,
            cwd=str(_REPO_ROOT),
            env=env,
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detached — survives bridge restart
        )
        return True
    except Exception:
        return False
    finally:
        # Popen dup'd the fd; close our handle so the bridge doesn't leak it.
        if log_fh not in (subprocess.DEVNULL, None):
            try:
                log_fh.close()
            except Exception:
                pass


def _enqueue_context_prep_task(phrase: str, channel_id, channel_name: str) -> None:
    """Drop a synthetic task that nudges the core to write a context result
    file targeted at the just-spawned voice session.

    Target: the per-channel pull namespace `results/<voice-channel-id>.task-
    <core-task-id>.txt` (PR #1033 / `result_channel_key.py`). The voice
    session's discord-voice-server scans that namespace once connected,
    reads-and-deletes the file, and injects its body into the live Gemini
    Live session via `transport.sendContent` — same path the work-tool
    drain uses. The injection is NOT spoken (no audio turn), it's session
    input the model can refer to when the user asks "what's going on?".

    The synthetic task body carries ONLY metadata (which phrase fired,
    which voice channel) — NO user content. The core agent decides what
    `active_drafts` / `last_results` are relevant from its OWN
    conversation state and writes them into the scoped result file; we
    don't pass any of that through the bridge here.

    `source: system-magic-word` + `priority: urgent` so the core's queue
    picks this ahead of normal tasks, AND so consumers can identify and
    filter the synthetic class if needed.

    Never raises — observability shouldn't block voice from launching.
    """
    try:
        ws = _resolve_workspace()
        tasks_dir = ws / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        ts_ms = int(time.time() * 1000)
        iso_now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        owner_id = os.environ.get("SUTANDO_DM_OWNER_ID", "").strip() or "owner"
        # Body instructs the core to write a SCOPED result file the voice
        # session will pick up via the per-channel pull namespace. No user
        # content in this body — the core fills the result file from its
        # own conversation state.
        body = (
            f"[SYSTEM] Magic word '{phrase}' fired. discord-voice-server is "
            f"spawning for voice channel id={channel_id} name={channel_name}. "
            f"Write a context result file at "
            f"results/{channel_id}.task-<this-task-id>.txt (per-channel pull "
            f"namespace) so the voice session injects it on connect. The body "
            f"should summarize the conversation state voice may need: any "
            f"active draft you're iterating on, the last few result subjects, "
            f"and that voice just joined via the '{phrase}' magic word. Keep "
            f"it 1-2 short paragraphs — voice consumes it as session input, "
            f"not as a spoken turn. No DM reply needed."
        )
        task_path = tasks_dir / f"task-{ts_ms}.txt"
        task_path.write_text(
            f"id: task-{ts_ms}\n"
            f"timestamp: {iso_now}\n"
            f"task: {body}\n"
            f"source: system-magic-word\n"
            f"channel_id: local-magic-word\n"
            f"user_id: {owner_id}\n"
            f"access_tier: owner\n"
            f"priority: urgent\n"
        )
    except Exception:
        # Synthetic task is a nice-to-have for context — never block the spawn.
        pass


def handle_join_trigger(message) -> str:
    """Owner said the magic word — summon discord-voice into their channel.

    Called by the bridge AFTER it has confirmed (a) the sender is the owner
    and (b) the trimmed message matches the join phrase. This function does
    the rest: resolve the owner's current voice channel, guard against a
    double-launch, queue a context-prep task for the core, and spawn the
    server. The context-prep task and the spawn are concurrent — neither
    waits on the other (per the agreed design: voice arrives immediately,
    context lands on the next core watcher tick).

    Returns a short human-readable reply string for the bridge to send back
    to the originating channel. Never raises — any failure becomes a reply.
    """
    phrase = load_join_phrase()
    try:
        channel = _owner_voice_channel(message)
    except Exception as e:
        return f"Couldn't check your voice status ({e}). Join a voice channel and try again."

    if channel is None:
        return (
            f"You're not in a voice channel. Join one first, then say "
            f"\"{phrase}\" and I'll hop in."
        )

    channel_id = getattr(channel, "id", None)
    guild = getattr(channel, "guild", None)
    guild_id = getattr(guild, "id", None)
    channel_name = getattr(channel, "name", str(channel_id))

    if channel_id is None or guild_id is None:
        return "Couldn't resolve that voice channel. Try again in a moment."

    if _server_already_running(channel_id):
        return f"I'm already in **{channel_name}** — see you there."

    if _spawn_voice_server(guild_id, channel_id):
        # Queue context-prep AFTER successful spawn so the core only sees
        # the synthetic task when voice is actually on the way. No await /
        # block — voice and context-prep race; voice's first turn falls
        # back to a greeting if the file isn't populated yet, subsequent
        # turns read the enriched view via the `recent_context` tool.
        _enqueue_context_prep_task(phrase, channel_id, channel_name)
        return f"On my way to **{channel_name}** — give me a few seconds to connect."
    return (
        "Couldn't launch the voice server. Check logs/discord-voice-server.log "
        "and that `npx tsx` is available."
    )


if __name__ == "__main__":
    # Tiny manual smoke test for the pure matcher.
    p = load_join_phrase()
    print(f"join_phrase = {p!r}")
    for sample in (p, p.upper(), f"{p} please", f"  {p}  ", "hello", ""):
        print(f"  {sample!r:30} -> {message_is_join_phrase(sample, p)}")
