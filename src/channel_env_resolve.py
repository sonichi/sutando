"""Pick the channel env file a caller should source for `channels/<source>`.

Layouts differ per host: some write REMOTE_TASK_* into `channels/<src>/.env`,
others into a sibling (e.g. `relay-client.env`) while `.env` holds Matrix
creds. Resolving by CONTENT rather than filename is what makes one instruction
correct on both.

This module owns only the SELECTION — the two rules a candidate must satisfy
are each answered by their existing owner, not re-stated here:

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
from channel_token import token_from_env_file  # noqa: E402

# Both spellings the gateway contract accepts (docs/remote-gateway-protocol.md);
# AG2_REMOTE_TOKEN is the legacy alias still present on older installs.
TOKEN_VARS = ("REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")


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
    """The first candidate that is BOTH contained and holds a non-empty token.

    None when the channel dir is absent or no candidate satisfies both.
    """
    channel_dir = Path(channels_dir) / source
    if not channel_dir.is_dir():
        return None
    for candidate in candidates(channel_dir):
        if not channel_env_is_contained(candidate, channels_dir, source):
            continue
        if any(token_from_env_file(var, candidate) for var in TOKEN_VARS):
            return candidate
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: channel_env_resolve.py <channels-dir> <source>", file=sys.stderr)
        return 2
    channels_dir, source = argv[1], argv[2]
    if not os.path.isdir(os.path.join(channels_dir, source)):
        print(f"channel-env: no channel dir {os.path.join(channels_dir, source)}", file=sys.stderr)
        return 1
    resolved = resolve_channel_env(channels_dir, source)
    if resolved is None:
        print(f"channel-env: no contained file under {channels_dir}/{source} defines a "
              f"non-empty {' / '.join(TOKEN_VARS)}", file=sys.stderr)
        return 1
    print(str(resolved))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
