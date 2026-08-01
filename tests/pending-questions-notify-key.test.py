#!/usr/bin/env python3
"""should_notify() must gate on the SET, not only the clock.

Regression for 2026-08-01. The rule was purely time-based — 3600s since the marker's mtime — with
no awareness of whether anything changed, so an unchanged queue re-notified every hour forever.
Observed live: the identical 17 items reached the owner three times inside 60 minutes (05:43 cron,
06:17 briefing, 06:4x cron), content hash unchanged across all three.

The discriminator already existed: `questions_key()` hashes the sorted titles and was used to name
the proactive file; the cooldown just never consulted it.

Every assertion below FAILS on the pre-fix module (it takes no argument and ignores content).
"""
import importlib.util
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("cpq", REPO / "src" / "check-pending-questions.py")
cpq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpq)

_fails = []
def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        _fails.append(name)

import tempfile
with tempfile.TemporaryDirectory() as td:
    marker = Path(td) / ".last-pq-notify"
    cpq.LAST_NOTIFY_FILE = marker
    # ISOLATION: assert the module is actually pointed at the tmp marker, or every
    # assertion below would be measuring the real one.
    check("ISOLATION: marker path is inside the tmpdir", str(cpq.LAST_NOTIFY_FILE).startswith(td))

    A = [{"title": "q one"}, {"title": "q two"}]
    B = [{"title": "q one"}, {"title": "q two"}, {"title": "q three"}]
    ka, kb = cpq.questions_key(A), cpq.questions_key(B)
    check("questions_key distinguishes the two sets", ka != kb)

    check("no marker at all -> notify", cpq.should_notify(ka) is True)

    marker.write_text(f"{int(time.time())} {ka}")
    check("SAME set, fresh marker -> stay quiet", cpq.should_notify(ka) is False)

    # The core regression: the old rule re-notified purely because time passed.
    old = time.time() - 7200
    marker.write_text(f"{int(old)} {ka}")
    import os
    os.utime(marker, (old, old))
    check("SAME set, marker 2h old -> STILL quiet (was: re-notified hourly forever)",
          cpq.should_notify(ka) is False)

    check("CHANGED set -> notify even though the marker is fresh", cpq.should_notify(kb) is True)

    # Legacy marker (bare timestamp, no key) must notify once rather than suppress.
    marker.write_text(str(int(time.time())))
    check("legacy marker with no key -> notify once (never silently suppress)",
          cpq.should_notify(ka) is True)

    # Back-compat: a caller with no set to hash keeps the old time-only behaviour.
    marker.write_text(f"{int(time.time())} {ka}")
    check("key=None keeps time-only behaviour (fresh marker -> quiet)",
          cpq.should_notify(None) is False)
    marker.write_text(f"{int(old)} {ka}")
    os.utime(marker, (old, old))
    check("key=None keeps time-only behaviour (2h old -> notify)",
          cpq.should_notify(None) is True)

    # --- A FLOOR, NOT A CLIFF (Mini's cold review, 2026-08-01) -----------------
    # The first version of this fix ended at `key != last_key`, so an unchanged
    # set was announced exactly ONCE, EVER — mtime was read and then discarded on
    # that path. That is wrong in the case the file exists for: a set is unchanged
    # precisely BECAUSE nobody answered it (one host carries 54 such items), so
    # the queue would go permanently mute with no error.
    #
    # EVERY assertion in this block FAILS against that version, which returned
    # False for the unchanged set at any age.
    check("floor constant is a day, not a cliff", cpq.UNCHANGED_REMINDER_SEC == 86400)

    for label, age, want in (
        ("23h", 23 * 3600, False),          # still inside the quiet window
        ("25h", 25 * 3600, True),           # floor fires — the queue asks again
        ("30d", 30 * 86400, True),          # and keeps asking, not once-ever
        ("1y",  365 * 86400, True),
    ):
        t = time.time() - age
        marker.write_text(f"{int(t)} {ka}")
        os.utime(marker, (t, t))
        check(f"SAME set, marker {label} old -> {'notify again' if want else 'stay quiet'}",
              cpq.should_notify(ka) is want)

    # Control: the floor must not resurrect the ORIGINAL bug. An unchanged set
    # one hour old stays quiet, which is what the hourly-spam fix bought.
    t = time.time() - 3700
    marker.write_text(f"{int(t)} {ka}")
    os.utime(marker, (t, t))
    check("CONTROL: unchanged set just past the OLD 1h cooldown -> still quiet",
          cpq.should_notify(ka) is False)

print()
if _fails:
    print(f"{len(_fails)} test(s) FAILED: {_fails}")
    sys.exit(1)
print("all tests passed — unchanged sets stay quiet; changed sets notify")
