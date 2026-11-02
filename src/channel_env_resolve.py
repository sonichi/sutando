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

When none of those holds a primary token, `channel_values` reads the desktop's ACTIVE
lane's own keys (`<NAME>_<LANE>`) from the launcher-named file, where a host signed in only
on a secondary homeserver keeps its token (and any URL); only an identity it lacks comes
from the lane's channel dir as `connections.lane_channel_dir` names it. Only
`gateway_serving.active_lane` names the lane; no other suffixed key is read, and no lane
name is decoded into a directory.

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
call it on any host without importing a stack; only the active-lane lookup and
`--room` lazily import `gateway_serving` / `connections`, which own the active
lane, its key names and a row's channel dir.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from channel_env_containment import channel_env_is_contained  # noqa: E402
from channel_token import RELAY_TOKEN_VARS, clean_relay_token, token_from_env_file  # noqa: E402

# Owned by channel_token, which also keys its relay-peel on it: two copies of
# this list would let the cleaner and the resolver disagree about what a relay is.
TOKEN_VARS = RELAY_TOKEN_VARS

DEVICE_ENV_VAR = "AG2_DEVICE_ENV"
DEVICE_ENV_SOURCE = "ag2space"


def launch_env_names(environ: Optional[Mapping[str, str]] = None) -> list[str]:
    """Names to carry across a session launcher; never open files or inspect values."""
    from connections import connection_env_names
    env = os.environ if environ is None else environ
    names = {DEVICE_ENV_VAR, "CLAUDE_CONFIG_DIR", *TOKEN_VARS,
             "REMOTE_TASK_URL", "AG2_GATEWAY_URL"}
    return sorted((names & env.keys()) | set(connection_env_names(env)))


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


def _resolve_in(channels_dir, source: str) -> Path | None:
    channel_dir = Path(channels_dir) / source
    if not channel_dir.is_dir():
        return None
    for candidate in candidates(channel_dir):
        if not channel_env_is_contained(candidate, channels_dir, source):
            continue
        if _has_token(candidate):
            return candidate
    return None


# One path segment under channels/, the charset scripts/channel-env.sh accepts.
_CHANNEL_DIR_RE = re.compile(r"^[a-z0-9._-]+$")


def resolve_channel_env(channels_dir, source: str) -> Path | None:
    """The usable launcher-named file, else the first channels-tree candidate
    that is BOTH contained and holds a non-empty token.

    None when neither the launcher-named file nor any candidate qualifies.
    """
    return _resolve_primary(channels_dir, source)


def _active_lane() -> str | None:
    try:
        from gateway_serving import active_lane
    except ImportError:
        return None
    return active_lane()


def lane_var(name: str, lane: str) -> str:
    """`name`'s key for `lane` in the device file, by the rule its launch key is named with
    (`connections.token_ref_for`): `AG2_REMOTE_TOKEN` + `s_hspace_dfoo` -> `AG2_REMOTE_TOKEN_S_HSPACE_DFOO`."""
    from connections import INSTANCE_TOKEN_PREFIX, token_ref_for
    return f"{name}_{token_ref_for(lane).removeprefix(INSTANCE_TOKEN_PREFIX)}"


def _lane_value(name: str, lane: str, path: Path) -> str:
    got = token_from_env_file(lane_var(name, lane), path)
    return clean_relay_token(got) if name in RELAY_TOKEN_VARS else got


def active_lane_env(channels_dir, source: str) -> "tuple[Path, str] | None":
    """(launcher-named file, active lane) when no file `resolve_channel_env` would pick exists
    and that file holds the active lane's own non-empty `AG2_REMOTE_TOKEN_<LANE>`."""
    if source != DEVICE_ENV_SOURCE or resolve_channel_env(channels_dir, source) is not None:
        return None
    named = (os.environ.get(DEVICE_ENV_VAR) or "").strip()
    lane = _active_lane()
    if not named or not lane or not Path(named).is_file():
        return None
    path = Path(named)
    return (path, lane) if _lane_value("AG2_REMOTE_TOKEN", lane, path) else None


def channel_values(channels_dir, source: str, names) -> dict:
    """{name: value} for each of `names` set non-empty in `resolve_channel_env`'s pick; without
    one, the active lane's values (`active_lane_values`)."""
    names = tuple(names)
    path = resolve_channel_env(channels_dir, source)
    if path is None:
        return active_lane_values(channels_dir, source, names)
    found = {n: token_from_env_file(n, path) for n in names}
    return {n: v for n, v in found.items() if v}


# Never a token or URL: those come from the device file's lane keys, so they always pair.
LANE_DIR_NAMES = ("AGENT_MXID",)


