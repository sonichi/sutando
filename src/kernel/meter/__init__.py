"""Durable usage metering.

    from kernel.meter import record
"""

from __future__ import annotations

from .meter import ledger_path, record

__all__ = ["record", "ledger_path"]
