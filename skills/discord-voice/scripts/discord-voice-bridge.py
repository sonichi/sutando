#!/usr/bin/env python3
"""
Discord Voice Bridge — Option B (Python bridge + TS bodhi sidecar).

Joins a Discord voice channel, captures PCM via discord-ext-voice-recv,
downsamples to 16 kHz mono, base64-encodes, and forwards over WebSocket to
`discord-voice-server.ts`. Receives Gemini PCM (24 kHz mono) back over the
same WS and plays it into the voice channel via discord.py's PCMAudio.

Companion to `discord-voice-server.ts` — see that file's header for the
WS protocol and bodhi reuse story.

Usage:
    python3 skills/discord-voice/scripts/discord-voice-bridge.py \
        --guild <GUILD_ID> \
        --channel <VOICE_CHANNEL_ID> \
        [--sidecar ws://localhost:3200/voice]

Env:
    DISCORD_BOT_TOKEN  — required (same token discord-bridge.py uses)

`join.py` (silent-presence-only) is intentionally NOT touched — this bridge
is a separate entry point so users can choose silent vs full Gemini mode.
"""

from __future__ import annotations

import argparse
import asyncio
import audioop
import base64
import json
import os
import signal
import sys
from pathlib import Path
from typing import Optional

# Self-rescue under whichever python3 has the deps installed (same trick as
# src/discord-bridge.py — see that file's commentary). We need discord +
# discord-ext-voice-recv + websockets all in the same interpreter.
try:
    import discord  # noqa: F401
    from discord.ext import voice_recv  # noqa: F401
    import websockets  # noqa: F401
except ModuleNotFoundError:
    _RESCUE_CANDIDATES = [
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/opt/homebrew/opt/python@3.13/bin/python3",
        "/opt/homebrew/opt/python@3.14/bin/python3",
    ]
    _current = os.path.realpath(sys.executable)
    for _cand in _RESCUE_CANDIDATES:
        if not os.path.exists(_cand) or os.path.realpath(_cand) == _current:
            continue
        import subprocess

        _check = subprocess.run(
            [_cand, "-c", "import discord; from discord.ext import voice_recv; import websockets"],
            capture_output=True,
        )
        if _check.returncode == 0:
            print(
                f"discord-voice-bridge: launched with {_current}; re-execing under {_cand}",
                file=sys.stderr,
                flush=True,
            )
            os.execv(_cand, [_cand, __file__, *sys.argv[1:]])
    raise

import discord  # noqa: E402  (after rescue)
from discord.ext import voice_recv  # noqa: E402
import websockets  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
try:
    from workspace_default import resolve_workspace  # type: ignore
except ModuleNotFoundError:
    # Fallback for ad-hoc / test invocations where the sutando src dir isn't on path.
    def resolve_workspace() -> Path:  # type: ignore
        env = os.environ.get("SUTANDO_WORKSPACE", "").strip()
        return Path(env).expanduser() if env else Path.home() / ".sutando" / "workspace"

WORKSPACE = resolve_workspace()

# Discord voice constants (libopus-decoded PCM is always s16le 48 kHz stereo).
DISCORD_SAMPLE_RATE = 48_000
DISCORD_CHANNELS = 2
DISCORD_SAMPLE_WIDTH = 2  # s16

# What we send to the TS sidecar (matches VoiceSession.handleAudioFromClient
# expectation in conversation-server.ts: PCM 16k mono s16le).
TS_INBOUND_RATE = 16_000

# What we receive from the TS sidecar (Gemini PCM 24k mono s16le, per
# conversation-server.ts:213 pcm24kToMulaw8k input).
TS_OUTBOUND_RATE = 24_000

# Min PCM chunk to send (avoid 20ms-packet flood). Discord packets are 20ms
# (3840 bytes after stereo s16 at 48k). Batch ~60ms before sending.
BATCH_MS = 60
BATCH_BYTES_48K = int(DISCORD_SAMPLE_RATE * (BATCH_MS / 1000.0)) * DISCORD_CHANNELS * DISCORD_SAMPLE_WIDTH


def log(msg: str) -> None:
    print(f"[discord-voice-bridge] {msg}", file=sys.stderr, flush=True)


# --- WebSocket client wrapper ----------------------------------------------


