"""Shared file-attachment allowlist for `[file:|send:|attach:]` markers.

Single source of truth for the policy that decides whether an agent-
emitted file marker can be delivered to the owner's Discord DM /
channel. Used by:

  - ``src/discord-bridge.py`` — live WS-connected bridge
    (``discord.File(path)``).
  - ``src/dm-result.py`` — REST-only fallback when the bridge isn't
    available (``multipart/form-data`` upload, see PR #1029).

Per @liususan091219 review on PR #1029: keeping the policy as a copy
in each file *will* drift even with the "keep in sync" comment, so
extracting here gives both consumers a shared import. A tightening
of the allowlist now lands in both paths automatically; a broadening
is a single edit. Pre-extract, the two copies had already silently
diverged — dm-result was missing the personal-notes, Desktop, and
Documents roots that discord-bridge had.

The policy:

  * Files must be regular files (`os.path.isfile`) so symlinks
    pointing at non-existent destinations don't qualify.
  * `realpath` collapses the path before the prefix check — defeats
    symlink-to-`/etc/passwd` and `..` escapes.
  * Either the realpath equals an allowed-root or starts with
    ``<root>/`` (the trailing-slash check prevents
    ``/private/tmp/sutando-xyz`` from being treated as a child of
    ``/private/tmp/sutando``).
  * Prefix matches use `startswith` against the realpath so the
    same anti-symlink/anti-traversal property holds.

Per-machine paths (`~/Desktop/iclr-backups`,
`~/Documents/sutando-launch-assets`) resolve at module-load time
based on the current `Path.home()`; if the user's home dir changes
between processes, the resolved root differs accordingly — that's
the same shape the discord-bridge copy already had and matches
expectations for owner-local file roots.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _dirs import result_dir as _result_dir  # noqa: E402


# Transport default: only the RESULT_DIR is sendable (where the agent writes
# files to return). Any richer policy (sutando's notes/docs/etc.) is INJECTED by
# the embedding app via register_extra_roots() — the package ships no app-specific
# paths.
_BASE_ROOTS: tuple[str, ...] = (str(_result_dir()),)
_EXTRA_ROOTS: list[str] = []


def register_extra_roots(*roots) -> None:
    """Embedding apps (e.g. sutando) add extra send-allowed roots."""
    _EXTRA_ROOTS.extend(str(r) for r in roots)


def _allowed_roots() -> tuple[str, ...]:
    return _BASE_ROOTS + tuple(_EXTRA_ROOTS)

# Prefix forms — files whose realpath starts with any of these strings
# are deliverable. Covers temp-file artifacts the agent generates
# (`/tmp/sutando-recording-*.mov`, `/tmp/echo-screenshot-*.png`, etc.)
# without needing to enumerate every filename.
SEND_ALLOWED_PREFIXES: tuple[str, ...] = (
    "/tmp/sutando-",
    "/private/tmp/sutando-",
    "/tmp/echo-",
    "/private/tmp/echo-",
)


def is_path_sendable(fpath: str) -> bool:
    """True iff `fpath` is a regular file AND its `realpath` resolves
    under one of ``SEND_ALLOWED_ROOTS`` or starts with one of
    ``SEND_ALLOWED_PREFIXES``.

    Single source of truth for the file-attachment-delivery policy.
    Mirrors the shape used by ``_is_path_sendable`` in both call sites
    pre-extract.

    A non-existent path, a symlink to outside the allowlist (caught by
    realpath collapse), or a path that simply doesn't match any
    allowed root/prefix all return False. Callers should fail-closed
    on False (log + skip; never deliver the file).
    """
    if not os.path.isfile(fpath):
        return False
    try:
        real = os.path.realpath(fpath)
    except OSError:
        return False
    for root in _allowed_roots():
        root_real = os.path.realpath(root)
        if real == root_real or real.startswith(root_real + os.sep):
            return True
    for prefix in SEND_ALLOWED_PREFIXES:
        if real.startswith(prefix):
            return True
    return False
