"""Shared Discord-bridge test fixture: seed the token under the CONFIG ROOT.

`src/discord-bridge.py` reads its token at import time from
`$CLAUDE_CONFIG_DIR/channels/discord/.env`, falling back to `~/.claude` only when
that variable is unset. Several fixtures seeded `Path.home() / ".claude" / ...`
directly, which is the WRONG root whenever CLAUDE_CONFIG_DIR is set — i.e. in
every real split-config install.

Those fixtures passed anyway, for a bad reason: a sibling test
(discord-bridge-collaborator-tier) set CLAUDE_CONFIG_DIR to a temp dir *and never
restored it*, so later tests in the same standalone run inherited a config root
that happened to contain a token. Fixing that leak (#2357) removed the masking and
turned three fixtures from masked-green into deterministic failures reporting
`DISCORD_BOT_TOKEN not set in $CLAUDE_CONFIG_DIR/channels/discord/.env`.

⚠ Those failures were NOT introduced by the leak fix — they were always wrong
about which root to use, and the leak hid it. Newly exposed is not newly
introduced; the right repair is to align the fixtures with the production
contract, not to restore the pollution.

Use `seed_discord_token()` before exec-loading the bridge.
"""
from __future__ import annotations

import os
from pathlib import Path

STUB_TOKEN = "DISCORD_BOT_TOKEN=test-stub-token\n"


def config_root() -> Path:
    """The root the BRIDGE will read, resolved the same way the bridge resolves it.

    Deliberately mirrors the production precedence ($CLAUDE_CONFIG_DIR, else
    ~/.claude) rather than picking one. A fixture that hardcodes either half is
    correct only by coincidence — which is exactly the bug this module fixes.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(override) if override else Path.home() / ".claude"


def seed_discord_token(root: Path | None = None) -> Path:
    """Ensure a stub DISCORD_BOT_TOKEN exists under the config root. Returns the file.

    Never overwrites an existing .env — a developer's real token must survive a
    test run. Idempotent, so several fixtures in one process are safe.
    """
    env_dir = (root or config_root()) / "channels" / "discord"
    env = env_dir / ".env"
    if not env.exists():
        env_dir.mkdir(parents=True, exist_ok=True)
        env.write_text(STUB_TOKEN)
    return env
