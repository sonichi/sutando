#!/usr/bin/env python3
"""runtime-health.py — derive this Sutando core's live health as one JSON object.

The machine-readable "is my agent working, idle, stuck-at-login, or offline?"
signal. The desktop app's Console renders it as a plain-English status strip +
one-click action cards (regular users) instead of making them read a raw
terminal; `sutando-whoami` can embed it too. Owner-designed 2026-07-13 — the
"when she's not responding, I can't tell if she's thinking or stuck" painpoint,
made concrete when a core sat unresponsive at claude's `/login` (locked keychain).

    python3 src/runtime-health.py           # prints JSON; also writes state/runtime-health.json

Output (single JSON object on stdout):
    health           working | idle | needs_login | offline | unknown
    authenticated    bool | null  (false when the core is sitting at claude's login prompt;
                     null when we can't tell, e.g. the core is offline)
    core_running     bool   (a `sutando-core` tmux session exists on the socket)
    gateway_running  bool   (the relay gateway bridge process is up)
    tmux_socket      the SUTANDO_TMUX_SOCKET this probed (private-socket aware)
    session          the tmux session name
    detail           short human string for the status strip

Design: every probe is best-effort and degrades — a missing tmux or an
unreadable status file yields `unknown`, never a crash. This is a read-only
observer; it starts nothing and kills nothing.
"""
import json
import math
import os
import tempfile
import socket
import subprocess
import sys
import time

SESSION = "sutando-core"
TMUX_SOCKET = os.environ.get("SUTANDO_TMUX_SOCKET", "/tmp/sutando-tmux.sock")

# A `core-status.json` claiming "running" is only trustworthy if its `ts` is
# recent — a crashed/wedged loop can leave it stuck on "running" indefinitely.
# Beyond this window a "running" record degrades to "unknown" rather than
# falsely reporting "working" (the exact incident this signal exists to catch).
# Aligned with the freshness gates other readers already apply (web-client 60s,
# core-heartbeat ~90s); 90s tolerates a normal long step between status writes
# while still catching a genuinely wedged loop (stale for far longer).
STALE_STATUS_SECONDS = 90

# The per-host core heartbeat (state/cores/<host>.alive) is rewritten every ~30s
# by a SEPARATE process (src/core_heartbeat.py); >90s means it stopped beating.
# Matches the documented staleness threshold every other reader of that file uses.
HEARTBEAT_STALE_SECONDS = 90

# ── Severity layer (design: docs/design-core-health-verdict.md) ──────────────
# One authoritative, severity-tagged verdict every consumer reads, so "report
# vs restart" is decided in ONE place instead of re-derived per consumer. This
# is additive: derive()'s existing `health`/`detail` keys are unchanged; the
# severity + signals + the gate are layered on top.
#
#   ok        working | idle           → no action
#   escalate  needs_login              → tell the human; NEVER auto-restart
#   critical  unknown(wedged) | offline→ restart, but only through severity_gate
#   warn      (reserved: 'degraded' soft-warnings folded in a later slice)
_SEVERITY = {
    "working": "ok",
    "idle": "ok",
    "degraded": "warn",
    "needs_login": "escalate",
    "unknown": "critical",   # status-stale wedge
    "offline": "critical",   # process/session gone
}


def severity_of(health):
    """Map a derive() health state to its severity bucket. Unknown states are
    treated as `critical` (fail toward noticing, not toward silence)."""
    return _SEVERITY.get(health, "critical")


def _host_label_safe():
    try:
        from util_paths import _host_label
        return _host_label()
    except Exception:
        try:
            return socket.gethostname().split(".")[0]
        except OSError:
            return None


