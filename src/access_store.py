"""Single writer contract for Discord access.json.

Every writer of a channel's access.json — tier-map seeding, thread-engage
seeding, pairing-code issuance, and the `/discord:access` skill's `group
append`/`group rm-allow` subcommands (via `scripts/access-mutate.py`) — must
go through ``mutate_access_file`` so a concurrent owner/tier/group update and
a thread seed (or a skill invocation, a separate OS process) can never
lost-update each other. No ``discord`` import here on purpose: bridge call
sites need the AST/source-slicing workaround to keep tests free of a full
discord.py mock; this module doesn't, so its own tests — and the
skill-callable CLI — can just ``import`` it.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import uuid
from pathlib import Path

import sys as _sys

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in _sys.path:
    _sys.path.insert(0, str(_SRC))

from util_paths import claude_home_path, channel_access_path  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402


def discord_access_backup_file() -> Path:
    """The durable on-disk backup path discord-bridge.py restores from.

    Single source of truth for this path — see ``resolve_discord_access_file``,
    which uses its existence to decide between the canonical and legacy live
    file, and any writer (bridge or skill CLI) that needs to keep the backup
    in sync with a live write.
    """
    return resolve_workspace() / "state" / "auth" / "discord-access-backup.json"


def resolve_discord_access_file() -> Path:
    """Resolve the live Discord access.json path — the exact rule
    discord-bridge.py's own ``_resolve_access_file`` applies, extracted here
    so any writer (the bridge, or a skill-callable CLI in a separate process)
    resolves the identical file rather than duplicating the migration logic.

    A missing canonical file is a wipe to restore (durable backup) or a
    migration to run — never a reason to reach the pre-migration home-directory file;
    that fallback retired with its ~30-day window (shipped 2026-06-21).
    """
    if discord_access_backup_file().exists():
        return claude_home_path("channels", "discord", "access.json")
    return channel_access_path("discord")


def read_access_for_transaction(path: Path):
    """Read access.json for a caller about to write it back.

    Absent-vs-corrupt contract every writer shares:
      - present + valid  -> parsed dict
      - genuinely ABSENT  -> fresh default dict (safe to seed)
      - present + corrupt -> None (caller MUST NOT overwrite; see
        mutate_access_file, which enforces this by never writing on None).
        A prior bare-except default here once wiped a real allowFrom and
        leaked pairing codes into channels (2026-07-21) — corruption must
        never be treated the same as "genuinely absent".
    """
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {"dmPolicy": "pairing", "allowFrom": [], "pending": {}}
    except Exception:
        return None


@contextlib.contextmanager
def _locked(path: Path):
    """OS-level mutual exclusion across processes via a sidecar lock file.

    fcntl.flock only excludes other flock'ers of the SAME inode, so every
    caller — in-process and cross-process (a future `/discord:access` skill
    invocation included) — must go through this same lock file. Held for the
    read+mutate+write only; never across an await/network call by any caller.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _atomic_write_owner_only(path: Path, text: str) -> None:
    """Born-0600 temp + fsync + os.replace — never observable broader than
    owner-only, even under a permissive umask; a failed/partial write leaves
    any previous file at *path* untouched. Mirrors discord-bridge's
    _write_owner_only (kept in sync deliberately, not imported, so this
    module stays free of the discord-bridge import chain)."""
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def mutate_access_file(path: Path, mutator, *, backup=None):
    """The one locked read-modify-write transaction every access.json writer
    must call through.

    ``mutator(data) -> (new_data_or_None, result)``: called with the doc from
    ``read_access_for_transaction`` (never None — see below). Return
    ``(None, result)`` to bail without writing (e.g. mutator decides there's
    nothing to change); return ``(new_data, result)`` to persist ``new_data``
    and back it up. ``result`` is returned to the caller either way.

    If the file is present-but-corrupt, ``mutator`` is never called (mirrors
    every existing call site's "corrupt -> don't touch it" behavior) and this
    returns ``None`` — the caller must handle that the same way it already
    handles a corrupt read today.

    The lock is held for the read + mutator call + write only. Do not perform
    network I/O or otherwise await inside ``mutator`` — the whole point of a
    short critical section is that other writers (in-process or cross-process)
    are blocked for its duration.
    """
    with _locked(path):
        data = read_access_for_transaction(path)
        if data is None:
            return None
        new_data, result = mutator(data)
        if new_data is not None:
            _atomic_write_owner_only(path, json.dumps(new_data, indent=2) + "\n")
            if backup is not None:
                backup(new_data)
        return result