def _lane_dir_file(channels_dir, lane: str, device: Path) -> Path | None:
    import connections
    d = connections.lane_channel_dir(lane, launch_file=device)
    if d in (".", "..", DEVICE_ENV_SOURCE) or not _CHANNEL_DIR_RE.fullmatch(d):
        return None
    home = Path(channels_dir) / d
    files = candidates(home) if home.is_dir() else []
    return next((f for f in files if channel_env_is_contained(f, channels_dir, d)), None)


def active_lane_values(channels_dir, source: str, names) -> dict:
    """{name: value} for `active_lane_env`'s lane: each name's `<NAME>_<LANE>` key in the device
    file; an identity name (`LANE_DIR_NAMES`) it lacks from the lane's channel dir file, if any."""
    lane_env = active_lane_env(channels_dir, source)
    if lane_env is None:
        return {}
    device, lane = lane_env
    names = tuple(names)
    found = {n: _lane_value(n, lane, device) for n in names}
    rest = [n for n in names if not found[n] and n in LANE_DIR_NAMES]
    lane_file = _lane_dir_file(channels_dir, lane, device) if rest else None
    for n in rest if lane_file else ():
        found[n] = token_from_env_file(n, lane_file)
    return {n: v for n, v in found.items() if v}


def channel_configured(channels_dir, source: str) -> bool:
    """Whether `channel_values` can find a token for `source` here."""
    return (resolve_channel_env(channels_dir, source) is not None
            or active_lane_env(channels_dir, source) is not None)


def _resolve_primary(channels_dir, source: str) -> Path | None:
    named = device_env(source)
    return named if named is not None else _resolve_in(channels_dir, source)


LAUNCH_KEY_RE = re.compile(r"^\s*(AG2_REMOTE_TOKEN_[A-Za-z0-9_]+)=", re.M)


def _has_launch_key(path: Path) -> bool:
    try:
        names = LAUNCH_KEY_RE.findall(path.read_text())
    except (OSError, UnicodeDecodeError):
        return False
    return any(token_from_env_file(var, path) for var in names)


def resolve_launch_key_env(channels_dir, source: str) -> Path | None:
    """`resolve_channel_env`'s pick, else the first file in the same order and under the same
    containment that holds a launch key: a host signed in only on a secondary homeserver writes
    `AG2_REMOTE_TOKEN_<INST>` and no primary token."""
    picked = _resolve_primary(channels_dir, source)
    return picked if picked is not None else _resolve_launch_key_file(channels_dir, source)


def _resolve_launch_key_file(channels_dir, source: str) -> Path | None:
    named = (os.environ.get(DEVICE_ENV_VAR) or "").strip() if source == DEVICE_ENV_SOURCE else ""
    if named and Path(named).is_file() and _has_launch_key(Path(named)):
        return Path(named)
    channel_dir = Path(channels_dir) / source
    if not channel_dir.is_dir():
        return None
    for candidate in candidates(channel_dir):
        if channel_env_is_contained(candidate, channels_dir, source) and _has_launch_key(candidate):
            return candidate
    return None


def resolve_room_channel_env(channels_dir, source: str, room_id: str) -> Path | None:
    """The env file for the connection that serves `room_id` (`connections.connection_for_room`):
    a named row's own channel dir, the legacy row's AG2 Space channel, else `source` as before.
    Raises `connections.NoConnectionForRoom` when that connection has no file here."""
    import connections
    row = connections.connection_for_room(room_id)
    if row is None:
        return resolve_channel_env(channels_dir, source)
    if not row["instance"]:
        # The legacy row's own server only: the active lane may be another server's.
        return _resolve_primary(channels_dir, DEVICE_ENV_SOURCE)
    channel_dir = connections.lane_channel_dir(row["instance"])
    picked = resolve_channel_env(channels_dir, channel_dir)
    if picked is None:
        raise connections.NoConnectionForRoom(
            f"connection {row['instance']} for server {row['server']} has no env file under "
            f"{channels_dir}/{channel_dir}; room_ops.py resolves its token by room itself")
    return picked


def main(argv: list[str]) -> int:
    args = argv[1:]
    if args == ["--launch-env-names"]:
        print("\n".join(launch_env_names()))
        return 0
    room = ""
    if len(args) == 4 and args[2] == "--room":
        room, args = args[3], args[:2]
    if len(args) != 2:
        print("usage: channel_env_resolve.py <channels-dir> <source> [--room <room-id>]", file=sys.stderr)
        return 2
    channels_dir, source = args
    if room:
        import connections
        try:
            resolved = resolve_room_channel_env(channels_dir, source, room)
        except connections.ConnectionsError as e:
            print(f"channel-env: {e}", file=sys.stderr)
            return 1
    else:
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
