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

Use `temp_config_root()` around the exec-load. It gives the fixture its OWN root,
so nothing is ever written to the caller's config dir.

⚠ Do NOT call `seed_discord_token()` against the ambient root. Seeding "the root the
bridge reads" fixes WHICH root is used but not WHOSE: with a real `CLAUDE_CONFIG_DIR`
set, the fixture then fabricates a Discord install in the caller's actual config dir
and leaves a stub credential behind — the very production symptom this PR reports,
relocated rather than removed (john-the-dev, #2357 review 2026-07-31T07:36).
"""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
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


@contextlib.contextmanager
def temp_config_root():
    """Point CLAUDE_CONFIG_DIR at a throwaway root, seed it, then PUT THE CALLER'S BACK.

    Isolation, not just routing. `seed_discord_token()` alone writes under whatever
    root is ambient, so a fixture run with a real `CLAUDE_CONFIG_DIR` leaves a stub
    `channels/discord/.env` in the caller's config dir. health-check then reports a
    Discord install that does not exist, with a fake token in it. Same host-leakage
    class as #2204.

    Two details are load-bearing:

    * **Restore ABSENCE, not the empty string.** If the caller had no
      CLAUDE_CONFIG_DIR, `os.environ["CLAUDE_CONFIG_DIR"] = ""` is not the same
      state — `config_root()` treats "" as unset only because it calls `.strip()`,
      and other readers may not. Pop the key instead.
    * **try/finally, not a pair of assignments.** A failing assertion between the
      two would otherwise leak both the env var and the temp dir.
    """
    prior = os.environ.get("CLAUDE_CONFIG_DIR")
    tmp = tempfile.mkdtemp(prefix="dbenv-config-")
    os.environ["CLAUDE_CONFIG_DIR"] = tmp
    try:
        seed_discord_token(Path(tmp))
        yield Path(tmp)
    finally:
        if prior is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = prior
        shutil.rmtree(tmp, ignore_errors=True)
