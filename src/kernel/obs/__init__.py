"""Observability facade.

    from kernel.obs import emit
"""

from __future__ import annotations

from .obs import emit, register_sink, reset_sinks, set_sampler
from .sink import JsonlFileSink, Sink, sink_from_config

__all__ = [
    "emit",
    "register_sink",
    "reset_sinks",
    "set_sampler",
    "Sink",
    "JsonlFileSink",
    "sink_from_config",
]
