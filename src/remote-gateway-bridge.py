#!/usr/bin/env python3
"""remote-gateway-bridge.py — sutando loader for the canonical ag2-sparrow client.

Post-#2082 (option A live-switch): the transport implementation lives
canonically in packages/ag2-sparrow/ag2_sparrow/remote_gateway_bridge.py
(published to PyPI as ag2-sparrow). This file keeps the sutando entrypoint +
workspace wiring:

  1. inject sutando's workspace dirs (tasks/, results/, state/) via
     ``set_dirs()`` BEFORE the client module evaluates its import-time config;
  2. pin MEDIA_DIR to ``<workspace>/data/remote-media`` — the transport's
     standalone default is ``STATE_DIR/remote-media``, sutando's pre-switch
     location was under ``data/`` and must not move;
  3. register sutando's extra send-allowed roots (notes/docs/owner asset dirs)
     on top of the transport's result-dir-only default;
  4. execute the canonical module source in THIS module's namespace with
     ``__package__`` pinned, so the package's relative imports resolve and
     every existing loader of this file — startup.sh's ``python3 src/...``,
     runpy via the deprecated remote-relay-bridge name, and the mock-gateway
     test harness (which loads the file several times under different env and
     mutates module attributes) — sees exactly the same module-level API and
     import-time env parsing as the pre-switch monolith.

Revert: restore this file's previous revision from git (the pre-switch
monolith is the parent commit of the one introducing this shim). No other
consumer changes are needed either way.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent
_REPO = _SRC.parent

# src/ first (workspace_default, util_paths), then the in-repo package root so
# ``import ag2_sparrow`` resolves without an install step (same code the PyPI
# dist ships; the in-repo copy is canonical for the live core).
for _p in (str(_SRC), str(_REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from proactive_routing import (BRIDGE_CHANNELS, body_target_channel,  # noqa: E402
                               proactive_destination, should_claim_proactive)
from delivery.readiness import read_ready_result  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402
from util_paths import claude_home_path, shared_personal_path  # noqa: E402

WS = resolve_workspace()

from ag2_sparrow._dirs import set_dirs  # noqa: E402

set_dirs(task_dir=WS / "tasks", result_dir=WS / "results", state_dir=WS / "state")

from task_envelope import stamp_text  # noqa: E402  (adapter-edge stamper)
from ag2_sparrow.local_task_protocol import set_task_stamper  # noqa: E402
set_task_stamper(stamp_text)
os.environ.setdefault("REMOTE_MEDIA_DIR", str(WS / "data" / "remote-media"))

from ag2_sparrow import send_allowlist as _send_allowlist  # noqa: E402

# Same roots the pre-switch monolith allowed via src/send_allowlist.py
# (result_dir is the transport's own base root — not repeated here).
# Membership guard: the test harness loads this file several times in one
# process; the package module is cached, so unguarded registration would
# stack duplicates.
for _root in (
    WS / "notes",
    shared_personal_path("notes", WS),
    WS / "docs",
    Path.home() / "Desktop" / "iclr-backups",
    Path.home() / "Documents" / "sutando-launch-assets",
):
    if str(_root) not in _send_allowlist._EXTRA_ROOTS:
        _send_allowlist.register_extra_roots(_root)

# Run the canonical client source in-place. exec (not import) is deliberate:
# module-level config (tier fail-closed parsing, URL/TOKEN, dirs) must
# re-evaluate on EVERY load of this file — the mock-gateway test harness loads
# it repeatedly under different env — and attribute reads/writes
# (``rtc._ack_disabled_until = 0.0``) must hit the same namespace the running code
# uses. A cached ``import ag2_sparrow.remote_gateway_bridge`` gives neither.
_IMPL = _REPO / "packages" / "ag2-sparrow" / "ag2_sparrow" / "remote_gateway_bridge.py"

# Runtime self-report, injected BEFORE the exec (anything after it is
# unreachable when the exec'd source's own __main__ guard fires; see #3285).
def _build_sha(repo):
    import subprocess
    from git_binary import git_argv  # resolver: never the bare CLT stub
    try:
        return subprocess.check_output(
            git_argv("-C", str(repo), "rev-parse", "HEAD"),
            text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
    except Exception:
        pass
    # Bundle install: the manifest's `sha` is the revision authority (its
    # built_at is a time, not a revision — never substituted here).
    try:
        import json as _json
        mf = _json.loads((Path(repo) / "ENGINE_MANIFEST.json").read_text())
        m = mf.get("sha") if isinstance(mf, dict) else None
        return m if isinstance(m, str) and len(m) >= 8 else None
    except (OSError, ValueError):
        return None

def _sha256_of(path):
    import hashlib
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None

# BOTH executed source inputs: the loader (this file) and the canonical
# implementation it compiles — a same-HEAD change to either must be visible.
RUNTIME_IDENTITY = {"build_sha": _build_sha(_REPO),
                    "entrypoint": str(Path(__file__).resolve()),
                    "loader_sha256": _sha256_of(__file__),
                    "module_sha256": _sha256_of(_IMPL)}
__package__ = "ag2_sparrow"  # PEP 328: makes the source's relative imports resolve
# The exec'd source's own __main__ guard must not fire mid-file: main() never
# returns, so every assignment below the exec stayed unreached in production.
_RUN_MAIN = __name__ == "__main__"
__name__ = "ag2_sparrow.remote_gateway_bridge"
exec(compile(_IMPL.read_text(encoding="utf-8"), str(_IMPL), "exec"), globals())

_CHANNEL = "ag2space"
# Fallback delay when no bridge for the routed channel exists on this host.
# Longer than a peer bridge's own poll grace so an installed bridge always
# gets first refusal; short enough that a gateway-only host stays responsive.
_PROACTIVE_GRACE_S = 180
# How long a configured bridge may show no sign of life before it is treated
# as gone. Hours, not minutes: it must survive a restart, a token reload and a
# laptop sleep, while still bounding the wait for a bridge that never returns.
_PROACTIVE_ABANDONED_S = 6 * 3600


def _channel_configured(channel: str) -> bool:
    """Whether `channel`'s bridge is installed on this host: a channel dir
    carrying `.env` or `access.json` — the same evidence health-check.py
    requires before it probes a bridge at all."""
    try:
        base = claude_home_path("channels", channel)
    except Exception:
        return False
    return (base / ".env").exists() or (base / "access.json").exists()


def _channel_last_alive(channel: str) -> float | None:
    """Newest of the bridge's own liveness traces, or None if it has never
    left one. Same traces (heartbeat, then log) health-check.py reads."""
    newest = None
    for p in (WS / "state" / f"{channel}-bridge.heartbeat",
              WS / "logs" / f"{channel}-bridge.log"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        newest = m if newest is None else max(newest, m)
    return newest


def _routed_bridge_still_owns(routed: str, path: Path, now: float) -> bool:
    """Whether `routed`'s bridge should keep this file rather than the gateway.

    DOWN is not ABSENT: age alone cannot tell "no telegram bridge here" from
    "the telegram bridge is restarting", and treating the second as the first
    hands a telegram-destined nudge to AG2 Space — the cross-channel
    mis-delivery this module exists to prevent. So configured-ness is asked
    FIRST and liveness only as a late tiebreaker: an installed bridge keeps
    its owner's file across any ordinary outage, but one silent for hours is
    treated as gone so the file cannot wait forever."""
    if not _channel_configured(routed):
        return False
    last = _channel_last_alive(routed)
    if last is None:
        # Installed but never seen running: bound the wait on the file itself
        # rather than assuming the bridge is either coming or gone.
        try:
            return (now - path.stat().st_mtime) < _PROACTIVE_ABANDONED_S
        except OSError:
            return True  # unreadable → leave it alone
    return (now - last) < _PROACTIVE_ABANDONED_S


def _ag2space_proactive_claim_gate(path: Path) -> bool:
    """Claim when routing says the owner lives here; otherwise claim only what
    no other bridge will ever take (see _routed_bridge_still_owns)."""
    # A filename destination outranks everything below, incl. the grace:
    # a destined file strands visibly rather than leak to the gateway room.
    dest = proactive_destination(path.name)
    if dest is not None:
        return dest == _CHANNEL
    # The BODY's redirect outranks activity (same rule as the bridge
    # gates) — a Matrix-targeted body is claimed here despite foreign activity.
    body = read_ready_result(path)
    if body:
        kind = body_target_channel(body)
        if kind is not None:
            return kind == _CHANNEL
    state = WS / "state" / "last-owner-activity.json"
    if should_claim_proactive(state, _CHANNEL):
        return True
    now = time.time()
    # Ask the SHARED policy which bridge routing prefers — never re-read the
    # state file here, or this becomes a second copy of the routing rule.
    # sorted() only makes the pick deterministic; it is not a priority order.
    routed = next(
        (c for c in sorted(BRIDGE_CHANNELS)
         if c != _CHANNEL and should_claim_proactive(state, c)),
        None,
    )
    if routed and _routed_bridge_still_owns(routed, path, now):
        return False
    try:
        return (now - path.stat().st_mtime) >= _PROACTIVE_GRACE_S
    except OSError:
        return False  # racing consumer already claimed it


# Assigned AFTER the exec: the canonical module's own `PROACTIVE_CLAIM_GATE =
# None` default runs inside it and would overwrite an earlier assignment.
PROACTIVE_CLAIM_GATE = _ag2space_proactive_claim_gate

if _RUN_MAIN:  # pragma: no cover — script-entry tail; the subprocess suite drives it
    __name__ = "__main__"
    main()  # noqa: F821  (defined by the exec above)
