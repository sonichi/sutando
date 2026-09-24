#!/usr/bin/env python3
"""Shared authority for whether Claude quota telemetry is usable.

Several readers interpret the same workspace state -- the credential proxy's
`state/quota-state.json` -- so the policy lives here and each reader calls it:
the quota tools (`skills/quota-tracker`, `skills/proactive-loop`), health-check,
the dashboard, and the delivery gate (`src/delivery/pane_gate.py`).

The record is the provider's own statement, refreshed by real traffic, but it
only speaks for a seat whose requests go THROUGH the proxy: a fresh `allowed`
written by some other process says nothing about an unrouted seat. So the one
decision is routed AND fresh AND accepted, never any of those alone.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from workspace_default import status_read_path

PROXY_PORT = 7846
PROXY_SCHEME = "http"
PROXY_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def points_at_credential_proxy(base_url: "str | None") -> bool:
    """Return whether *base_url* targets this host's credential proxy."""
    if not base_url:
        return False
    try:
        parsed = urlparse(base_url if "//" in base_url else "//" + base_url)
        host = (parsed.hostname or "").strip().lower()
        port = parsed.port
    except ValueError:
        return False
    scheme = (parsed.scheme or PROXY_SCHEME).lower()
    return (
        scheme == PROXY_SCHEME
        and host in PROXY_HOSTS
        and port == PROXY_PORT
    )


def quota_windows(headers: dict) -> dict:
    """Every window the proxy reported, keyed by window, as (utilization or None,
    that window's own status or None). A window is named by its `-utilization`
    header OR its `-status` header: a rejected status can arrive with no
    utilization, and a scan keyed on utilization alone would never see it.
    The headline `-status` is not a window."""
    out = {}
    prefix = "anthropic-ratelimit-unified-"
    names = []
    for k in headers:
        if not k.startswith(prefix):
            continue
        rest = k[len(prefix):]
        for suffix in ("-utilization", "-status"):
            if rest.endswith(suffix):
                w = rest[:-len(suffix)]
                if w and w not in names:
                    names.append(w)
    for w in names:
        v = headers.get(f"{prefix}{w}-utilization")
        try:
            u = float(v)
        except (TypeError, ValueError):
            u = None
        st = headers.get(f"{prefix}{w}-status")
        out[w] = (u, str(st) if st is not None else None)
    return out


def limit_windows(headers: dict) -> dict:
    """`quota_windows` minus `overage`: overage is purchase eligibility, not a
    limit. The live record carries it rejected while the account is fully allowed."""
    return {w: v for w, v in quota_windows(headers).items() if w != "overage"}


def resolve_available(status: str, proxy_available: Any, headers: Optional[dict] = None) -> bool:
    """Resolve the proxy's persisted availability signal without coercion.

    `headers`, when given, gates on EVERY reported window, not just the
    headline: the proxy's `available` flag only reflects the overall/5h
    windows, so a per-model or weekly window (e.g. `7d_oi`) can be rejected
    while the headline and `available` both still read allowed.
    """
    if status == "rejected":
        return False
    if headers and any(st == "rejected" for _u, st in limit_windows(headers).values()):
        return False
    if isinstance(proxy_available, bool):
        return proxy_available
    return status == "allowed"


def availability_decision(
    quota: Any,
    *,
    base_url: "str | None",
    stale: bool,
) -> dict[str, Any]:
    """Return the one authoritative routed/fresh/accepted quota decision."""
    payload = quota if isinstance(quota, dict) else {}
    headers = payload.get("headers")
    headers = headers if isinstance(headers, dict) else {}
    status = headers.get("anthropic-ratelimit-unified-status", "unknown")
    routed = points_at_credential_proxy(base_url)
    accepted = resolve_available(str(status), payload.get("available"), headers)
    available = accepted and routed and not stale
    return {
        "available": available,
        "routed": routed,
        "status": status,
        "unavailable_reason": (
            None if available
            else "not-routed" if not routed
            else "stale" if stale
            else "rejected"
        ),
    }


