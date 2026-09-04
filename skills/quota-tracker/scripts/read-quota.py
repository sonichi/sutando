#!/usr/bin/env python3
"""
Read Claude Code quota state from quota-state.json.

Usage:
  python3 read-quota.py              # human readable
  python3 read-quota.py --json       # machine readable
  python3 read-quota.py --gate       # exit 1 if exhausted, not routed, OR stale

Burn-rate tracking (closes #1087):
  On each human/json read, tracks per-5min utilization delta via an EWMA
  (alpha=0.3) stored in state/quota-burn-history.json. Outputs:
    Burn rate: X.X%/pass (N samples)
    Est. passes left: N (~Nm)
  Skips the sample if a 5h reset occurred (util dropped) or the gap is
  outside the 2min–2h window.
"""
from __future__ import annotations

# PEP 604 unions (`Path | None`) below are evaluated at import/def time, which
# raises TypeError on Python 3.9 (the system python3 on some hosts). Defer all
# annotation evaluation so the module imports cleanly on 3.9+.
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# Canonical (and only) home is <workspace>/state/quota-state.json, written by
# the credential proxy. The skill-dir / cwd fallbacks were removed: a stale
# leftover quota-state.json under skills/quota-tracker/ silently shadowed the
# fresh file and froze the dashboard for ~12h (2026-05-21). One path, one
# source of truth — if it's missing, say so rather than read a stale copy.
# NOTE: `.resolve()` follows the ~/.claude/skills symlink into the repo, so the
# path is <repo>/skills/quota-tracker/scripts/read-quota.py — four levels deep.
# Three .parent landed on <repo>/skills (no src/ there), so the workspace_default
# import silently failed (→ except below → "not found") and quota read as missing
# regardless of where the proxy wrote. Walk up four to reach <repo>/src.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from quota_availability import (  # noqa: E402
    PROXY_PORT as _PROXY_PORT,
    PROXY_SCHEME as _PROXY_SCHEME,
    availability_decision,
    points_at_credential_proxy,
    resolve_available as _resolve_available,
)
try:
    from workspace_default import status_read_path  # noqa: E402
    _canonical = status_read_path("quota-state.json")
    _burn_history_path = status_read_path("quota-burn-history.json")
except ImportError:
    _canonical = None
    _burn_history_path = None

if _canonical is not None and _canonical.exists():
    QUOTA_FILE = _canonical
else:
    print("No quota-state.json found. Is the credential proxy running?")
    sys.exit(1)

BURN_HISTORY_FILE: Path | None = _burn_history_path

# EWMA smoothing factor. 0.3 = ~3-sample half-life, responsive without noise.
_EWMA_ALPHA = 0.3
# Sample inclusion window: skip deltas from outside [MIN_GAP_S, MAX_GAP_S].
_MIN_GAP_S = 120     # 2 min — same-pass double-reads shouldn't count
_MAX_GAP_S = 7200    # 2 h — stale gap yields unreliable per-pass rate
# A window needs this many of its OWN samples before it can be forecast.
_MIN_SAMPLES = 2



# The credential proxy writes quota-state.json; 7846 is its port everywhere else
# in the tree (restart.sh, health-check.py, services_status.py).
def _redacted_endpoint(base_url: str) -> str:
    """scheme://host:port only — userinfo, path, query and fragment carry secrets.

    It reaches shared self-diagnose bundles, so it must never echo the raw value.
    """
    try:
        u = urlparse(base_url if "//" in base_url else "//" + base_url)
        host = (u.hostname or "").strip()
        port = f":{u.port}" if u.port else ""
    except ValueError:
        return "an unparseable endpoint"
    if not host:
        return "another endpoint"
    return f"{u.scheme}://{host}{port}" if u.scheme else f"{host}{port}"


def _points_at_credential_proxy(base_url: "str | None") -> bool:
    """Compatibility wrapper for tests and callers of the historic helper."""
    return points_at_credential_proxy(base_url)

def _load_burn_history() -> dict:
    if not BURN_HISTORY_FILE:
        return {}
    try:
        return json.loads(BURN_HISTORY_FILE.read_text())
    except Exception:
        return {}


def _save_burn_history(h: dict) -> None:
    if not BURN_HISTORY_FILE:
        return
    try:
        BURN_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BURN_HISTORY_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(h, indent=2))
        tmp.rename(BURN_HISTORY_FILE)
    except Exception:
        pass


