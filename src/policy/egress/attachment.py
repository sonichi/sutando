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
from workspace_default import resolve_workspace  # noqa: E402
from util_paths import (  # noqa: E402
    legacy_dotted_workspace_path,
    shared_personal_path,
)

_REPO = resolve_workspace()

# Owner-relative + machine-local roots. Files under these roots are
# delivered to Discord as attachments without further checks.
SEND_ALLOWED_ROOTS: tuple[str, ...] = (
    str(_REPO / "results"),
    str(_REPO / "notes"),
    # Notes canonical home: save_note writes to the private location, so both
    # spellings stay allowed during the transition (resolver picks what exists).
    str(shared_personal_path("notes", _REPO)),
    str(_REPO / "docs"),
    # Rendered episode bundles. This tree can be a symlink out to the sync
    # root; roots are realpath'd below, and `generated` keeps notes/ private.
    str(legacy_dotted_workspace_path("notes", "generated")),
    str(Path.home() / "Desktop" / "iclr-backups"),
    str(Path.home() / "Documents" / "sutando-launch-assets"),
)

# Prefix forms: a file whose REALPATH starts with one of these is deliverable,
# so agent temp artifacts need no per-filename enumeration.
SEND_ALLOWED_PREFIXES: tuple[str, ...] = (
    "/tmp/sutando-",
    "/private/tmp/sutando-",
    "/tmp/echo-",
    "/private/tmp/echo-",
)


def is_path_sendable(fpath: str, extra_roots: tuple[str, ...] = ()) -> bool:
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

    `extra_roots` lets ONE adapter extend the policy with a root that is
    genuinely its own without that root becoming global. Slack passes its
    inbound `slack-inbox/` so an uploaded file can be echoed back; nothing
    else may send from there. Extra roots go through the SAME realpath +
    boundary check as the canonical ones — they widen the allowlist, never
    the matching rule, so a symlink escape is still caught.
    """
    if not os.path.isfile(fpath):
        return False
    try:
        real = os.path.realpath(fpath)
    except OSError:
        return False
    for root in (*SEND_ALLOWED_ROOTS, *extra_roots):
        root_real = os.path.realpath(root)
        if real == root_real or real.startswith(root_real + os.sep):
            return True
    for prefix in SEND_ALLOWED_PREFIXES:
        if real.startswith(prefix):
            return True
    return False

# A caller must handle every outcome: a branch set that omits one drops
# that case silently.
ATTACH_SEND = "send"
ATTACH_EMPTY = "empty"
ATTACH_MISSING = "missing"
ATTACH_REFUSED = "refused"


def classify_attachment(fpath: str, extra_roots: tuple = ()) -> tuple:
    """(outcome, normalized path) for one `[file:]` marker value.

    REFUSED is the case that motivates this: a path that exists but fails the
    allowlist is authorization-denied, not absent, and a caller that tests only
    `sendable` then `not isfile` matches neither branch -- so the file is
    dropped with no send, no log and no error.
    """
    norm = os.path.expanduser((fpath or "").strip())
    if not norm:
        return (ATTACH_EMPTY, norm)
    # Order preserved from the call sites this replaces: authorization first,
    # so a sendable path never pays a stat.
    if is_path_sendable(norm, extra_roots):
        return (ATTACH_SEND, norm)
    if not os.path.isfile(norm):
        return (ATTACH_MISSING, norm)
    return (ATTACH_REFUSED, norm)