def gate_windows_allowed(quota: Any) -> bool:
    """The DELIVERY GATE's stricter reading, on top of `availability_decision`:
    the headline and every window `quota_windows` reports must be exactly
    `allowed`. `resolve_available` already refuses a rejected window; this also
    refuses `allowed_warning`, and a record with no headline at all is silence,
    and silence holds."""
    payload = quota if isinstance(quota, dict) else {}
    headers = payload.get("headers")
    headers = headers if isinstance(headers, dict) else {}
    headline = headers.get("anthropic-ratelimit-unified-status")
    if headline != "allowed":
        return False
    return all(st in (None, "allowed") for _u, st in limit_windows(headers).values())


# ---- the record on disk ---------------------------------------------------

#: A limit can begin at any moment, so only a RECENT observation vouches for now.
#: Health-check's six-hour horizon asks "is the proxy wired"; this asks "is it lifted".
FRESH_SEC = 10 * 60


@dataclass(frozen=True)
class QuotaRecord:
    payload: dict
    age_s: Optional[float]    # None: no usable timestamp on the record

    def fresh(self, fresh_sec: float = FRESH_SEC) -> bool:
        return self.age_s is not None and 0 <= self.age_s <= fresh_sec


def _parse_when(value) -> Optional[float]:
    """An ISO-8601 timestamp (the proxy writes `...Z`) as epoch seconds, else None."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def read_quota_record(workspace, now: Optional[float] = None) -> Optional[QuotaRecord]:
    """The record at `<workspace>/state/quota-state.json`, or None when absent or
    unreadable. Never raises: every caller is on a path that must fail closed."""
    path = status_read_path("quota-state.json", Path(workspace))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    when = _parse_when(data.get("last_checked"))
    if when is None:
        try:
            when = path.stat().st_mtime
        except OSError:
            when = None
    at = time.time() if now is None else now
    return QuotaRecord(data, None if when is None else at - when)


# ---- the running seat's routing -------------------------------------------

@dataclass(frozen=True)
class SeatEnv:
    """What the running seat carries. `observed` False means its environment could
    not be read: unknown, never a bypass. `base_url` and `config_dir` are the
    process's ANTHROPIC_BASE_URL and CLAUDE_CONFIG_DIR (None when absent); `cwd`
    is its pane's current path, where a probe must run."""
    observed: bool
    base_url: Optional[str]
    cwd: Optional[str] = None
    config_dir: Optional[str] = None


def _run_tmux(socket_path: str, *args: str, tmux_bin: str = "tmux"):
    try:
        return subprocess.run([tmux_bin, "-S", socket_path, *args],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None


def _run_ps(pid: str):
    return subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)],
                          capture_output=True, text=True, timeout=15)


_ENV_PAIR = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def seat_env_base_url(socket_path: Optional[str], session: str,
                      tmux_runner: Optional[Callable] = None,
                      ps_runner: Optional[Callable] = None) -> SeatEnv:
    """ANTHROPIC_BASE_URL as the RUNNING seat carries it, read from its process.

    The pid comes from tmux, never `pgrep -f` (which matches any argv holding the
    string, this probe's own shell included). EVERY pane of the session is
    enumerated, because the runtime keeps sibling windows in one session and
    `list-panes -t` resolves to the current window; the seat is the one process
    whose argv carries `--name <session>`, which is how the launcher starts it.
    Zero or several matches, no readable environment, no such session: unobserved.
    """
    tmux_runner = tmux_runner or _run_tmux
    ps_runner = ps_runner or _run_ps
    if not socket_path:
        return SeatEnv(False, None)
    panes = tmux_runner(socket_path, "list-panes", "-s", "-t", f"={session}", "-F", "#{pane_pid} #{pane_current_path}")
    if panes is None or getattr(panes, "returncode", 1) != 0:
        return SeatEnv(False, None)
    rows = [ln.split(None, 1) for ln in (panes.stdout or "").splitlines() if ln.strip()]
    pids = [(r[0], r[1].strip() if len(r) > 1 else None) for r in rows if r[0].isdigit()]
    if not pids:
        return SeatEnv(False, None)

    def _names_this_session(argv: str) -> bool:
        toks = argv.split()
        for i, t in enumerate(toks):
            if t == "--name" and i + 1 < len(toks) and toks[i + 1] == session:
                return True
            if t == f"--name={session}":
                return True
        return False

    matches = []
    for pid, cwd in pids:
        try:
            proc = ps_runner(pid)
        except Exception:  # noqa: BLE001 -- a probe failure is unknown
            return SeatEnv(False, None)
        if proc is None or getattr(proc, "returncode", 1) != 0:
            continue
        out = proc.stdout or ""
        if _names_this_session(out):
            matches.append((out, cwd))
    if len(matches) != 1:
        return SeatEnv(False, None)
    argv, cwd = matches[0]
    pairs = [t for t in argv.split() if _ENV_PAIR.match(t)]
    if not pairs:
        return SeatEnv(False, None)
    env = {t.split("=", 1)[0]: t.split("=", 1)[1] for t in pairs}
    return SeatEnv(True, env.get("ANTHROPIC_BASE_URL"), cwd, env.get("CLAUDE_CONFIG_DIR"))


