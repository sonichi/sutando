"""Compatibility shim — canonical module is `delivery.channel_key` (phase-1a restructure).
Kept one transition window for out-of-tree importers; see docs/migration-transition-window.md.
"""
import sys as _sys
from delivery.channel_key import *  # noqa: F401,F403
_sys.modules[__name__].__dict__.update(
    {k: v for k, v in _sys.modules["delivery.channel_key"].__dict__.items() if not k.startswith("__")})
