#!/usr/bin/env python3
"""What the observed runtime implies about the declared roster — pure policy.

`roster.json` carries a worker `state`, every consumer honours it, and nothing
has ever written a transition into it: `live` is an assertion made once at
registration that no measurement can contradict (sonichi/sutando#4417). This
module is the missing half's decision logic, kept free of I/O so the rules are
testable without spawning a worker: callers read the files and pass dicts.

Three separations the callers must preserve:

  existence  the roster declares the worker          (desired)
  liveness   a fresh beat under a matching incarnation (observed)
  readiness  live AND not wedged on work it accepted   (derived)

`retired` is an owner decision and `abandoned` is an observation. Reconciliation
may write the second and must never write the first, or the desired/observed
confusion this module exists to fix simply reappears inside the state enum.
"""
from __future__ import annotations

# Mirrors pool_roster.STATES; imported there rather than redefined by callers.
LIVE = "live"
RECOVERING = "recovering"
ABANDONED = "abandoned"
RETIRED = "retired"

UNKNOWN = "unknown"

#: A beat older than this is no longer evidence of liveness.
DEFAULT_LIVE_S = 90
#: ...and past this, recovery is no longer plausible.
DEFAULT_ABANDON_S = 1800

MISSING = "POOL_WORKER_MISSING"
BINDING_UNAVAILABLE = "POOL_BINDING_TARGET_UNAVAILABLE"
UNEXPECTED = "POOL_WORKER_UNEXPECTED"
BEAT_UNREADABLE = "POOL_BEAT_UNREADABLE"
WEDGED = "POOL_WORKER_WEDGED"


def classify_beat(beat, now, *, live_s=DEFAULT_LIVE_S, abandon_s=DEFAULT_ABANDON_S):
    """A beat's own verdict: live / recovering / abandoned / unknown.

    `unknown` is a real answer, not a soft no. A worker that predates beats and
    a worker that died both present as "no beat", and treating that as death
    would abandon every worker on the release that introduces this file.
    """
    if beat is None:
        return UNKNOWN
    ts = beat.get("last_beat_at")
    if not isinstance(ts, (int, float)):
        return UNKNOWN
    age = now - ts
    if age < 0:
        # A beat from the future is a clock fault, not freshness.
        return UNKNOWN
    if age <= live_s:
        return LIVE
    if age <= abandon_s:
        return RECOVERING
    return ABANDONED


def derive_readiness(observed_state, accepted_ages_s, *, hard_timeout_s):
    """live is not ready: a worker wedged on work it accepted is still beating.

    `accepted_ages_s` is the age of each delivery this worker has accepted and
    not finished; one older than the hard timeout means it cannot take more.
    """
    if observed_state != LIVE:
        return False, None
    for age in accepted_ages_s or ():
        if isinstance(age, (int, float)) and age > hard_timeout_s:
            return False, WEDGED
    return True, None


def reconcile(desired, observed, now, *, bindings=None, live_s=DEFAULT_LIVE_S,
              abandon_s=DEFAULT_ABANDON_S):
    """Transitions the roster should take, and anomalies a human should see.

    Returns {"transitions": [...], "anomalies": [...]}. It never edits bindings:
    a pin outliving its worker is an anomaly to report, not a routing choice to
    silently rewrite.
    """
    desired = dict(desired or {})
    observed = dict(observed or {})
    bindings = dict(bindings or {})
    transitions, anomalies = [], []

    for wid, row in sorted(desired.items()):
        declared = (row or {}).get("state", LIVE)
        if declared == RETIRED:
            continue                      # owner intent; no observation overrides it
        beat = observed.get(wid)
        if beat is not None and not isinstance(beat, dict):
            anomalies.append({"code": BEAT_UNREADABLE, "worker_id": wid})
            continue
        seen = classify_beat(beat, now, live_s=live_s, abandon_s=abandon_s)
        if seen == UNKNOWN:
            # No usable beat: report, never transition. Absence of evidence here
            # is indistinguishable from a worker that never learned to beat.
            anomalies.append({"code": MISSING, "worker_id": wid,
                              "declared": declared, "observed": UNKNOWN})
            continue
        if seen != declared:
            transitions.append({"worker_id": wid, "from": declared, "to": seen})
        if seen in (RECOVERING, ABANDONED):
            anomalies.append({"code": MISSING, "worker_id": wid,
                              "declared": declared, "observed": seen})

    effective = {}
    for wid, row in desired.items():
        declared = (row or {}).get("state", LIVE)
        if declared == RETIRED:
            effective[wid] = RETIRED
            continue
        # UNKNOWN is not collapsed to the declared value: guessing "live" for an
        # unverifiable target restores the silence this module exists to break.
        effective[wid] = classify_beat(observed.get(wid), now,
                                       live_s=live_s, abandon_s=abandon_s)

    for source, target in sorted(bindings.items()):
        for wid in (target if isinstance(target, list) else [target]):
            if wid in ("core", None):
                continue
            if effective.get(wid) != LIVE:
                anomalies.append({"code": BINDING_UNAVAILABLE, "room": source,
                                  "worker_id": wid,
                                  "worker_state": effective.get(wid, UNKNOWN)})

    for wid in sorted(observed):
        if wid in desired:
            continue
        if classify_beat(observed.get(wid), now, live_s=live_s, abandon_s=abandon_s) == LIVE:
            # A hand-spawned worker is normal; it is reported, never acted on.
            anomalies.append({"code": UNEXPECTED, "worker_id": wid, "severity": "notice"})

    return {"transitions": transitions, "anomalies": anomalies}