# ---- whose model the record speaks for ------------------------------------

_MODEL_TAG = re.compile(r"\[[^\]]*\]$")   # `claude-x[1m]` names the same model as `claude-x`


def _norm_model(value) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return _MODEL_TAG.sub("", value.strip())


def seat_model(env: SeatEnv, workspace) -> Optional[str]:
    """The model the seat is running: its config dir's `settings.json` (what a
    bare `claude` in that seat resolves), else `state/model-switch.json`. None
    when neither says."""
    candidates = []
    if env.config_dir:
        candidates.append(Path(env.config_dir) / "settings.json")
    candidates.append(Path(workspace) / "state" / "model-switch.json")
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        model = _norm_model(data.get("model")) if isinstance(data, dict) else None
        if model:
            return model
    return None


def record_model(quota: Any) -> Optional[str]:
    """The model of the request that last refreshed the record, if it says."""
    payload = quota if isinstance(quota, dict) else {}
    last = payload.get("last_request")
    return _norm_model(last.get("model")) if isinstance(last, dict) else None


def record_speaks_for_seat(quota: Any, env: SeatEnv, workspace) -> bool:
    """True only when the seat's model and the record's are both known and equal.
    A record refreshed on another model may say allowed while THIS seat's model
    is limited, and the proxy's headers carry no per-model window to catch that
    (measured: status, 5h, 7d, overage -- nothing per model), so unknown holds."""
    mine, theirs = seat_model(env, workspace), record_model(quota)
    return mine is not None and theirs is not None and mine == theirs


# ---- refreshing a stale record on purpose ---------------------------------

PROBE_TIMEOUT_S = 60
#: Under state/: the last probe attempt, so every notifier on one host sends one probe per window.
PROBE_MARK = "quota-probe.last"


def _launch_detached(argv, env, timeout, cwd=None):
    """The default probe runner: start the request and return at once. The gate
    holds on this poll; the next poll reads the record the proxy has refreshed."""
    subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def _claim_probe_marker(mark: Path, at: float, fresh_sec: float) -> bool:
    """Take the marker under the shared file lock: two notifiers that both find it
    stale serialize here, and only the first one out of the lock sends a probe.
    The lock is a sibling file, because locking creates its file and the marker's
    own mtime is the record being judged."""
    from file_lock import locked_file
    with locked_file(mark.with_name(mark.name + ".lock")):
        try:
            if at - mark.stat().st_mtime < fresh_sec:
                return False
        except OSError:
            pass
        mark.write_text(str(at), encoding="utf-8")
        os.utime(mark, (at, at))
        return True


