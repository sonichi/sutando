"""The one boundary through which an adapter fetches its served setup from the backend.

Sparrow holds the gateway bearer, so Sparrow fetches: when a heartbeat reply announces a
setup_revision other than the one it holds, `Refresher` GETs the broker's `/v1/setup` (the
bearer's own `agent.setup.steps` result), validates it whole, and only then replaces
`served-setup[.<instance>].json`. A failed or refused fetch writes nothing, so the last good
copy stays, and is retried on a doubling backoff. `fetch_setup(kind, vendor, state_dir)`
returns that copy to an adapter, or None; every caller then falls back to the text and
defaults the package ships. A transport set with `set_transport` replaces the file read."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

from . import diagnostics, gateway_instance

Transport = Callable[[str, str], Optional[dict]]
_transport: "Transport | None" = None

SETUP_PATH = "/v1/setup"
SERVED_NAME = "served-setup"
RESULT_SCHEMA_VERSION = 1
TEXT_SCHEMA_VERSION = 1
# The backend serves these four together or not at all.
LISTENER_KEYS = ("text_schema_version", "runtime_text", "tunables", "package")
FETCH_TIMEOUT_S = 10
BACKOFF_MIN_S, BACKOFF_MAX_S = 60.0, 3600.0
MAX_BYTES = 256 * 1024


def set_transport(transport: "Transport | None") -> None:
    global _transport
    _transport = transport


def served_path(state_dir: Path, instance: str = "") -> Path:
    return Path(state_dir) / f"{SERVED_NAME}{gateway_instance.suffix(instance)}.json"


def load_served(state_dir: Path, instance: str = "") -> "dict | None":
    try:
        value = json.loads(served_path(state_dir, instance).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def fetch_setup(kind: str = "acp", vendor: str = "muse", state_dir: "Path | None" = None,
                instance: str = "") -> "dict | None":
    """{revision, text_schema_version, runtime_text, tunables, package} or None. Without a
    transport, the copy Sparrow last fetched, only when it is the setup for `kind`/`vendor`."""
    if _transport is not None:
        try:
            result = _transport(kind, vendor)
        except Exception:  # noqa: BLE001 - a failed fetch only means the shipped defaults apply
            return None
        return result if isinstance(result, dict) else None
    if state_dir is None:
        return None
    served = load_served(state_dir, instance)
    if served is None or (served.get("kind"), served.get("vendor")) != (kind, vendor):
        return None
    return served


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def result_problem(result, announced: str, agent: str = "", gateway_url: str = "") -> str:
    """Why a fetched `/v1/setup` result must not replace the last good copy; '' when it may."""
    if not isinstance(result, dict):
        return "not an object"
    version = result.get("schema_version")
    if not _is_int(version) or version != RESULT_SCHEMA_VERSION:
        return "unknown schema_version"
    if diagnostics.revision_field(result.get("revision"), gateway_url) != announced:
        return "revision is not the announced one"
    mxid = result.get("agent_mxid")
    if agent and (not isinstance(mxid, str) or mxid.lower() != agent.lower()):
        return "agent_mxid is not this agent"
    if not all(isinstance(result.get(k), str) and result.get(k) for k in ("kind", "vendor")):
        return "no kind or vendor"
    present = [result.get(k) is not None for k in LISTENER_KEYS]
    if any(present) and not all(present):
        return "listener fields are partly set"
    if all(present):
        text_version = result["text_schema_version"]
        if not _is_int(text_version) or text_version != TEXT_SCHEMA_VERSION:
            return "unknown text_schema_version"
        if not all(isinstance(result[k], dict) for k in ("runtime_text", "tunables", "package")):
            return "runtime_text, tunables or package is not an object"
    return ""


class Refresher:
    """Sparrow's side: keeps served-setup current with the announced revision.
    `get(path, timeout_s)` is the bridge's authenticated GET; `write(path, text)` its durable
    writer, returning False when the bytes did not commit."""

    def __init__(self, get, write, state_dir: Path, instance: str = "", log=print,
                 gateway_url: str = "", clock=time.monotonic):
        self._get, self._write, self._log, self._clock = get, write, log, clock
        self.path = served_path(state_dir, instance)
        self._gateway_url = gateway_url
        held = load_served(state_dir, instance) or {}
        self.held = diagnostics.revision_field(held.get("revision"), gateway_url)
        self._retry_at, self._backoff, self._failed_for = 0.0, 0.0, None

    def maybe_refresh(self, announced, agent: str = "") -> str:
        """One step; never raises. Returns what happened, for tests and the caller's log."""
        try:
            return self._step(announced, agent)
        except Exception as e:  # noqa: BLE001 - the heartbeat that called this must go on
            self._fail(announced, "error " + diagnostics.describe(e))
            return "failed"

    def _step(self, announced, agent: str) -> str:
        if not isinstance(announced, str) or not announced:
            return "none"
        if announced == self.held:
            return "unchanged"
        if announced == self._failed_for and self._clock() < self._retry_at:
            return "backoff"
        try:
            result = self._get(SETUP_PATH, FETCH_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - any failure keeps the last good copy
            code = getattr(e, "code", None)
            self._fail(announced, f"HTTP {code}" if _is_int(code) else diagnostics.describe(e))
            return "failed"
        why = result_problem(result, announced, agent, self._gateway_url)
        if not why:
            text = json.dumps(result, sort_keys=True, allow_nan=False)
            why = "too large" if len(text.encode("utf-8")) > MAX_BYTES else ""
        if why:
            self._fail(announced, f"refused ({why})")
            return "refused"
        if not self._write(self.path, text):
            self._fail(announced, "not written")
            return "failed"
        self.held, self._retry_at, self._backoff, self._failed_for = announced, 0.0, 0.0, None
        self._log("served setup fetched")
        return "fetched"

    def _fail(self, announced, why: str) -> None:
        # A newly announced revision starts its own backoff from the minimum.
        if announced != self._failed_for:
            self._backoff = 0.0
        self._backoff = min(max(self._backoff * 2, BACKOFF_MIN_S), BACKOFF_MAX_S)
        self._retry_at, self._failed_for = self._clock() + self._backoff, announced
        self._log(f"served setup not fetched: {why}; keeping the last good copy, "
                  f"retry in {self._backoff:.0f}s")
