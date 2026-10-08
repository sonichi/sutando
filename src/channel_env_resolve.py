"""Pick the channel env file a caller should source for `channels/<source>`.

Layouts differ per host: some write REMOTE_TASK_* into `channels/<src>/.env`,
others into a sibling (e.g. `relay-client.env`) while `.env` holds Matrix
creds. Resolving by CONTENT rather than filename is what makes one instruction
correct on both.

This module owns the SELECTION, including the candidate ORDER:

  1. `$AG2_DEVICE_ENV`, for the `ag2space` source only. The desktop launcher
     names this file for every process it spawns; it is the only pointer that
     reaches a desktop-spawned core. It is trusted because the launcher names
     it — it is exempt from containment, not approximated by it — but it must
     be an existing regular file holding a non-empty token, else it falls
     through. The gateway bridge's `_channel_env_candidates` keeps the same
     first entry (pinned by tests/channel-env-order-contract.test.py).
  2. The `channels/<source>` candidates, which must satisfy both rules below.

The channels dir is the caller's input; this module never guesses a home dir.
The two rules a channels-tree candidate must satisfy are each answered by
their existing owner, not re-stated here:

  * containment — `channel_env_containment.channel_env_is_contained`. The
    caller's contract is `set -a; . "$(...)"; set +a`, so a returned path is
    *executed*: a `.env` symlinked out of the channels tree would source a
    credential file the sender/probe contract deliberately refuses. Selection
    must therefore agree with the sender rather than approximate it.
  * a usable token — `channel_token.token_from_env_file`, which already owns
    "a present-but-empty value does not count" (the same defect class as
    startup.sh's prefix-only `grep -q "<VAR>="` gate). Key-presence alone
    selects a blank `.env` over a sibling holding the real token.

Dependency-light (stdlib only, plus those two siblings) so a shell wrapper can
call it on any host without importing a stack.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from channel_env_containment import channel_env_is_contained  # noqa: E402
from channel_token import RELAY_TOKEN_VARS, token_from_env_file  # noqa: E402

# Owned by channel_token, which also keys its relay-peel on it: two copies of
# this list would let the cleaner and the resolver disagree about what a relay is.
TOKEN_VARS = RELAY_TOKEN_VARS

DEVICE_ENV_VAR = "AG2_DEVICE_ENV"
DEVICE_ENV_SOURCE = "ag2space"


def _has_token(path: Path) -> bool:
    return any(token_from_env_file(var, path) for var in TOKEN_VARS)


def device_env(source: str) -> Path | None:
    """The launcher-named file when it applies to `source` and is usable."""
    if source != DEVICE_ENV_SOURCE:
        return None
    named = (os.environ.get(DEVICE_ENV_VAR) or "").strip()
    if not named:
        return None
    path = Path(named)
    if not path.is_file() or not _has_token(path):
        return None
    return path


def candidates(channel_dir: Path) -> list[Path]:
    """`.env` first so a correct existing layout keeps its precedence, then any
    sibling `*.env` sorted, so the pick is deterministic across hosts."""
    seen: list[Path] = []
    dot = channel_dir / ".env"
    if dot.is_file():
        seen.append(dot)
    for sibling in sorted(channel_dir.glob("*.env")):
        if sibling.is_file() and sibling not in seen:
            seen.append(sibling)
    return seen


def resolve_channel_env(channels_dir, source: str) -> Path | None:
    """The usable launcher-named file, else the first channels-tree candidate
    that is BOTH contained and holds a non-empty token.

    None when neither the launcher-named file nor any candidate qualifies.
    """
    named = device_env(source)
    if named is not None:
        return named
    channel_dir = Path(channels_dir) / source
    if not channel_dir.is_dir():
        return None
    for candidate in candidates(channel_dir):
        if not channel_env_is_contained(candidate, channels_dir, source):
            continue
        if _has_token(candidate):
            return candidate
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: channel_env_resolve.py <channels-dir> <source>", file=sys.stderr)
        return 2
    channels_dir, source = argv[1], argv[2]
    resolved = resolve_channel_env(channels_dir, source)
    if resolved is None and not os.path.isdir(os.path.join(channels_dir, source)):
        print(f"channel-env: no channel dir {os.path.join(channels_dir, source)}", file=sys.stderr)
        return 1
    if resolved is None:
        print(f"channel-env: no contained file under {channels_dir}/{source} defines a "
              f"non-empty {' / '.join(TOKEN_VARS)}", file=sys.stderr)
        return 1
    print(str(resolved))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
