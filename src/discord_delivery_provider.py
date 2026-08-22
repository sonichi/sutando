"""Alias of `channels.discord.delivery_provider` (phase-1a restructure); one transition window."""
import importlib as _importlib
import sys as _sys
_sys.modules[__name__] = _importlib.import_module("channels.discord.delivery_provider")