def _advance_ewma(prev, per_pass: float, samples: int):
    """Fold one per-pass sample into an EWMA. Returns (ewma, samples)."""
    if prev is None:
        return per_pass, 1
    return _EWMA_ALPHA * per_pass + (1 - _EWMA_ALPHA) * prev, min(samples + 1, 99)


def _window_horizon(ewma, util, reset_epoch):
    """Passes until `util` reaches 1.0 at `ewma`/pass — or None if it never does.

    None means "this window does not bind": either there is no usable rate, or
    the forecast runs past the window's own reset, at which point it refills and
    the projection is void. A window cannot constrain you beyond its refill, so
    reporting a number larger than the time to that refill is not a conservative
    estimate — it is an unreachable one.
    """
    if not ewma or ewma <= 0:
        return None
    passes = ((1 - util) * 100) / (ewma * 100)
    if reset_epoch is not None:
        passes_until_reset = max(0.0, (reset_epoch - time.time()) / 300.0)
        if passes > passes_until_reset:
            return None
    return passes


def _update_burn_rate(current_util_5h: float, current_util_7d=None,
                      reset_5h_epoch=None, reset_7d_epoch=None) -> dict | None:
    """Update the burn-rate EWMAs and forecast the BINDING window.

    Both rolling limits are tracked, because either can be the one that stops
    the loop and they run out at different times. The 5h window refills every
    five hours; the 7d window can be days from its reset, so a 5h-only forecast
    reports headroom that the account does not actually have. Observed
    2026-08-05 on Chis-Mac-mini: 5h at 89% remaining and 7d at 27%, and the
    5h-only forecast printed 615 minutes left when the 5h window it was
    projecting refilled in 212 — a number 403 minutes past its own reset, while
    the window that could not refill for another four days went unmentioned.

    `estimated_passes_left` keeps its name and its meaning of "passes until the
    loop is stopped"; what changes is that it now considers every window that
    can stop it. `binding_window` names which one it came from, and is None when
    no window runs out before its own reset.
    """
    now = time.time()
    h = _load_burn_history()
    last_ts = h.get("last_read_ts")

    new_h = dict(h)
    new_h["last_read_ts"] = now
    new_h["last_util_5h"] = current_util_5h
    if current_util_7d is not None:
        new_h["last_util_7d"] = current_util_7d
    new_h["schema_version"] = 2

    ewma_5h = h.get("burn_rate_5h_ewma")
    ewma_7d = h.get("burn_rate_7d_ewma")
    samples_5h = h.get("burn_samples", 0)
    samples_7d = h.get("burn_samples_7d", 0)

    if last_ts is not None:
        gap = now - last_ts
        if _MIN_GAP_S <= gap <= _MAX_GAP_S:
            scale = 300.0 / gap
            # `burn_samples` keeps its pre-existing meaning — 5h samples — so a
            # v1 history file carries over without reinterpretation.
            last_5h = h.get("last_util_5h")
            if last_5h is not None and current_util_5h - last_5h >= 0:
                ewma_5h, samples_5h = _advance_ewma(
                    ewma_5h, (current_util_5h - last_5h) * scale, samples_5h)
                new_h["burn_rate_5h_ewma"] = ewma_5h
                new_h["burn_samples"] = samples_5h
            # 7d is folded on its OWN counter. The windows reset independently:
            # a 5h reset zeroes that delta while 7d keeps climbing, so a shared
            # counter would suppress the 7d forecast for exactly the readings
            # taken across a 5h reset — the moments the 7d number matters most.
            last_7d = h.get("last_util_7d")
            if (current_util_7d is not None and last_7d is not None
                    and current_util_7d - last_7d >= 0):
                ewma_7d, samples_7d = _advance_ewma(
                    ewma_7d, (current_util_7d - last_7d) * scale, samples_7d)
                new_h["burn_rate_7d_ewma"] = ewma_7d
                new_h["burn_samples_7d"] = samples_7d

    _save_burn_history(new_h)

    # A window is EXPECTED whenever the caller supplied its utilization, and
    # FORECAST only once it has two samples of its own. Keeping those separate
    # is the whole point: every pre-existing v1 history already satisfies the 5h
    # gate and carries NO 7d counter, so for the first reads after an upgrade
    # the 7d window is expected-but-unforecast. Folding that into "no window
    # binds" prints an all-clear over a window nobody measured — the same
    # could-not-measure-reported-as-a-result this change exists to remove, one
    # layer up. Reproduced against a real v1 history with 7d at 95%: it returned
    # binding_window null and the human path said "no window runs out".
    # The same guard covers a 7d stream that never matures; omission is never
    # read as safety.
    expected = ["5h"] + (["7d"] if current_util_7d is not None else [])
    samples = {"5h": new_h.get("burn_samples", 0),
               "7d": new_h.get("burn_samples_7d", 0)}
    utils = {"5h": current_util_5h, "7d": current_util_7d}
    ewmas = {"5h": new_h.get("burn_rate_5h_ewma"),
             "7d": new_h.get("burn_rate_7d_ewma")}
    resets = {"5h": reset_5h_epoch, "7d": reset_7d_epoch}

    horizons, unforecast = {}, []
    for w in expected:
        if samples[w] >= _MIN_SAMPLES:
            horizons[w] = _window_horizon(ewmas[w], utils[w], resets[w])
        else:
            unforecast.append(w)
    if not horizons:
        return None

    binding = min((w for w in horizons if horizons[w] is not None),
                  key=lambda w: horizons[w], default=None)

    result = {
        "burn_rate_pct_per_pass": round((new_h.get("burn_rate_5h_ewma") or 0) * 100, 2),
        "burn_samples": new_h.get("burn_samples", 0),
        "binding_window": binding,
        "estimated_passes_left": round(horizons[binding], 1) if binding else None,
        "estimated_minutes_left": round(horizons[binding] * 5) if binding else None,
        # Non-empty means the verdict is INCOMPLETE, not clear. A consumer that
        # reads `binding_window: null` on its own cannot tell those apart.
        "unforecast_windows": unforecast,
    }
    if new_h.get("burn_rate_7d_ewma"):
        result["burn_rate_7d_pct_per_pass"] = round(new_h["burn_rate_7d_ewma"] * 100, 2)
    return result


