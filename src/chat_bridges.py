"""The chat-app bridges (Telegram, Discord, Slack): which are skipped on this host, set up, and running.

One reading for health-check's "configured but not running" check and for anything that shows the owner
their chat apps. A bridge is set up when its channel directory holds a `.env` or an `access.json`, and
skipped when `SKIP_<CHANNEL>=1` is in the environment or the main `.env`. Standard library only, plus
sutando_platform's process probe.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional, Tuple

# (channel, process name, display name)
BRIDGES = (("telegram", "telegram-bridge", "Telegram"), ("discord", "discord-bridge", "Discord"),
           ("slack", "slack-bridge", "Slack"))


def skipped(channel: str, env_path: Path) -> bool:
    """True if SKIP_<CHANNEL>=1 is set as an env var or in the main .env: a per-host silence."""
    var = f"SKIP_{channel.upper()}"
    if os.environ.get(var) == "1":
        return True
    if env_path.exists():
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, sep, val = line.partition("=")
                if sep and key.strip() == var and val.strip().strip('"').strip("'") == "1":
                    return True
        except Exception:
            pass
    return False


def configured(channels_dir: Path, channel: str) -> bool:
    return (Path(channels_dir) / channel / ".env").exists() or (Path(channels_dir) / channel / "access.json").exists()


def probe(process: str) -> Tuple[list, bool]:
    """(pids, whether the probe answered) for the bridge's script."""
    from sutando_platform import probe_pids
    return probe_pids(f"{process}\\.py$")


def states(channels_dir: Path, env_path: Path,
           probe_fn: Callable[[str], Tuple[list, bool]] = probe) -> list:
    """[{channel, name, running}] for every set-up, unskipped bridge; running is None when the probe failed."""
    out = []
    for channel, process, name in BRIDGES:
        if skipped(channel, env_path) or not configured(channels_dir, channel):
            continue
        pids, ok = probe_fn(process)
        running: Optional[bool] = bool(pids) if ok else None
        out.append({"channel": channel, "name": name, "running": running})
    return out