class SidecarLink:
    """Persistent WebSocket connection to the TS bodhi sidecar.

    Sends inbound audio chunks (PCM 16k mono base64) and receives outbound
    Gemini audio (PCM 24k mono base64) + transcript events. Auto-reconnects
    if the connection drops while the voice channel is still active.
    """

    def __init__(self, url: str, on_audio, on_transcript, guild: Optional[str], channel: Optional[str]):
        self.url = url
        self.on_audio = on_audio          # callable(bytes_pcm_24k_mono)
        self.on_transcript = on_transcript  # callable(role, text)
        self.guild = guild
        self.channel = channel
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._send_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self.ws is not None:
            try:
                await self.ws.send(json.dumps({"type": "bye"}))
            except Exception:
                pass
            try:
                await self.ws.close()
            except Exception:
                pass
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def send_pcm16k(self, pcm: bytes) -> None:
        # Drop on backpressure rather than block the audio thread.
        try:
            self._send_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            log("send queue full — dropping audio chunk")

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                log(f"connecting to sidecar {self.url}")
                async with websockets.connect(self.url, max_size=4 * 1024 * 1024) as ws:
                    self.ws = ws
                    backoff = 1.0
                    await ws.send(json.dumps({
                        "type": "hello",
                        "guild": self.guild,
                        "channel": self.channel,
                    }))
                    sender = asyncio.create_task(self._sender_loop(ws))
                    receiver = asyncio.create_task(self._receiver_loop(ws))
                    done, pending = await asyncio.wait(
                        [sender, receiver, asyncio.create_task(self._stop.wait())],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
            except Exception as e:
                log(f"sidecar WS error: {e}")
            self.ws = None
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    async def _sender_loop(self, ws) -> None:
        while True:
            pcm = await self._send_queue.get()
            payload = base64.b64encode(pcm).decode("ascii")
            await ws.send(json.dumps({"type": "audio", "pcm": payload}))

    async def _receiver_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type")
            if t == "audio":
                pcm = base64.b64decode(msg.get("pcm", ""))
                if pcm:
                    self.on_audio(pcm)
            elif t == "transcript":
                self.on_transcript(msg.get("role", ""), msg.get("text", ""))
            elif t == "ready":
                log(f"sidecar ready: sessionId={msg.get('sessionId')}")
            # Ignore everything else (forwards-compatible).


# --- Discord-side audio plumbing -------------------------------------------


class _ResampleSink(voice_recv.AudioSink):
    """Sink that receives decoded PCM (48k stereo s16le) per voice packet,
    downmixes to mono, downsamples to 16k, batches ~60 ms, and forwards
    asynchronously to the sidecar.

    discord-ext-voice-recv calls `write()` on its own thread, so we hop
    onto the asyncio loop via `loop.call_soon_threadsafe`.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, link: SidecarLink):
        super().__init__()
        self.loop = loop
        self.link = link
        self._buffer = bytearray()
        self._ratecv_state = None

    def wants_opus(self) -> bool:
        return False  # we want decoded PCM

    def write(self, user, data: voice_recv.VoiceData) -> None:
        pcm = data.pcm
        if not pcm:
            return
        # PCM is 48k stereo s16le. Downmix to mono.
        mono_48k = audioop.tomono(pcm, DISCORD_SAMPLE_WIDTH, 0.5, 0.5)
        self._buffer.extend(mono_48k)
        # Buffer until we have ~BATCH_MS at 48k mono (half of stereo bytes).
        needed = BATCH_BYTES_48K // 2  # stereo→mono halves the byte count
        while len(self._buffer) >= needed:
            chunk = bytes(self._buffer[:needed])
            del self._buffer[:needed]
            # Downsample 48k → 16k mono.
            pcm_16k, self._ratecv_state = audioop.ratecv(
                chunk,
                DISCORD_SAMPLE_WIDTH,
                1,
                DISCORD_SAMPLE_RATE,
                TS_INBOUND_RATE,
                self._ratecv_state,
            )
            asyncio.run_coroutine_threadsafe(self.link.send_pcm16k(pcm_16k), self.loop)

    def cleanup(self) -> None:
        self._buffer.clear()
        self._ratecv_state = None


class _PlaybackSource(discord.AudioSource):
    """discord.AudioSource that streams PCM 48k stereo s16le from an in-memory
    queue. The sidecar feeds us 24k mono; we upsample + duplicate per
    `enqueue_pcm24k`.

    Discord requests 20 ms frames (3840 bytes at 48k stereo s16). When we
    have no audio queued, return silence so playback stays "active" and the
    voice connection doesn't stall.
    """

    FRAME_MS = 20
    FRAME_BYTES = int(DISCORD_SAMPLE_RATE * (FRAME_MS / 1000.0)) * DISCORD_CHANNELS * DISCORD_SAMPLE_WIDTH
    SILENCE = b"\x00" * FRAME_BYTES

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._ratecv_state = None
        self._lock = asyncio.Lock()
        # Track if we've ever pushed data — return silence-as-data if so to
        # keep the player alive across pauses between Gemini turns.
        self._opened = False

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        if len(self._buffer) >= self.FRAME_BYTES:
            frame = bytes(self._buffer[: self.FRAME_BYTES])
            del self._buffer[: self.FRAME_BYTES]
            return frame
        if self._opened:
            # Keep stream "live" between Gemini turns.
            return self.SILENCE
        return b""  # signals end-of-source pre-open

    def enqueue_pcm24k(self, pcm: bytes) -> None:
        # Upsample 24k mono → 48k mono, then stereo-duplicate.
        pcm_48k_mono, self._ratecv_state = audioop.ratecv(
            pcm,
            DISCORD_SAMPLE_WIDTH,
            1,
            TS_OUTBOUND_RATE,
            DISCORD_SAMPLE_RATE,
            self._ratecv_state,
        )
        pcm_48k_stereo = audioop.tostereo(pcm_48k_mono, DISCORD_SAMPLE_WIDTH, 1.0, 1.0)
        self._buffer.extend(pcm_48k_stereo)
        self._opened = True

    def cleanup(self) -> None:
        self._buffer.clear()
        self._ratecv_state = None


# --- Main bot orchestration -------------------------------------------------


class DiscordVoiceBridge(discord.Client):
    def __init__(self, guild_id: int, channel_id: int, sidecar_url: str):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        super().__init__(intents=intents)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.sidecar_url = sidecar_url
        self.vc: Optional[voice_recv.VoiceRecvClient] = None
        self.sink: Optional[_ResampleSink] = None
        self.source: Optional[_PlaybackSource] = None
        self.link: Optional[SidecarLink] = None
        self._shutdown = asyncio.Event()

    async def on_ready(self) -> None:
        log(f"logged in as {self.user} (id={self.user.id if self.user else '?'})")
        guild = self.get_guild(self.guild_id)
        if guild is None:
            log(f"guild {self.guild_id} not visible — bot is probably not invited")
            await self.close()
            return
        channel = guild.get_channel(self.channel_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            log(f"channel {self.channel_id} is not a voice/stage channel")
            await self.close()
            return

        # Connect with the voice_recv client so we can receive audio.
        log(f"connecting to voice channel #{channel.name} ({channel.id})")
        try:
            self.vc = await channel.connect(cls=voice_recv.VoiceRecvClient)
        except Exception as e:
            log(f"voice connect failed: {e}")
            await self.close()
            return

        loop = asyncio.get_event_loop()

        # Audio source (playback) — start it immediately so it stays "live".
        self.source = _PlaybackSource()
        # Need at least one byte before play() will accept; prime with silence.
        self.source.enqueue_pcm24k(b"\x00" * int(TS_OUTBOUND_RATE * 0.02) * 2)
        try:
            self.vc.play(self.source)
        except Exception as e:
            log(f"playback start failed: {e}")

        # Sidecar link.
        def on_audio_from_sidecar(pcm_24k: bytes) -> None:
            if self.source is not None:
                self.source.enqueue_pcm24k(pcm_24k)

        def on_transcript(role: str, text: str) -> None:
            log(f"transcript[{role}]: {text[:200]}")

        self.link = SidecarLink(
            self.sidecar_url,
            on_audio_from_sidecar,
            on_transcript,
            guild=str(self.guild_id),
            channel=str(self.channel_id),
        )
        await self.link.start()

        # Sink (capture).
        self.sink = _ResampleSink(loop, self.link)
        try:
            self.vc.listen(self.sink)
        except Exception as e:
            log(f"sink listen failed: {e}")

        log("ready — audio bridging active")

    async def shutdown(self) -> None:
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        log("shutting down")
        try:
            if self.vc is not None:
                try:
                    self.vc.stop_listening()
                except Exception:
                    pass
                try:
                    self.vc.stop()  # stop playback
                except Exception:
                    pass
                try:
                    await self.vc.disconnect(force=True)
                except Exception:
                    pass
        finally:
            if self.link is not None:
                await self.link.stop()
            await self.close()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Discord voice bridge (Option B)")
    p.add_argument("--guild", required=True, type=int, help="Discord guild (server) ID")
    p.add_argument("--channel", required=True, type=int, help="Discord voice channel ID")
    p.add_argument(
        "--sidecar",
        default=os.environ.get("DISCORD_VOICE_SIDECAR", "ws://localhost:3200/voice"),
        help="TS sidecar WS URL (default: ws://localhost:3200/voice)",
    )
    return p.parse_args()


async def amain() -> None:
    args = parse_args()
    token = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN")
    if not token:
        log("DISCORD_BOT_TOKEN env var required")
        sys.exit(2)

    bridge = DiscordVoiceBridge(args.guild, args.channel, args.sidecar)

    loop = asyncio.get_event_loop()

    def _signal_handler() -> None:
        log("signal received — shutting down")
        asyncio.create_task(bridge.shutdown())

    for sig_name in ("SIGTERM", "SIGINT"):
        try:
            loop.add_signal_handler(getattr(signal, sig_name), _signal_handler)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main-thread fallback.
            pass

    try:
        await bridge.start(token)
    finally:
        await bridge.shutdown()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