def resolve_available(status: str, proxy_available) -> bool:
    """Compatibility wrapper around the shared availability policy owner."""
    return _resolve_available(status, proxy_available)


def main():
    data = json.loads(QUOTA_FILE.read_text())
    headers = data.get("headers", {})

    # Staleness guard: the proxy rewrites this file on every API response, so
    # an old mtime means quota data is NOT current (proxy dead, or the session
    # isn't routed through it). Report it loudly — a confident 4-day-old
    # reading once drove a full day of budget decisions (2026-07-17).
    age_s = time.time() - QUOTA_FILE.stat().st_mtime
    stale = age_s > 30 * 60

    # Presence is not destination: the launcher honours a caller-set URL verbatim,
    # so only the proxy's own host:port proves these numbers describe this session.
    status = headers.get("anthropic-ratelimit-unified-status", "unknown")
    util_5h = float(headers.get("anthropic-ratelimit-unified-5h-utilization", 0))
    util_7d = float(headers.get("anthropic-ratelimit-unified-7d-utilization", 0))
    reset_5h = headers.get("anthropic-ratelimit-unified-5h-reset", "")
    reset_7d = headers.get("anthropic-ratelimit-unified-7d-reset", "")
    # The API also meters a top-tier-model weekly lane (`7d_oi`); a pinned
    # Opus/Fable core can exhaust it while the all-model windows read fine.
    util_7d_oi_raw = headers.get("anthropic-ratelimit-unified-7d_oi-utilization")
    util_7d_oi = float(util_7d_oi_raw) if util_7d_oi_raw is not None else None
    reset_7d_oi = headers.get("anthropic-ratelimit-unified-7d_oi-reset", "")

    # Stated once: a second copy of this predicate drifts the moment either is
    # extended, and the two fields then contradict each other in one payload.
    # Fails closed on unrouted or stale: a routed proxy that stopped writing keeps
    # `routed` true while every number is a fossil.
    decision = availability_decision(
        data,
        base_url=os.environ.get("ANTHROPIC_BASE_URL"),
        stale=stale,
    )
    routed = decision["routed"]
    available = decision["available"]

    result = {
        "status": status,
        # Fails closed when unrouted OR stale: --gate exits on this field, and a
        # machine consumer never sees the human NOT ROUTED / STALE banner.
        "available": available,
        "utilization_5h": util_5h,
        "utilization_7d": util_7d,
        "remaining_5h_pct": round((1 - util_5h) * 100),
        "remaining_7d_pct": round((1 - util_7d) * 100),
        "state_age_seconds": int(age_s),
        "stale": stale,
        "routed": routed,
        # Three unavailable states, three different remedies; not-routed outranks
        # stale because a foreign file's age says nothing about this session.
        "unavailable_reason": decision["unavailable_reason"],
    }

    if reset_5h:
        result["reset_5h"] = datetime.fromtimestamp(int(reset_5h)).isoformat()
    if reset_7d:
        result["reset_7d"] = datetime.fromtimestamp(int(reset_7d)).isoformat()
    if util_7d_oi is not None:
        result["utilization_7d_oi"] = util_7d_oi
        result["remaining_7d_oi_pct"] = round((1 - util_7d_oi) * 100)
        if reset_7d_oi:
            result["reset_7d_oi"] = datetime.fromtimestamp(int(reset_7d_oi)).isoformat()

    # Unrouted: the utilization delta is another session's, and _update_burn_rate
    # ends in _save_burn_history — a foreign sample outlives the banner flagging it.
    if "--gate" not in sys.argv and routed:
        burn = _update_burn_rate(
            util_5h, util_7d,
            int(reset_5h) if reset_5h else None,
            int(reset_7d) if reset_7d else None,
        )
        if burn:
            result["burn"] = burn

    if "--json" in sys.argv:
        print(json.dumps(result, indent=2))
        return

    if "--gate" in sys.argv:
        sys.exit(0 if result["available"] else 1)

    # Human readable
    if not routed:
        hrs = age_s / 3600
        # Two different causes need two different remedies: no endpoint at all
        # versus an endpoint the caller deliberately pointed somewhere else.
        _url = os.environ.get("ANTHROPIC_BASE_URL")
        if _url:
            print(f"⛔ NOT ROUTED: ANTHROPIC_BASE_URL is {_redacted_endpoint(_url)}, not the "
                  f"local credential "
                  f"proxy ({_PROXY_SCHEME}://localhost:{_PROXY_PORT}).")
        else:
            print("⛔ NOT ROUTED: ANTHROPIC_BASE_URL is unset, so THIS session does not go "
                  "through the proxy.")
        print(f"   The numbers below are another session's, {hrs:.1f}h old. They are NOT "
              "this session's budget —")
        if _url:
            print("   do not tier work off them. (Point it at the proxy, or clear it and "
                  "relaunch via the core launcher.)")
        else:
            print("   do not tier work off them. (The core launcher exports it only when the "
                  "proxy port already")
            print("   has a listener at launch — relaunch the proxy, then restart this core.)")
    elif stale:
        hrs = age_s / 3600
        print(f"⚠ STALE: quota state is {hrs:.1f}h old — proxy not feeding it; numbers below are historical, not current")
    print(f"Status: {status}")
    print(f"5h window: {int(util_5h * 100)}% used, {result['remaining_5h_pct']}% remaining")
    if reset_5h:
        print(f"  Resets: {datetime.fromtimestamp(int(reset_5h)).strftime('%H:%M %b %d')}")
    print(f"7d window: {int(util_7d * 100)}% used, {result['remaining_7d_pct']}% remaining")
    if reset_7d:
        print(f"  Resets: {datetime.fromtimestamp(int(reset_7d)).strftime('%H:%M %b %d')}")
    if util_7d_oi is not None:
        print(f"7d-oi window (top-tier models): {int(util_7d_oi * 100)}% used, "
              f"{result['remaining_7d_oi_pct']}% remaining")
        if reset_7d_oi:
            print(f"  Resets: {datetime.fromtimestamp(int(reset_7d_oi)).strftime('%H:%M %b %d')}")
    if not routed:
        print("Burn rate / passes-left: SUPPRESSED — computed from traffic that is not "
              "this session's.")
    elif result.get("burn"):
        b = result["burn"]
        print(f"Burn rate: {b['burn_rate_pct_per_pass']}%/pass ({b['burn_samples']} samples)")
        # An unforecast window taints BOTH outcomes, not just the empty one: a
        # binding number is only the minimum over the windows actually measured,
        # so printing it bare while another window is unmeasured asserts more
        # than was checked. Caveat first, verdict second.
        pending = b.get("unforecast_windows") or []
        caveat = (f" [INCOMPLETE: no history yet for {', '.join(pending)} — "
                  f"not forecast]") if pending else ""
        if b.get("binding_window"):
            print(f"Est. passes left: {b['estimated_passes_left']} "
                  f"(~{b['estimated_minutes_left']}m, {b['binding_window']} window binds)"
                  + caveat)
        elif pending:
            print(f"Est. passes left: INCOMPLETE — no history yet for "
                  f"{', '.join(pending)}; those windows are not forecast")
        else:
            print("Est. passes left: no window runs out before its own reset")


if __name__ == "__main__":
    main()