def probe_refreshes_record(workspace, base_url: Optional[str], now: Optional[float] = None,
                           fresh_sec: float = FRESH_SEC, runner: Optional[Callable] = None,
                           timeout: float = PROBE_TIMEOUT_S, cwd: Optional[str] = None,
                           config_dir: Optional[str] = None) -> bool:
    """Send one minimal request THROUGH the proxy so it rewrites the record, and
    say whether a probe was sent. The verdict is then read from the record, never
    from the probe's outcome: a request the limit refuses still refreshes the
    headers, and that refusal is the answer. No `--model`, and run in the seat's
    own cwd and config dir: seats launch without a flag, so what a bare `claude`
    resolves THERE is the seat's model, and a model-scoped limit on it shows in
    the record this refreshes. Only toward
    a proxy address, at most once per `fresh_sec` host-wide, and the marker is
    claimed under a lock before the request goes out. Login state is the caller's."""
    if not points_at_credential_proxy(base_url):
        return False
    mark = Path(workspace) / "state" / PROBE_MARK
    at = time.time() if now is None else now
    try:
        if not _claim_probe_marker(mark, at, fresh_sec):
            return False
    except OSError:
        return False
    run = runner or _launch_detached
    env = dict(os.environ, ANTHROPIC_BASE_URL=str(base_url))
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    try:
        run(["claude", "-p", "ok"], env=env, timeout=timeout, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        pass
    return True

def provider_allows_now(workspace, socket_path: Optional[str], session: Optional[str],
                        now: Optional[float] = None, fresh_sec: float = FRESH_SEC,
                        tmux_runner: Optional[Callable] = None,
                        ps_runner: Optional[Callable] = None,
                        probe: bool = False, probe_runner: Optional[Callable] = None) -> bool:
    """True only when a FRESH record says accepted on EVERY window AND the named
    seat is routed through the proxy. Absent, stale, rejected anywhere, an
    unreadable record, an unobserved or unrouted seat all answer False: silence
    never overrides a limit banner.

    With `probe`, a record that cannot vouch for a ROUTED seat -- absent, stale, or
    last refreshed on another model -- is refreshed first by one request through the
    proxy on the seat's own model, then read again; the probe's outcome is never the
    verdict."""
    if not session:
        return False
    rec = read_quota_record(workspace, now)
    if rec is None and not probe:
        return False
    # An unobserved seat carries no base URL, so the decision reads it as unrouted.
    env = seat_env_base_url(socket_path, session, tmux_runner, ps_runner)
    # Absent, stale, or refreshed on another model: a record kept fresh by
    # another seat's traffic would otherwise hold this seat forever.
    cannot_vouch = (rec is None or not rec.fresh(fresh_sec)
                    or not record_speaks_for_seat(rec.payload, env, workspace))
    if probe and cannot_vouch and points_at_credential_proxy(env.base_url):
        if probe_refreshes_record(workspace, env.base_url, now, fresh_sec, runner=probe_runner,
                                  cwd=env.cwd, config_dir=env.config_dir):
            rec = read_quota_record(workspace, now)
    if rec is None:
        return False
    decision = availability_decision(rec.payload, base_url=env.base_url,
                                     stale=not rec.fresh(fresh_sec))
    return (bool(decision["available"]) and gate_windows_allowed(rec.payload)
            and record_speaks_for_seat(rec.payload, env, workspace))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(prog="quota_availability")
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--socket", default=None)
    ap.add_argument("--session", default="sutando-core")
    ap.add_argument("--probe", action="store_true", help="refresh a stale record through the proxy first")
    a = ap.parse_args()
    ws = a.workspace
    if ws is None:
        from workspace_default import resolve_workspace
        ws = resolve_workspace()
    rec = read_quota_record(ws)
    env = seat_env_base_url(a.socket, a.session)
    if rec is None:
        print("quota-availability: record absent or unreadable")
        raise SystemExit(2)
    d = availability_decision(rec.payload, base_url=env.base_url, stale=not rec.fresh())
    age = "unknown age" if rec.age_s is None else f"{int(rec.age_s)}s old"
    print(f"quota-availability: status={d['status']} {age} fresh={rec.fresh()} "
          f"seat={'unobserved' if not env.observed else (env.base_url or '<no base url>')} "
          f"routed={d['routed']} -> {'available' if d['available'] else d['unavailable_reason']}")
    raise SystemExit(0 if provider_allows_now(ws, a.socket, a.session, probe=a.probe) else 1)