def _heartbeat_fresh(workspace):
    """Independent liveness signal: the per-host core heartbeat
    `<ws>/state/cores/<host>.alive`, rewritten every ~30s by a SEPARATE process
    (src/core_heartbeat.py). Returns:
        True   beating (mtime within HEARTBEAT_STALE_SECONDS) — core is alive
        False  missing or stale — core stopped beating (dead)
        None   host/path can't be resolved — can't tell (never a down-vote)

    This is the process-INDEPENDENT corroborator the gate needs (qingyun CR on
    #2527): a genuinely offline core stops beating (process=False AND
    heartbeat=False → 2 votes → act), while a single mis-probe (e.g. bad PATH
    making the pgrep read False on a live core) still beats (heartbeat fresh →
    1 vote → report), so a lingering gateway can no longer make a dead core
    unrecoverable, and a mis-probe still can't kill a live one.

    The orphan-writer risk (a dead core leaving a fresh heartbeat because the
    standalone core_heartbeat.py sidecar kept running) is resolved at the SOURCE:
    core_heartbeat.py now binds to the core's tmux session and stops beating
    (unlinking .alive) once the session it saw has gone away — so a fresh .alive
    genuinely means the core lived within the window (qingyun CR on #2527, 2nd
    P1)."""
    host = _host_label_safe()
    if not host:
        return None
    try:
        p = os.path.join(workspace, "state", "cores", f"{host}.alive")
        return (time.time() - os.path.getmtime(p)) <= HEARTBEAT_STALE_SECONDS
    except OSError:
        return False  # missing/unreadable .alive == not beating


def severity_gate(verdict, *, confirm_min=2, freshly_booted=False):
    """The ONE place that turns a verdict into an action. Returns one of:

        none      severity ok — nothing to do
        report    a soft (warn) issue — surface it, do not touch the core
        escalate  a human-only blocker (needs_login) — notify the human, NEVER
                  auto-restart (no seed/restart can clear a real /login)
        act       restart is warranted — ONLY for a `critical` verdict that has
                  (a) persisted >= confirm_min cycles, (b) >= 2 independent live
                  signals agreeing it is down, and (c) is not freshly booted.
                  Anything short of all three downgrades to `report` so a merely
                  slow / idle / mis-probed (but alive) core is never killed.

    This is the structural form of the owner's rule (2026-08-02): report health
    issues, restart only when critical AND confirmed."""
    sev = verdict.get("severity") or severity_of(verdict.get("health", ""))
    if sev == "ok":
        return "none"
    if sev == "warn":
        return "report"
    if sev == "escalate":
        return "escalate"
    # critical
    confirm = int(verdict.get("confirm") or 0)
    signals = verdict.get("signals") or {}
    # Corroboration: count the independent signals that positively indicate the
    # core is NOT usefully alive. A single wrong probe (bad PATH, idle→unknown)
    # must not be enough on its own — generalizes #2404's compound gate.
    # `heartbeat_fresh` is process-independent (a separate writer), so a truly
    # offline core (process=False AND heartbeat=False) clears the >=2 bar while a
    # lone mis-probe on a still-beating core does not (qingyun CR on #2527).
    down_votes = sum(1 for k in ("process", "status_fresh", "gateway", "heartbeat_fresh")
                     if signals.get(k) is False)
    if freshly_booted:
        return "report"
    if confirm >= confirm_min and down_votes >= 2:
        return "act"
    return "report"


# Markers that mean the bundled claude CLI is sitting at its auth prompt and the
# core therefore cannot act. Kept broad on purpose — the failure mode is a user
# staring at an unresponsive agent, so a false "needs_login" (rare) is far less
# costly than missing a real one.
_LOGIN_MARKERS = (
    "not logged in",
    "please run /login",
    "run `claude login`",
    "run 'claude login'",
    "unlock-keychain",
    "invalid api key",
    "authentication_error",
)


