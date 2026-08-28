#!/usr/bin/env python3
"""Per-host services-status emitter for the bundled Sutando runtime.

Writes `<workspace>/state/services-status.json` every ~30 seconds: a single
snapshot of which sidecar services are running/offline, for the desktop
Settings → Services surface (`ServicesSettings.tsx` in ag2space-cinny-desktop)
to render per-service badges + a restart affordance.

Why aggregate, not re-probe
---------------------------
Each sidecar already leaves a liveness trace — the core heartbeat's
`state/cores/<host>.alive` (mtime = liveness), the task watcher's
`state/watch-tasks-stream.pid`, a listening TCP port for network services.
This emitter *reads those existing signals* and folds them into one file the
UI can consume, rather than inventing a second, divergent source of truth.

Schema (v1) — matches the contract handed to the desktop UI:
    {
      "schema_version": 1,
      "emitted_at": <epoch s>,           # when this file was written
      "host": "<host-label>",
      "services": [
        {"id","name","status","pid","since","detail","last_check_at"}, ...
      ]
    }
  status ∈ running | offline | degraded | unknown
  Freshness: a reader treats the whole file as stale (→ "unknown") when
  `now - emitted_at > 90` (same window as the .alive liveness signal).

The `id` is the stable machine key the UI's restart affordance targets; it is
kept stable across display-name (`name`) changes.

This mirrors `src/core_heartbeat.py` exactly in shape — standalone, launched by
startup.sh, atomic-write, SIGTERM/SIGINT cleanup. The probe helpers are pure
(clock + filesystem + a connect/pid callable injected), so the decision logic
is unit-testable with no real processes or sockets.

Usage:
  python3 src/services_status.py               # 30s loop
  python3 src/services_status.py --once         # single emit (tests/debug)
  python3 src/services_status.py --interval 10  # custom cadence
"""
from __future__ import annotations
import argparse
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

# Resolve workspace via the M0 helper (same rationale as core_heartbeat.py):
# a status file written to the wrong tree is invisible to every post-M0 reader.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402
from gateway_serving import read_verdict as read_gateway_verdict  # noqa: E402

WORKSPACE = resolve_workspace()
STATE_DIR = WORKSPACE / "state"
CORES_DIR = STATE_DIR / "cores"

SCHEMA_VERSION = 1
STATUS_PATH = STATE_DIR / "services-status.json"

# Liveness window shared with the .alive heartbeat: a signal older than this is
# treated as "the thing that writes it is gone", i.e. offline.
ALIVE_TTL_S = 90.0
# The gateway bridge rewrites state/gateway-status.json on EVERY poll outcome,
# so a sidecar older than this means the bridge is wedged or predates the
# sidecar — either way it has no usable opinion and pgrep answers instead.
GATEWAY_STATUS_PATH = STATE_DIR / "gateway-status.json"
GATEWAY_STATUS_TTL_S = 180.0


def _host_label() -> str:
    """Per-host label — delegates to util_paths (honors $SUTANDO_HOST_LABEL,
    else short hostname), matching the heartbeat's `.alive` filename so `core`
    resolves to the right per-host signal. Falls back to short hostname."""
    try:
        from util_paths import _host_label as hl
        return hl()
    except Exception:  # pragma: no cover — defensive fallback if util_paths absent
        return socket.gethostname().split(".")[0]


# ---------------------------------------------------------------------------
# Pure probe helpers — status decision only; all I/O injected or read directly.
# Each returns (status, detail, since) where since is best-effort or None.
# ---------------------------------------------------------------------------

def probe_alive_file(path: Path, now: float, ttl: float = ALIVE_TTL_S) -> tuple[str, str, float | None]:
    """A heartbeat-style `.alive` file: running if its mtime is within `ttl`,
    else offline. Missing file → offline. Unreadable → unknown."""
    try:
        if not path.exists():
            return ("offline", "no heartbeat file", None)
        mtime = path.stat().st_mtime
        age = now - mtime
        if age <= ttl:
            return ("running", f"beat {int(age)}s ago", mtime)
        return ("offline", f"stale {int(age)}s", mtime)
    except OSError as e:  # pragma: no cover — defensive; hard to trigger in tests
        return ("unknown", f"stat failed: {e}", None)


def probe_pidfile(path: Path, pid_alive) -> tuple[str, str, float | None]:
    """A `<name>.pid` file holding a single PID: running if `pid_alive(pid)`.
    Missing/empty file → offline. Malformed → unknown. `pid_alive` is injected
    (real: os.kill(pid, 0)) so the branch logic is testable without a process."""
    try:
        if not path.exists():
            return ("offline", "no pidfile", None)
        raw = path.read_text().strip()
        if not raw:
            return ("offline", "empty pidfile", None)
        pid = int(raw)
    except (OSError, ValueError) as e:
        return ("unknown", f"unreadable pidfile: {e}", None)
    if pid <= 0:
        # os.kill(0, 0) / negative pids signal the process GROUP, which succeeds
        # and would read as falsely "running" — a 0/negative pidfile is corrupt.
        return ("unknown", f"non-positive pid {pid} in pidfile", None)
    if pid_alive(pid):
        return ("running", f"pid {pid}", None)
    return ("offline", f"pid {pid} dead", None)


