"""Sutando observability + metering — the local spine.

Domain-oriented layout (was src/kernel + src/adapters/executor/claude-code):
    from observability.obs import emit
    from observability.meter import record
    from observability.config import load_observability_config

Requires the repo ``src/`` on ``sys.path`` (which also resolves the flat
``workspace_default`` twin the modules import).
"""
from __future__ import annotations
