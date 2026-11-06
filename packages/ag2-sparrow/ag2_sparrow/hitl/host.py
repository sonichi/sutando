"""The machine a requirement's terminal is on: the per-host label, "" when unreadable."""
from __future__ import annotations


def device_host() -> str:
    # Same source as the relay's "answer it at the core's terminal on <host>" line.
    try:
        from util_paths import _host_label
        return str(_host_label() or "")
    except Exception:
        return ""