def probe_port(port: int, connect) -> tuple[str, str, float | None]:
    """A listening TCP port: running if `connect(port)` succeeds. `connect` is
    injected (real: a short-timeout socket connect to 127.0.0.1:port) so tests
    don't open real sockets."""
    try:
        if connect(port):
            return ("running", f"listening :{port}", None)
        return ("offline", f":{port} not listening", None)
    except Exception as e:  # pragma: no cover — connect callable is defensive
        return ("unknown", f"probe error: {e}", None)


def probe_process(pattern: str, pgrep) -> tuple[str, str, float | None]:
    """A process matched by a `pgrep -f <pattern>`: running if `pgrep(pattern)`
    returns a truthy pid list. For services with no port/pidfile (the bridges,
    the gateway) — the same detection health-check.py uses. `pgrep` is injected
    (real: /usr/bin/pgrep -f) so the branch logic is testable without a process."""
    try:
        pids = pgrep(pattern)
        if pids:
            return ("running", f"pid {pids[0]}", None)
        return ("offline", "no process", None)
    except Exception as e:  # pragma: no cover — pgrep callable is defensive
        return ("unknown", f"probe error: {e}", None)


def probe_gateway(
    path: Path, pattern: str, now: float, pgrep, ttl: float = GATEWAY_STATUS_TTL_S
) -> tuple[str, str, float | None]:
    """Gateway liveness: prefer the bridge's OWN status sidecar, fall back to pgrep.

    `probe_process` answers "does a process with this argv exist", which is not
    the same question as "is the connection serving". A gateway whose route has
    gone can sit in a retry/backoff loop for hours with the process healthy, and
    the dashboard would show it green the whole time (observed 2026-07-28: 4.9h
    of `connected: false` reported as `running`, pid and all).

    `state/gateway-status.json` is written by the transport on every poll
    outcome, so it answers the real question. Missing or stale (bridge wedged,
    or too old to emit one) → no opinion, and the pgrep probe answers as before,
    so hosts running an older bridge keep their previous behaviour.

    Same precedence `core-input-watch.gateway_alive()` adopted in #2253.
    """
    # Serving verdict is gateway_serving's; the TTL, the rendering and the
    # pgrep fallback are this reader's.
    v = read_gateway_verdict(path, now=now, max_age=ttl)
    if v is not None:
        if v.serving:
            return ("running", "connected", v.last_ok_ts)
        if v.never_polled:
            # connected, but no completed poll to point at — the shape a dead
            # bridge's own last write leaves behind.
            return ("offline", "not serving — no successful poll yet", None)
        detail = "not serving"
        if v.last_ok_ts:
            detail = f"not serving — no successful poll for {int(now - v.last_ok_ts)}s"
        return ("offline", detail, v.last_ok_ts)
    return probe_process(pattern, pgrep)  # absent/unreadable/malformed/stale


def _real_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        # PermissionError → process exists but is not ours: still "alive".
        return isinstance(sys.exc_info()[1], PermissionError)
    except OSError:  # pragma: no cover
        return False


