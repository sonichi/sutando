#!/usr/bin/env python3
"""Gate for /startup step 3: exit 0 only when this session's cron ceremony is stamped complete.

Runs health-check's `session-crons` probe — the same stamp-vs-session-boundary test the desktop
app's ceremony-health uses to decide whether to re-send /startup — so the agent sees the app's
criterion at the moment it can act. A hand-rolled `CronCreate` passes every cheap check
(`CronList` looks perfect) and still fails this one, because only /schedule-crons writes the stamp.

Usage: verify-ceremony.py [--workspace DIR] [--host-label LABEL]
Exit: 0 = ok (emit "/startup complete"), 1 = not ok (do NOT emit; run /schedule-crons), 2 = probe unavailable.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
# Must equal STAMP_SKEW_TOLERANCE_SECS in the desktop's ceremony_health.rs; change both together.
STAMP_SKEW_TOLERANCE_SECS = 2.0
# Private health-check names the gate reads; a missing one is "cannot answer" (rc 2), not rc 1.
REQUIRED_PROBE_NAMES = ("check_session_cron_registration", "_last_core_launch_at", "_host_label", "WORKSPACE_DIR")


def load_health_check():
    probe_file = REPO / "src" / "health-check.py"
    # spec_from_file_location returns a VALID spec for a missing file (None only for an
    # unknown suffix), so the existence check has to be explicit or the guard guards nothing.
    if not probe_file.is_file():
        return None
    spec = importlib.util.spec_from_file_location("health_check", probe_file)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    except Exception:  # unimportable probe is "cannot answer" (rc 2), never "not ok" (rc 1)
        return None
    return mod


def stamp_predates_launch(mod, workspace, host_label):
    """The desktop ceremony-health criterion, verbatim: a readable stamp older than this host's
    launch boundary is a diverged ceremony, whatever crons.json holds. None = no evidence."""
    launch = mod._last_core_launch_at(workspace)
    ws = Path(workspace or mod.WORKSPACE_DIR)
    stamp_file = ws / "hosts" / (host_label or mod._host_label()) / "schedule-crons-stamp.json"
    try:
        ts = json.loads(stamp_file.read_text()).get("ts")
    except (OSError, ValueError, AttributeError):
        return None
    if not launch or isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    started_at = launch[0]
    if ts + STAMP_SKEW_TOLERANCE_SECS < started_at:
        return int(started_at - ts)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", type=Path, default=None)
    ap.add_argument("--host-label", default=None)
    a = ap.parse_args(argv)
    mod = load_health_check()
    if mod is None or any(not hasattr(mod, n) for n in REQUIRED_PROBE_NAMES):
        print("verify-ceremony: session-crons probe unavailable — cannot confirm; do not report complete", file=sys.stderr)
        return 2
    result = mod.check_session_cron_registration(a.workspace, host_label=a.host_label)
    status, detail = result.get("status"), result.get("detail", "")
    if status == "ok":
        # The probe forgives a stale stamp when no session cron is expected; the desktop does not.
        try:
            stale = stamp_predates_launch(mod, a.workspace, a.host_label)
        except (AttributeError, TypeError) as exc:  # helper drifted after the name check
            print(f"verify-ceremony: launch-boundary check unavailable ({exc}) — cannot confirm; "
                  "do not report complete", file=sys.stderr)
            return 2
        if stale is not None:
            status = "warn"
            detail = (f"stamp predates this session's launch ({stale}s older) — the desktop re-sends "
                      "/startup until /schedule-crons stamps, even with 0 crons to register")
    if status == "ok":
        print(f"verify-ceremony: ok — {detail}")
        return 0
    print(f"verify-ceremony: {status} — {detail}", file=sys.stderr)
    print("verify-ceremony: NOT complete. Invoke /schedule-crons (the only writer of "
          "schedule-crons-stamp.json) and re-run; do not hand-roll CronCreate.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
