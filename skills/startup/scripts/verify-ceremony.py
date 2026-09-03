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
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def load_probe():
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
    return getattr(mod, "check_session_cron_registration", None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", type=Path, default=None)
    ap.add_argument("--host-label", default=None)
    a = ap.parse_args(argv)
    probe = load_probe()
    if probe is None:
        print("verify-ceremony: session-crons probe unavailable — cannot confirm; do not report complete", file=sys.stderr)
        return 2
    result = probe(a.workspace, host_label=a.host_label)
    status, detail = result.get("status"), result.get("detail", "")
    if status == "ok":
        print(f"verify-ceremony: ok — {detail}")
        return 0
    print(f"verify-ceremony: {status} — {detail}", file=sys.stderr)
    print("verify-ceremony: NOT complete. Invoke /schedule-crons (the only writer of "
          "schedule-crons-stamp.json) and re-run; do not hand-roll CronCreate.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