def _real_connect(port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _real_pgrep(pattern: str) -> list[str]:
    import subprocess
    try:
        out = subprocess.run(
            ["/usr/bin/pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=3,
        )
        return [p for p in out.stdout.split() if p]
    except Exception:  # pragma: no cover — pgrep missing/timeout → treat as none
        return []


def service_registry() -> list[dict]:
    """The full supervised-service set the desktop Settings → Services surface
    shows — matched to sutando-desktop's dashboard (owner G9, 2026-07-18: "all
    the services shown in the dashboard of Sutando-desktop"). Ports + pgrep
    patterns mirror `src/health-check.py` (the detection source of truth):
    voice-agent :9900, web-client :8080, conversation-server :3100,
    screen-capture :7845, credential-proxy :7846; gateway + bridges are pgrep'd
    (no fixed port). A service not running reports `offline` (correct — the UI
    shows it greyed), so listing all is safe on hosts that run only some."""
    host = _host_label()
    return [
        {"id": "core", "name": "Sutando Core",
         "probe": ("alive_file", CORES_DIR / f"{host}.alive")},
        # KNOWINGLY PRIMARY-ONLY (#2503): named GATEWAY_INSTANCE bridges publish
        # SUFFIXED sidecars this probe does not read, and the pgrep fallback is
        # identity-blind (instance identity lives only in env — the launcher's
        # P1). With a live named secondary, a dead primary's stale sidecar falls
        # through to pgrep and the secondary's process reads as "running".
        # Instance-aware probing is tracked separately; until then this row
        # reports the PRIMARY bridge only.
        {"id": "gateway", "name": "AG2 Gateway",
         "probe": ("gateway", GATEWAY_STATUS_PATH, r"remote-gateway-bridge\.py$")},
        {"id": "task-watcher", "name": "Task Watcher",
         "probe": ("pidfile", STATE_DIR / "watch-tasks-stream.pid")},
        {"id": "voice-agent", "name": "Voice Agent",
         "probe": ("port", 9900)},
        {"id": "web-client", "name": "Web Client",
         "probe": ("port", 8080)},
        {"id": "conversation-server", "name": "Phone",
         "probe": ("port", 3100)},
        {"id": "screen-capture", "name": "Screen Capture",
         "probe": ("port", 7845)},
        {"id": "credential-proxy", "name": "Credential Proxy",
         "probe": ("port", 7846)},
        # `$`-anchored, like the gateway row above. An UNANCHORED pattern also
        # matches any process that merely MENTIONS the script — most concretely
        # `python3 src/discord-bridge.py send <channel> <text>`, the one-off REST
        # send used to post from outside the daemon. Measured: with such a send
        # in flight, `pgrep -f 'discord-bridge\.py'` returned BOTH it and the
        # daemon, so a dead daemon would have read `running` for the life of the
        # send. Anchoring keeps the daemon (each launches with the script path
        # LAST in argv) and drops the sub-command form, which has trailing args.
        {"id": "discord-bridge", "name": "Discord",
         "probe": ("process", r"discord-bridge\.py$")},
        {"id": "slack-bridge", "name": "Slack",
         "probe": ("process", r"slack-bridge\.py$")},
        {"id": "telegram-bridge", "name": "Telegram",
         "probe": ("process", r"telegram-bridge\.py$")},
    ]


def build_payload(
    registry: list[dict],
    now: float,
    *,
    pid_alive=_real_pid_alive,
    connect=_real_connect,
    pgrep=_real_pgrep,
) -> dict:
    """Assemble the full services-status payload from the registry. Pure given
    the injected `pid_alive`/`connect`/`pgrep` callables and `now`."""
    services = []
    for spec in registry:
        kind, *args = spec["probe"]
        arg = args[0]
        if kind == "gateway":
            status, detail, since = probe_gateway(args[0], args[1], now, pgrep)
        elif kind == "alive_file":
            status, detail, since = probe_alive_file(arg, now)
        elif kind == "pidfile":
            status, detail, since = probe_pidfile(arg, pid_alive)
        elif kind == "port":
            status, detail, since = probe_port(arg, connect)
        elif kind == "process":
            status, detail, since = probe_process(arg, pgrep)
        else:  # pragma: no cover — registry is code-owned; guards a typo
            status, detail, since = ("unknown", f"bad probe kind: {kind}", None)
        services.append({
            "id": spec["id"],
            "name": spec["name"],
            "status": status,
            "pid": None,
            "since": since,
            "detail": detail,
            "last_check_at": now,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "emitted_at": now,
        "host": _host_label(),
        "services": services,
    }


def write_payload(payload: dict, path: Path | None = None) -> None:
    """Atomic write (tmp + rename) so a concurrent reader never sees a partial
    file — same discipline as core_heartbeat.write_beat. `path` resolves to the
    module-level STATUS_PATH at call time (not def time) so a reassignment of
    STATUS_PATH — e.g. in tests — is honored."""
    if path is None:
        path = STATUS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


_SHUTDOWN_REQUESTED = False


def _handle_signal(signum: int, frame) -> None:
    global _SHUTDOWN_REQUESTED
    _SHUTDOWN_REQUESTED = True


def emit_once() -> dict:
    """Build + write one snapshot; return it (for tests/debug)."""
    payload = build_payload(service_registry(), time.time())
    write_payload(payload)
    return payload


def run_forever(interval: float = 30.0) -> int:
    # Clamp to a 1s floor: interval <= 0 would make the sleep slices zero and
    # spin the loop (continuous CPU + disk writes).
    interval = max(1.0, interval)
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    while not _SHUTDOWN_REQUESTED:
        try:
            emit_once()
        except Exception as e:  # pragma: no cover — transient FS hiccup; retry
            print(f"services_status: write failed: {e}", file=sys.stderr, flush=True)
        slept = 0.0
        slice_s = min(1.0, interval)
        while slept < interval and not _SHUTDOWN_REQUESTED:  # pragma: no cover — timing glue; loop body tested via injected emit
            time.sleep(slice_s)
            slept += slice_s
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--interval", type=float, default=30.0, help="seconds between emits (default: 30)")
    p.add_argument("--once", action="store_true", help="emit a single snapshot and exit")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.once:
        emit_once()
        return 0
    return run_forever(interval=args.interval)  # pragma: no cover — forever-loop entrypoint


if __name__ == "__main__":
    sys.exit(main())
