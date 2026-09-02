"""Agent Endpoint resolver — resolve(endpoint, mode) → a transport route.

The four-concept model (design session 2026-08-07): callers name WHO they
want (`sutando://<id>`) and WHICH interaction lane (durable | realtime |
local-control); this module picks the transport. Call sites must never
hardcode a socket path, gateway URL, or task directory again — when a new
transport appears (the remote-realtime session gateway, a SQLite durable
store), it lights up here and zero call sites change.

Inputs are the AgentRuntime descriptor (`sutando-config.sh runtime` emits it:
runtimeSocket, workspace; call_tiers are voice/Web endpoints, deliberately
NOT a durable route) — injected for
tests, subprocess-loaded by default. Stdlib-only, no daemon, no lock service:
the resolver must answer under total daemon death (same R1 constraint as the
task protocol, because the durable lane is the crash path).

Invariant carried from the model: the route names a transport and an address,
NOTHING else — authorization, dedupe, and lifecycle stay with dispatcher/
policy and the Durable Work Model regardless of which route is returned.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SCHEME = "sutando://"

# Interaction lanes (concept #2). `durable` = submit work that must survive
# crashes and transport changes; `realtime` = interactive session (stream,
# cancel, typing); `local-control` = trusted same-machine control surface.
MODES = ("durable", "realtime", "local-control")

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}$")


class UnsupportedLane(Exception):
    """The lane is named by the model but has no transport yet.

    Deliberately loud: remote realtime is a KNOWN gap (voice is a bespoke
    exception, not a general lane). Callers that hit this should fall back to
    the durable lane explicitly, not silently."""


@dataclass(frozen=True)
class Route:
    transport: str   # "filesystem" | "uds" ("gateway" reserved for the remote lanes)
    address: str     # tasks dir or socket path
    endpoint: str    # normalized bare agent id
    mode: str


def parse_endpoint(endpoint: str) -> str:
    """`sutando://qingyun-001` or bare `qingyun-001` → validated bare id."""
    bare = endpoint[len(SCHEME):] if endpoint.startswith(SCHEME) else endpoint
    if not _ID_RE.match(bare):
        raise ValueError(f"not a sutando endpoint: {endpoint!r}")
    return bare


def load_descriptor() -> dict:
    """The live AgentRuntime descriptor. Callers in tests inject instead."""
    # Anchor on the config loader's repo-root walk (symlink/bundle-safe),
    # not a __file__ hop; sys.path guard covers spec_from_file_location loads.
    src_dir = str(Path(__file__).resolve().parent)
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    from sutando_config import _find_repo_root
    root = _find_repo_root()
    if root is None:
        raise ValueError(
            "cannot locate the repo root (sutando.config.json); "
            "cannot run sutando-config.sh runtime")
    out = subprocess.run(
        ["bash", "scripts/sutando-config.sh", "runtime"],
        capture_output=True, text=True, timeout=15, cwd=str(root))
    return json.loads(out.stdout)


def resolve(endpoint: str, mode: str, descriptor: dict, *,
            self_id: str | None = None) -> Route:
    """Pick the transport for (endpoint, mode) from the runtime descriptor.

    `self_id` names the local agent; endpoint == self_id (or the literal
    "self") routes locally; anything else is a remote agent. v0 scope: local
    durable/local-control/realtime
    only; every remote lane raises UnsupportedLane — realtime until the
    session gateway exists, durable until the descriptor carries a real
    task-gateway coordinate (call_tiers advertise the voice/Web endpoints,
    which do not speak the durable task protocol).
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    bare = parse_endpoint(endpoint)
    is_self = bare == "self" or (self_id is not None and bare == parse_endpoint(self_id))

    if is_self:
        if mode == "durable":
            workspace = descriptor.get("workspace", "")
            if not workspace:
                raise ValueError("descriptor has no workspace; cannot route durable lane")
            return Route("filesystem", str(Path(workspace) / "tasks"), bare, mode)
        # realtime and local-control both terminate on the runtime's UDS —
        # local realtime IS the UDS lane; browsers reach it via a localhost
        # gateway that itself ends on this same socket.
        socket = descriptor.get("runtimeSocket", "")
        if not socket:
            raise ValueError("descriptor has no runtimeSocket; cannot route local lane")
        return Route("uds", socket, bare, mode)

    if mode == "durable":
        # call_tiers are the voice/Web direct endpoints — a reachable tier is
        # NOT a durable task gateway. Loud until the descriptor names one.
        raise UnsupportedLane(
            f"remote durable to {bare!r}: the descriptor has no task-gateway "
            "coordinate (call_tiers are voice/Web endpoints, not the durable "
            "task API) — add a gateway field to the runtime descriptor first")

    raise UnsupportedLane(
        f"remote {mode} to {bare!r}: no session gateway exists yet — "
        "build the session gateway lane first")
