#!/usr/bin/env python3
"""`_local_core_alive` / `_local_core_started_within` must reject a FUTURE mtime.

Both used a one-sided age test -- `(now - mtime) < max_age_s` and
`now - mtime >= 90.0` -- while `heartbeat_is_fresh()` in the same module is
two-sided precisely because a future-dated heartbeat has a NEGATIVE age that
every one-sided test accepts as fresh forever (clock step, bad write, skewed
sync). A core that reads as permanently alive and permanently just-booted
suppresses the dead-core recovery this file exists to provide.

Why this file exists alongside `heartbeat-freshness-bounds.test.py`: that suite
covers `_live_core_socket` / `_local_core_socket`, NOT these two. Measured --
it returns rc=0 against the broken code, so its name ("bounded on both ends")
reads like coverage these functions never had.

Run: python3 tests/local-core-freshness-bounds.test.py
"""
from __future__ import annotations
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
_spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)

HOST = subprocess.run(["bash", str(REPO / "scripts" / "sutando-config.sh"), "host-label"],
                      capture_output=True, text=True).stdout.strip()


def _ws(mtime_offset=None, started_offset=-10.0, now=None):
    """Workspace with this host's .alive at now+mtime_offset; None = no file."""
    now = time.time() if now is None else now
    ws = Path(tempfile.mkdtemp())
    (ws / "state" / "cores").mkdir(parents=True)
    if mtime_offset is not None:
        f = ws / "state" / "cores" / f"{HOST}.alive"
        f.write_text(json.dumps({"started_at": now + started_offset}))
        os.utime(f, (now + mtime_offset, now + mtime_offset))
    return ws, now


class LocalCoreFreshnessBounds(unittest.TestCase):
    def test_future_mtime_is_not_alive(self):
        ws, now = _ws(mtime_offset=100_000)
        self.assertIs(hc._local_core_alive(workspace=ws), False,
                      "a heartbeat dated in the FUTURE read as a live core — "
                      "one-sided age test accepts a negative age")

    def test_future_mtime_is_not_just_booted(self):
        ws, now = _ws(mtime_offset=100_000)
        self.assertIs(hc._local_core_started_within(300, workspace=ws, now=now), False,
                      "a future-dated heartbeat read as just-booted, which "
                      "suppresses recovery on every subsequent pass")

    # --- negative controls: the fix must not reject legitimate states ---
    def test_fresh_still_alive(self):
        ws, now = _ws(mtime_offset=-5)
        self.assertIs(hc._local_core_alive(workspace=ws), True)
        self.assertIs(hc._local_core_started_within(300, workspace=ws, now=now), True)

    def test_stale_is_dead(self):
        ws, now = _ws(mtime_offset=-500)
        self.assertIs(hc._local_core_alive(workspace=ws), False)
        self.assertIs(hc._local_core_started_within(300, workspace=ws, now=now), False)

    def test_missing_file_is_dead_not_unknown(self):
        ws, now = _ws(mtime_offset=None)
        self.assertIs(hc._local_core_alive(workspace=ws), False)
        self.assertIs(hc._local_core_started_within(300, workspace=ws, now=now), False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
