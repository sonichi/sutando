"""Narrow observability + metering config slice.

    from kernel.config import load_observability_config
"""

from __future__ import annotations

from .observability_config import OBSERVABILITY_DEFAULTS, load_observability_config

__all__ = ["OBSERVABILITY_DEFAULTS", "load_observability_config"]
