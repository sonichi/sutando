"""The one boundary through which an adapter fetches its served setup from the backend.

`fetch_setup(kind, vendor)` returns the `agent.setup.steps` result as a dict, or None when
it cannot be fetched; every caller then falls back to the text and defaults the package
ships. The package has no client for that Action yet, so no transport is installed and
the fetch returns None; a transport is a callable `(kind, vendor) -> dict | None`."""
from __future__ import annotations

from typing import Callable, Optional

Transport = Callable[[str, str], Optional[dict]]
_transport: "Transport | None" = None


def set_transport(transport: "Transport | None") -> None:
    global _transport
    _transport = transport


def fetch_setup(kind: str = "acp", vendor: str = "muse") -> "dict | None":
    """{revision, text_schema_version, runtime_text, tunables, package} or None."""
    if _transport is None:
        return None
    try:
        result = _transport(kind, vendor)
    except Exception:  # noqa: BLE001 - a failed fetch only means the shipped defaults apply
        return None
    return result if isinstance(result, dict) else None