def _run(cmd):
    """Run a command, returning (rc, stdout). Never raises.

    rc is None when the command could not be EXECUTED at all (binary missing,
    broken PATH, timeout) — distinct from a command that ran and returned
    non-zero. Callers must treat None as "unknown", never as a positive
    negative observation (qingyun CR on #2527): collapsing an unexecutable probe
    to a False "down" reading let a correlated probe outage masquerade as a dead
    core."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return None, ""


def _core_running():
    # rc None = the probe itself could not run (tmux absent / PATH broken) → that
    # is UNKNOWN, not "not running". A real has-session miss returns rc 1, which
    # stays False. (qingyun CR on #2527: probe-unavailable must not be a down-vote.)
    rc, _ = _run(["tmux", "-S", TMUX_SOCKET, "has-session", "-t", SESSION])
    if rc is None:
        return None
    return rc == 0


def _gateway_configured():
    """Whether the ag2.space mobile gateway is provisioned on THIS host.

    The gateway bridge only runs where a remote task token is configured (env or
    channels/ag2space/.env). Returns True (configured), False (config readable,
    no token), or None (can't tell — no CLAUDE_CONFIG_DIR / unreadable .env).
    Mirrors health-check.check_gateway_bridge's detection."""
    if os.environ.get("REMOTE_TASK_TOKEN") or os.environ.get("AG2_REMOTE_TOKEN"):
        return True
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if not cfg:
        return None
    try:
        with open(os.path.join(cfg, "channels", "ag2space", ".env")) as f:
            for ln in f:
                if ln.startswith(("REMOTE_TASK_TOKEN=", "AG2_REMOTE_TOKEN=")):
                    return True
    except OSError:
        return None
    return False


def _gateway_running():
    # A host with no gateway provisioned has `remote-gateway-bridge` CORRECTLY
    # absent — that is not-applicable, never a down-vote. Only a *configured*
    # gateway that is missing counts as down, or the gate would restart a
    # perfectly live core just for lacking an optional component (bassil CR on
    # #2527, mirroring health-check.check_gateway_bridge + the #2554 comm-sweep
    # single-owner-lane fix). Not-configured / can't-tell -> None.
    if _gateway_configured() is not True:
        return None
    rc, _ = _run(["pgrep", "-f", "remote-gateway-bridge"])
    if rc == 0:
        return True
    # Fallback: a window named "gateway" in the core session.
    rc2, out = _run(["tmux", "-S", TMUX_SOCKET, "list-windows", "-t", SESSION, "-F", "#{window_name}"])
    if rc2 == 0 and any(w.strip() == "gateway" for w in out.splitlines()):
        return True
    # Neither probe confirmed the gateway. Only report "down" if at least one
    # probe actually RAN; if BOTH were unavailable we cannot tell (None), so it is
    # never counted as a down-vote (qingyun CR on #2527).
    if rc is None and rc2 is None:
        return None
    return False



# The AG2 Space desktop app runs its own engine runtime under
# `space.ag2.app/engine`, and that engine is what serves the Station connector
# gateway (sutando.ag2.space). Station connectors are therefore available only
# while AG2 Space is running; when it is not, Station calls fail with a bare
# ENOTFOUND. Surfacing this lets any reader (health-check, dashboard, the agent)
# attribute a Station failure to "AG2 Space not running" rather than guess.
_AG2SPACE_APP_MARKER = "AG2 Space.app/Contents/MacOS"
_STATION_GATEWAY_HOST = "sutando.ag2.space"


def _ag2space_app_running():
    """Tri-state: is the AG2 Space desktop app (its UI binary) running?

    Matches ONLY the app bundle's MacOS executable, NOT the broad
    `space.ag2.app/engine` tree (which every bundled Sutando process shares —
    credential-proxy, web-client, voice-agent, the core tmux, etc.; qingyun CR
    on #2680). This is a narrow "the app is open" signal and does NOT by itself
    imply the Station gateway is reachable — see _station_available. rc None
    (pgrep unexecutable) → None (unknown), never a False down-vote.
    """
    rc, _ = _run(["pgrep", "-f", _AG2SPACE_APP_MARKER])
    if rc is None:
        return None
    return rc == 0


_STATION_CONNECT_TIMEOUT = 1.0   # hard per-connect bound (seconds)


def _probe_station(timeout=_STATION_CONNECT_TIMEOUT):
    """BLOCKING, BOUNDED tri-state reachability probe of the Station gateway.

    One getaddrinfo plus a SINGLE connect to the first resolved address, bounded
    by `timeout`. Returns True when that address accepts a TLS-port TCP
    connection, False when it resolves but the connect is refused/times out, and
    None on a DNS/resolver or socket error — UNKNOWN rather than a positive
    "unavailable" (same tri-state discipline as _run). Bounded to one connect
    (the earlier version tried every resolved address sequentially, up to
    N×timeout). Must only be reached via the file-cached _station_available().
    """
    try:
        addrs = socket.getaddrinfo(_STATION_GATEWAY_HOST, 443, type=socket.SOCK_STREAM)
    except OSError:
        return None
    if not addrs:
        return None
    family, socktype, proto, _canon, sockaddr = addrs[0]
    s = socket.socket(family, socktype, proto)
    s.settimeout(timeout)
    try:
        s.connect(sockaddr)
        return True
    except (socket.timeout, OSError):
        return False
    finally:
        s.close()


# Station reachability is FILE-cached, and the network probe is kept ENTIRELY off
# derive()'s hot path (qingyun CR #2680). derive() runs every ~3s in
# core-input-watch's IN-PROCESS liveness loop, so it must NEVER touch the network:
# _station_cached() only READS the on-disk verdict and returns instantly (a hung
# resolver can't stall the supervisor tick). The probe runs only from the one-shot
# `runtime-health.py main()` (`sutando-config.sh runtime`), via _refresh_station(),
# which bounds the whole DNS+connect in a KILLABLE subprocess with a hard deadline
# — a real cancellable boundary (socket.settimeout doesn't bound getaddrinfo, so a
# subprocess we can kill is the only way to cap a hung resolver). No threads => no
# worker accumulation. Wall-clock time is used because the cache is shared across
# processes (monotonic clocks have per-process origins).
_STATION_TTL = 60.0               # a refresh older than this is stale
_STATION_ATTEMPT_COOLDOWN = 15.0  # after an attempt, don't re-probe this soon
_STATION_PROBE_DEADLINE = 3.0     # hard end-to-end cap on the killable probe
_STATION_CACHE_NAME = "station-available.json"


def _station_cache_file(workspace):
    return os.path.join(workspace, "state", _STATION_CACHE_NAME)


def _read_station_cache(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_station_cache(path, data):
    # Shared state on the derive() hot path, so it takes the same unique-staging
    # contract; best-effort stays the caller's policy, not the writer's.
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_json_atomic(path, data)
    except OSError:
        pass


def _write_json_atomic(path, obj):
    """`open(path, "w")` truncates before it writes, so a reader polling in that
    window sees an empty or partial file. Swap a fully-written temp file in.

    The staging name must be UNIQUE per writer: a fixed `<path>.tmp` is itself
    shared state, and this module is an on-demand one-shot, so two callers can
    truncate the same staging inode, write through separate descriptors and race
    os.replace() -- publishing interleaved bytes, or raising ENOENT once the
    shared path has already moved."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _cache_ts(x):
    """A cache timestamp as a FINITE number, or None if missing/malformed. The
    cache is a mutable workspace state file — it can be corrupted, synced, or
    hand-edited — so NEVER do arithmetic on a value straight out of it (qingyun
    CR #2680: a `"bad"` value_ts raised TypeError in derive(), which
    core-input-watch calls unguarded every ~3s → killed the supervisor). bool is
    an int subclass in Python, so exclude it explicitly. NaN/±inf are floats but
    poison every comparison — `(now - NaN) >= ttl` is always False, so a
    `value_ts: NaN` would read as permanently FRESH and freeze the verdict
    forever (qingyun CR #2680 round 2) — so require math.isfinite too."""
    return (x if isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x) else None)


def _cache_verdict(v):
    """A tri-state verdict (True/False/None); anything else reads as None. Use
    IDENTITY, not `in (True, False, None)`: `1 == True` and `0 == False` in
    Python, so membership would let an integer `1`/`0` masquerade as the bool
    verdict and be returned unchanged, violating the bool|null field contract
    (qingyun CR #2680 round 2)."""
    return v if (v is True or v is False or v is None) else None


def _fresh_age(now, value_ts, ttl):
    """`now - value_ts` when the timestamp is plausibly fresh, else None.

    "Fresh" = within `ttl` AND not implausibly in the future. The cache is synced
    across hosts with independent clocks, so a corrupt/skewed record can carry a
    far-future `value_ts`; a naive `(now - value_ts) < ttl` reads that as fresh
    (age is very negative) and freezes the verdict indefinitely (qingyun CR #2680:
    "revive an indefinitely stale availability verdict"). A small negative age is
    tolerated as benign clock skew; anything more than `ttl` in the future is
    treated as not-fresh (unknown)."""
    age = now - value_ts
    if age >= ttl or age < -ttl:
        return None
    return age


def _station_cached(workspace, *, now=None, ttl=_STATION_TTL):
    """READ-ONLY tri-state Station verdict from the on-disk cache — NEVER probes,
    so it is safe on derive()'s ~3s supervisor loop (always returns instantly).

    Returns the persisted value only while it is FRESH (younger than `ttl`); an
    expired, absent, OR malformed verdict reads as None (unknown) rather than a
    stale confident True/False or a raised exception. Without the freshness check
    a last-known `True` would keep reporting `station_available: true` forever
    after Station went down; without the schema guard a corrupt cache record
    would crash core-input-watch (qingyun CR #2680). The value is refreshed
    off-loop by the one-shot main() via _refresh_station."""
    cache = _read_station_cache(_station_cache_file(workspace))
    value_ts = _cache_ts(cache.get("value_ts"))
    if value_ts is None:
        return None  # missing or malformed timestamp -> unknown
    t = now if now is not None else time.time()
    if _fresh_age(t, value_ts, ttl) is None:
        return None  # expired OR implausibly-future -> unknown, not a stale verdict
    return _cache_verdict(cache.get("value"))


def _probe_station_bounded(connect_timeout=_STATION_CONNECT_TIMEOUT,
                           deadline=_STATION_PROBE_DEADLINE, argv=None):
    """Run the blocking probe inside a KILLABLE subprocess with a hard end-to-end
    `deadline`. Because socket.settimeout does not bound getaddrinfo, this is the
    only way to cap a hung resolver: on timeout the child is killed and the result
    is None (unknown). Child prints 'true'/'false'/'' (see the --probe-station
    entrypoint). `argv` is injectable for tests."""
    argv = argv or [sys.executable, os.path.abspath(__file__),
                    "--probe-station", str(connect_timeout)]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=deadline)
    except (subprocess.TimeoutExpired, OSError):
        return None  # hung/failed resolver — killed at the deadline, UNKNOWN
    out = (proc.stdout or "").strip()
    return {"true": True, "false": False}.get(out, None)


def _refresh_station(workspace, *, now=None, ttl=_STATION_TTL,
                     deadline=_STATION_PROBE_DEADLINE,
                     cooldown=_STATION_ATTEMPT_COOLDOWN, probe=None):
    """Probe the gateway (bounded, cancellable) and persist the verdict. Called
    ONLY from the one-shot main() — never from derive()/the 3s loop. Honors the
    TTL (a still-fresh verdict is not re-probed) and an attempt cooldown (a hung
    resolver is not re-entered by a separate one-shot caller). `probe`/`now`
    injectable."""
    now = now if now is not None else time.time()
    path = _station_cache_file(workspace)
    cache = _read_station_cache(path)
    value_ts = _cache_ts(cache.get("value_ts"))
    if value_ts is not None and _fresh_age(now, value_ts, ttl) is not None:
        return _cache_verdict(cache.get("value"))  # still FRESH — no re-probe
    attempt_ts = _cache_ts(cache.get("attempt_ts"))
    if attempt_ts is not None and _fresh_age(now, attempt_ts, cooldown) is not None:
        return _cache_verdict(cache.get("value"))  # a recent attempt in flight
    _write_station_cache(path, {**cache, "attempt_ts": now})
    runner = probe or (lambda: _probe_station_bounded(deadline=deadline))
    try:
        result = runner()
    except Exception:
        result = None  # best-effort: never a spurious False
    _write_station_cache(path, {"value": result, "value_ts": time.time(), "attempt_ts": now})
    return result


def _pane_text():
    rc, out = _run(["tmux", "-S", TMUX_SOCKET, "capture-pane", "-p", "-t", SESSION])
    return out if rc == 0 else ""


def needs_login(pane_text):
    """Pure predicate: does the core pane show claude's auth prompt? Testable
    without a live tmux — this is the load-bearing 'stuck vs thinking' decision."""
    low = pane_text.lower()
    return any(m in low for m in _LOGIN_MARKERS)


def _core_status(workspace):
    """Read the agent's own status ('running'|'idle') from core-status.json.

    This is a shared state file written by other processes, so treat it as
    untrusted: a missing/corrupt file (OSError/ValueError) OR a valid-but-non-object
    JSON value (e.g. a stray `[]` — `.get` would AttributeError) degrades to None,
    never a crash — keeping the script's "unknown, not exception" contract.
    """
    try:
        with open(os.path.join(workspace, "state", "core-status.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    ts = data.get("ts")
    try:
        ts = float(ts) if ts is not None else None
    except (TypeError, ValueError):
        ts = None
    return data.get("status"), ts


def _resolve_workspace(repo):
    rc, out = _run(["bash", os.path.join(repo, "scripts", "sutando-config.sh"), "workspace"])
    return out.strip() if rc == 0 and out.strip() else os.path.join(repo, "workspace")


def derive():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    workspace = _resolve_workspace(repo)

    core = _core_running()
    gateway = _gateway_running()
    ag2space_app = _ag2space_app_running()
    station = _station_cached(workspace)  # READ-ONLY: never probes on the 3s loop

    # Raw signals, captured for the audited verdict. Defaults hold for the
    # offline / unknown branches (core gone or unprobeable → status/login
    # unknowable).
    status_fresh = None
    pane_login = False

    if core is None:
        # The process probe could not run (tmux/pgrep unavailable, broken PATH).
        # This is NOT evidence the core is down — treat it as unknown and fail
        # closed: no signal here is set False, so the gate sees zero down-votes
        # from a correlated probe outage and can only report, never act
        # (qingyun CR on #2527). Genuine down-signals from probes that DID run
        # (e.g. a real gateway miss + stale heartbeat) still count normally.
        health, authed, detail = "unknown", None, "Core liveness unknown (process probe unavailable)"
    elif not core:
        health, authed, detail = "offline", None, "Agent is not running"
    else:
        # Read the status FIRST. The login probe used to short-circuit ahead of
        # this, so a false marker did not merely add noise — it REPLACED the
        # wedged-core verdict, hiding the one signal that catches an
        # unresponsive agent. That inverts the rationale stated above for
        # tolerating false positives (#2456).
        status, ts = _core_status(workspace)
        stale = ts is not None and (time.time() - ts) > STALE_STATUS_SECONDS
        # "Acting" needs POSITIVE evidence, not merely the absence of proof to
        # the contrary. A missing `ts` cannot show freshness any more than it can
        # show staleness (see the "no ts -> working (can't prove stale)" case),
        # so it must NOT license overriding a login marker — that would be the
        # same absence-of-evidence mistake in the other direction.
        acting = status in ("running", "idle") and ts is not None and not stale
        login = needs_login(_pane_text())
        # status_fresh: True = advanced within the window, False = stale,
        # None = no record to judge (can't prove either way).
        status_fresh = None if ts is None else (not stale)
        pane_login = login

        if login and not acting:
            # Marker AND no evidence of progress. A genuine sign-in prompt stops
            # the loop, so a stale/unknown status is what a real one looks like —
            # the two corroborate. Keep the staleness in the text so the wedge
            # signal survives alongside the louder verdict rather than being
            # erased by it.
            health, authed = "needs_login", False
            detail = "Agent needs to sign in"
            if status == "running" and stale:
                detail += " (status also stale — if the pane is clean, treat as possibly wedged)"
        elif login and acting:
            # The status says the agent advanced within the freshness window, so
            # it is demonstrably acting. A sign-in prompt cannot be true at the
            # same time; the marker is stale pane text or an unrelated log line.
            # Reporting "needs to sign in" for a working agent is simply wrong,
            # and the false-positive-is-cheap argument does not reach here — it
            # was about an UNRESPONSIVE agent.
            authed = True
            health = "working" if status == "running" else "idle"
            detail = ("Agent is working" if status == "running" else "Agent is online and idle")
            detail += " (login marker seen in pane but status is fresh — treating as a false positive)"
        else:
            authed = True
            if status == "running" and not stale:
                health, detail = "working", "Agent is working"
            elif status == "running" and stale:
                # Session alive but status hasn't advanced — likely a wedged/
                # crashed loop; don't claim "working" off a stale record.
                health, detail = "unknown", "Status stale (still 'running', not updated recently) — possibly wedged"
            elif status == "idle":
                health, detail = "idle", "Agent is online and idle"
            else:
                health, detail = "unknown", "Agent is running (status unknown)"

    return {
        "health": health,
        "severity": severity_of(health),
        "authenticated": authed,
        "core_running": core,
        "gateway_running": gateway,
        "ag2space_app_running": ag2space_app,
        # Real reachability of the Station gateway (tri-state); None = unknown.
        "station_available": station,
        "tmux_socket": TMUX_SOCKET,
        "session": SESSION,
        "detail": detail,
        # Raw inputs behind the verdict, so a wrong call is auditable instead of
        # shipping as yet another "consumer disagreed" fix.
        "signals": {
            "process": core,
            "gateway": gateway,
            "status_fresh": status_fresh,
            "pane_login": pane_login,
            # Process-independent liveness (separate writer) — the offline
            # corroborator that a lingering gateway can't fake (#2527 CR).
            "heartbeat_fresh": _heartbeat_fresh(workspace),
        },
    }


def _confirm_count(state_dir, health, severity):
    """How many consecutive cycles this (health, severity) has held, read from
    the prior core-verdict.json. Powers the gate's persistence requirement so a
    one-cycle blip can never reach `act`. Best-effort: any read failure resets to
    a fresh count of 1 (fail toward re-confirming, not toward acting)."""
    try:
        with open(os.path.join(state_dir, "core-verdict.json")) as f:
            prev = json.load(f)
        # A malformed verdict (valid JSON but not an object — e.g. `[]`) must not
        # crash the count: `.get` on a list raises AttributeError, which the
        # OSError/ValueError guard below does NOT catch and would propagate out of
        # main() (bassil CR on #2527). Non-dict -> treat as no prior, reset to 1.
        if isinstance(prev, dict) and prev.get("health") == health and prev.get("severity") == severity:
            return int(prev.get("confirm") or 0) + 1
    except (OSError, ValueError):
        pass
    return 1


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ws = _resolve_workspace(repo)  # has its own repo/workspace fallback; never raises
    # Refresh the Station verdict OFF the supervisor loop: this one-shot process
    # (not core-input-watch's 3s derive() loop) is the only place the network
    # probe runs, and it's bounded in a killable subprocess so a hung resolver
    # can't freeze even this caller. derive() below then READS the fresh cache.
    try:
        _refresh_station(ws)
    except Exception:
        pass  # a refresh failure must never block the health read
    result = derive()
    # Best-effort persist so anything (app, dashboard) can read the latest without
    # re-probing; failure to write must not fail the read.
    try:
        state_dir = os.path.join(ws, "state")
        os.makedirs(state_dir, exist_ok=True)
        _write_json_atomic(os.path.join(state_dir, "runtime-health.json"), result)
        # The authoritative verdict (design: docs/design-core-health-verdict.md):
        # same facts as runtime-health.json plus the persistence count the gate
        # needs. Additive — consumers migrate to this file one PR at a time.
        verdict = dict(result)
        verdict["ts"] = int(time.time())
        verdict["confirm"] = _confirm_count(state_dir, result["health"], result["severity"])
        _write_json_atomic(os.path.join(state_dir, "core-verdict.json"), verdict)
    except OSError:
        pass
    print(json.dumps(result, indent=2))


if __name__ == "__main__":  # pragma: no cover - CLI dispatch; logic tested via _probe_station
    # `--probe-station <timeout>`: the child of _probe_station_bounded — runs the
    # blocking probe once and prints its tri-state ('true'/'false'/'' for None) so
    # the parent can bound it in a killable subprocess. Kept tiny and side-effect
    # free (no cache writes, no health output).
    if len(sys.argv) >= 2 and sys.argv[1] == "--probe-station":
        _t = float(sys.argv[2]) if len(sys.argv) > 2 else _STATION_CONNECT_TIMEOUT
        _r = _probe_station(_t)
        print("" if _r is None else ("true" if _r else "false"))
        sys.exit(0)
    main()
