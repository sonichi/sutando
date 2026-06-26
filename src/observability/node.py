"""Node (machine) identity for the observability + metering spine.

Every emitted event/usage record carries ``node`` -- which MACHINE produced it --
so a multi-core fleet's events are attributable per host.

Resolution (twin of ``node.ts``, intentionally identical and decoupled):
  1. ``SUTANDO_NODE_ID`` env var (explicit override).
  2. short hostname (first dot-segment).

Deliberately does NOT reach into the workspace/memory layer to read
``stand-identity.json`` -- that keeps this module free of the V1 hold-list
(``util_paths``) dependency. A deployment that wants the stand-identity machine
name as the node id has ``runtime/boot`` export ``SUTANDO_NODE_ID`` from it; the
the module just reads the env. Cached after first resolution.
"""

from __future__ import annotations

import os
import socket

__all__ = ["node_id", "reset_node_id"]

_cached: str | None = None


def node_id() -> str:
    global _cached
    if _cached is not None:
        return _cached
    override = os.environ.get("SUTANDO_NODE_ID", "").strip()
    if override:
        _cached = override
        return _cached
    _cached = socket.gethostname().split(".")[0] or "unknown"
    return _cached


def reset_node_id() -> None:
    """Test hook -- clears the cache so an env change takes effect."""
    global _cached
    _cached = None
