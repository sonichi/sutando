"""sources/ — watcher SOURCE registry.

One module per client-side integration; each contributes SUBSCRIBE specifics
(stream_request/source_configured, CONFIG_KEYS/DEFAULTS/VAULT_KEYS) and a
NORMALIZE fn (event_to_task). The shared runner owns everything else. Mirrors
the broker's server-side integrations/ registry, one level up on the client.
"""

from __future__ import annotations

import importlib

SOURCES = {
    "bee": "ag2_sparrow.sources.bee",
}


def get(name: str):
    """The source module for `name`, or None if unregistered."""
    path = SOURCES.get(name)
    return importlib.import_module(path) if path else None
