#!/usr/bin/env python3
"""result_claimant — which worker finished a task, read from its done flag.

The gateway stamps an outbound result with the worker that produced it. The
workspace layout that answer is read from lives HERE, not in the packaged
ag2-sparrow adapter: that adapter ships standalone and must not carry Sutando's
state shape (CLAUDE.md, "Optional capability discovery stays at the adapter
edge"). `src/remote-gateway-bridge.py` injects `resolve_claimant` into the
package at load, the same seam `set_task_stamper` already uses.

Two layouts are read. The pool's is not spelled here at all — it comes from its
own writer, `pool_delivery.done_flag`, so a move there surfaces as a failure
here instead of drifting into silence. The pre-pool per-core layout
(`state/cores/<instance>/done/<stem>.flag`, written by `finish_task` in
`src/pool_follower.py`) is spelled once, below.

Reading is strictly fail-closed: this module returns a name only when the state
tree names exactly one claimant. An ambiguous or unreadable tree raises, and the
caller abstains — a guess would draw another worker's name and colour on the
reply with full confidence, which is worse than no attribution at all.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pool_delivery  # noqa: E402

# Never a real workspace: only the shape of the writer's output is read off it.
_PROBE = Path("/_result_claimant_probe")

# Pre-pool layout. Its writer lives on the pool follower line, not here, so it
# cannot be imported the way the pool's own root is derived below.
_LEGACY_ROOT = "cores"


class Unattributable(Exception):
    """The state tree does not name exactly one claimant. The message is what
    the adapter logs; abstaining is always the answer."""


def _pool_flag(state: Path, worker: str, task_id: str) -> Path:
    rel = pool_delivery.done_flag(_PROBE, worker, task_id).relative_to(_PROBE / "state")
    return state / rel


def _legacy_flag(state: Path, worker: str, task_id: str) -> Path:
    return state / _LEGACY_ROOT / worker / "done" / f"{task_id}.flag"


_POOL_ROOT = _pool_flag(Path("/"), "w", "t").relative_to("/").parts[0]
_LAYOUTS = ((_POOL_ROOT, _pool_flag), (_LEGACY_ROOT, _legacy_flag))


def claimants(state_dir, task_id: str) -> list[str]:
    """Every worker whose done flag names `task_id`, across both layouts.

    `task_id` is the result stem, `task-` prefix included. Raises OSError when a
    root exists but cannot be fully enumerated: `Path.glob` reports that as an
    absence, and an absence here reads as "some other worker finished it".
    """
    state = Path(state_dir)
    found = set()
    for root, flag_of in _LAYOUTS:
        try:
            entries = list(os.scandir(state / root))
        except FileNotFoundError:
            continue  # this layout is simply not in use on this host
        for entry in entries:
            try:
                os.stat(flag_of(state, entry.name, task_id))
            except (FileNotFoundError, NotADirectoryError):
                continue
            found.add(entry.name)
    return sorted(found)


def resolve_claimant(state_dir, task_id: str) -> str:
    """The one worker that finished `task_id`, or "" when none claims it.

    Raises Unattributable when the tree cannot answer definitely, so the caller
    logs why it abstained rather than silently treating it as unclaimed.
    """
    try:
        found = claimants(state_dir, task_id)
    except OSError as exc:
        raise Unattributable(f"claim tree unreadable ({exc})") from exc
    if len(found) == 1:
        return found[0]
    if found:
        raise Unattributable(f"ambiguous done flags ({', '.join(found)})")
    return ""
