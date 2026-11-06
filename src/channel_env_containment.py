"""Shared containment policy for a channel's `.env` credential file.

Two independent surfaces need the exact same answer to "is this
`channels/<source>/.env` somewhere we trust?":

  * `skills/task-progress/scripts/notify.py` (`send_remote_gateway`) — the
    sender: refuses to READ a `.env` that resolves outside the trusted roots.
  * `src/core-supervisor-relay.py` (`_is_deliverable`) — the probe: decides
    whether a channel is routable BEFORE the sender ever runs, so it must
    agree with the sender or a source gets selected that then fails to send
    (#2701 review P1).

That agreement used to be maintained by hand — byte-identical logic pasted in
both files behind a "widen BOTH together or never" comment. This module is the
single owner instead: both callers delegate here, so there is exactly one
place to widen and no comment can go stale.

`channel_env_is_contained(env_path, channels_dir, source)` accepts a
resolution in exactly two cases, and fails closed otherwise:

  1. it lands inside `realpath(channels_dir)` — the channels tree itself. A
     symlinked `channels/` dir is fine; an entry that links OUT of it is not.
  2. it IS `realpath($SUTANDO_APP_SUPPORT/channels/<source>/.env)`. The AG2
     Space desktop app keeps the durable channel env under its own
     app-support root and lays `$CLAUDE_CONFIG_DIR/channels/<source>/.env` as
     a symlink to it (launch-sutando.sh; #3150/#3201). `SUTANDO_APP_SUPPORT`
     is exported by the app for every process it spawns, so this is a second,
     explicitly configured root — a containment test, not a shape match. A
     planted link to `/tmp/<source>/.env` or `~/x/<source>/.env` still fails
     closed, as does a link to another channel's file under the app root, and
     everything fails closed when the variable is unset.

Dependency-light by design (stdlib `os` only) so both a skill script and core
can import it without pulling in either stack.
"""
from __future__ import annotations

import os


def channel_env_is_contained(env_path, channels_dir, source: str) -> bool:
    """True iff `env_path` resolves inside `channels_dir` or the app-support
    relocation for `source` (see module docstring for the exact two cases)."""
    real_env = os.path.realpath(env_path)
    if real_env.startswith(os.path.realpath(channels_dir) + os.sep):
        return True
    app_support = (os.environ.get("SUTANDO_APP_SUPPORT") or "").strip()
    if not app_support:
        return False
    relocated = os.path.join(app_support, "channels", source, ".env")
    return real_env == os.path.realpath(relocated)
