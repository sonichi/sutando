#!/usr/bin/env python3
"""Ingestion-level before/after for the progress-stream placeholder guard (#2157).

The fix drops a peer node's "⏳ <step> (Ns)" progress placeholders at task
INGESTION (in `_handle_discord_message`) so they don't each become a fresh task
(a self-inflicted flood). This proves the drop happens at the ingestion path,
not just in the detector:

  AFTER (placeholder channel msg): _handle_discord_message returns early, writes
                                   NO task, logs "[skip] progress-stream placeholder".
  Contrast (a real task msg):      is_progress_placeholder() is False, so the guard
                                   does NOT drop it — it proceeds past the guard.

Run: python3 tests/discord-bridge-progress-placeholder-ingest.test.py  (exit 0/1)
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import os
import sys
import json
import pathlib
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parent.parent

# The bridge needs discord.py; resolve an interpreter that has it (mirror the
# other discord-bridge tests) or skip cleanly.
try:
    import discord  # noqa: F401
except ImportError:
    for _cand in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(_cand) and os.path.realpath(_cand) != os.path.realpath(sys.executable):
            import subprocess
            if subprocess.run([_cand, "-c", "import discord"], capture_output=True).returncode == 0:
                os.execv(_cand, [_cand, os.path.abspath(__file__), *sys.argv[1:]])
    print("SKIP — discord.py not importable under any known interpreter")
    sys.exit(0)

# Isolate CLAUDE_CONFIG_DIR *and* seed the canonical access.json BEFORE the
# bridge is exec'd (qingyun P1 on #2157). The env var alone is not isolation:
# discord-bridge.py resolves channel config at MODULE level, and
# channel_access_path() falls back to the LEGACY real-home
# ~/.claude/channels/discord/access.json when the canonical path is missing —
# so an empty temp root still reads the operator's real allowlist and emits the
# deprecation warning. This is the exact rule scripts/lint-hermetic-bridge-tests.py
# enforces; my own test was violating my own gate.
_cfg_root = tempfile.mkdtemp(prefix="sutando-ppg-test-")
os.environ["CLAUDE_CONFIG_DIR"] = _cfg_root
os.environ.setdefault("DISCORD_BOT_TOKEN", "faketoken-for-tests")
_chan_dir = pathlib.Path(_cfg_root) / "channels" / "discord"
_chan_dir.mkdir(parents=True, exist_ok=True)
(_chan_dir / "access.json").write_text(json.dumps({"allowFrom": [], "groups": {}}))

spec = importlib.util.spec_from_file_location("discordbridge_ppg", REPO / "src" / "discord-bridge.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "  FAIL ") + name + ((" — " + detail) if detail and not cond else ""))
    if not cond:
        failures.append(name)


class FakeChannel:
    """A regular (non-DM) guild channel — isinstance(_, DMChannel) is False, so
    the handler takes the `not is_dm` branch where the placeholder guard lives."""
    def __init__(self, cid=555000):
        self.id = cid
        self.name = "review-preflight"
        self.sent: list[str] = []

    async def send(self, text):
        self.sent.append(text)


class FakeAuthor:
    def __init__(self, uid=999001, bot=True):
        self.id = uid
        self.bot = bot  # True = peer node posting placeholders; False = human

    def __str__(self):
        return "peer-node#0001" if self.bot else "chi#1234"


class FakeMsg:
    def __init__(self, content, channel, bot=True):
        self.content = content
        self.channel = channel
        self.author = FakeAuthor(bot=bot)
        self.mentions: list = []
        self.role_mentions: list = []
        self.embeds: list = []
        self.attachments: list = []
        self.message_snapshots: list = []
        self.type = discord.MessageType.default
        self.reference = None
        self.id = 777001


def _drive(content, tasks_dir, bot=True):
    """Run _handle_discord_message on a channel msg with the given content;
    return (captured_stdout, task_files_written)."""
    fake_client = type("_C", (), {"user": object()})()
    buf = io.StringIO()
    with patch.object(mod, "client", fake_client), \
         patch.object(mod, "_observe_for_mod", AsyncMock()), \
         patch.object(mod, "TASKS_DIR", tasks_dir), \
         patch.object(mod, "load_channel_config", lambda cid: (False, set())), \
         contextlib.redirect_stdout(buf):
        try:
            asyncio.run(mod._handle_discord_message(FakeMsg(content, FakeChannel(), bot=bot)))
        except Exception:
            # Past the guard the real ingest path may hit unmet deps; we only
            # assert guard behavior (drop vs not-dropped-by-this-guard) here.
            pass
    written = [p.name for p in tasks_dir.glob("*.txt")]
    return buf.getvalue(), written


PLACEHOLDER = "⏳ Reviewing 42 open PRs (137s)"
REAL_TASK = "please review PR #2157 and confirm the placeholder guard is safe"

# AFTER (placeholder): dropped at ingestion — no task written, skip logged.
with tempfile.TemporaryDirectory() as td:
    tasks = Path(td) / "tasks"
    tasks.mkdir()
    out, written = _drive(PLACEHOLDER, tasks)
    print(f"  AFTER  (placeholder): tasks_written={written}  skip_logged={'[skip] progress-stream placeholder' in out}")
    check("placeholder is DROPPED at ingestion — no task file written", written == [],
          f"unexpected tasks: {written}")
    check("placeholder drop is logged ('[skip] progress-stream placeholder')",
          "[skip] progress-stream placeholder" in out)

# Contrast (a real task msg): this guard does NOT drop it.
with tempfile.TemporaryDirectory() as td:
    tasks = Path(td) / "tasks"
    tasks.mkdir()
    out, _ = _drive(REAL_TASK, tasks)
    print(f"  BEFORE (real task):  placeholder_guard_fired={'[skip] progress-stream placeholder' in out}")
    check("a real task is NOT dropped by the placeholder guard",
          "[skip] progress-stream placeholder" not in out)
    check("detector agrees: a real task is not a placeholder",
          mod.progress_stream.is_progress_placeholder(REAL_TASK) is False)

# --- HUMAN control (qingyun P1 on #2157) ------------------------------------
# The exact placeholder SHAPE from a HUMAN author must survive. Before the fix
# the guard keyed on text alone, so an owner/team message reading
# "⏳ deploy the release (9s)" was silently discarded before task creation —
# a valid human task lost, with only a skip log to show for it.
HUMAN_LOOKALIKE = "⏳ deploy the release (9s)"
with tempfile.TemporaryDirectory() as td:
    tasks = Path(td) / "tasks"
    tasks.mkdir()
    out, _ = _drive(HUMAN_LOOKALIKE, tasks, bot=False)
    check("HUMAN message matching the placeholder shape is NOT dropped",
          "[skip] progress-stream placeholder" not in out,
          "guard must key on author.bot, not on text shape alone")
    # The detector still recognises the shape — proving the fix is the SCOPING,
    # not a weakened detector (which would reopen the flood this PR closes).
    check("detector still classifies that exact text as a placeholder",
          mod.progress_stream.is_progress_placeholder(HUMAN_LOOKALIKE) is True,
          "if this flips, the fix loosened detection instead of scoping it")

# And the same text from a BOT is still dropped — the flood fix is intact.
with tempfile.TemporaryDirectory() as td:
    tasks = Path(td) / "tasks"
    tasks.mkdir()
    out, written = _drive(HUMAN_LOOKALIKE, tasks, bot=True)
    check("same text from a BOT is still dropped (flood fix intact)",
          "[skip] progress-stream placeholder" in out and not written)

print()
if failures:
    print(f"FAIL — {len(failures)}: {failures}")
    sys.exit(1)
print("PASS — progress-placeholder ingestion drop")
