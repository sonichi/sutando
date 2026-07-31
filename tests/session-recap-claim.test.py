#!/usr/bin/env python3
"""Tests for session-recap recap_claim.py (single-flight boot recap) — hermetic.

The boot recap worker runs in the background for ~1-2 min and only stamps
state/last-recap-session.txt at the END, so the stamp alone cannot stop a
mid-session /schedule-crons re-run from double-launching a second worker
inside that window (#2454 review, P1). recap_claim.py closes the window with
an O_EXCL-atomic claim file. Pinned behaviors:
  1. concurrency: N simultaneous claims for the same session -> exactly ONE
     exit-0 winner (the double-launch proof)
  2. a fresh claim blocks later claimers (in-flight skip)
  3. release --stamp writes the stamp atomically and drops the claim;
     a later claim for the same session skips as already-recapped
  4. release without --stamp (failure path) drops the claim only -> a later
     claim for the session wins again (retry works)
  5. a stale claim (dead worker) is reclaimed by the next claimer
  6. a corrupt claim file counts as stale (age falls back to epoch 0)
  7. CLI surface: main() wires claim/release + --state-dir + exit codes

All state goes through --state-dir tempdirs (the supported test hook — no
env vars); calls are in-process (importlib + argv patch, the repo's
coverage-visible pattern).

Run: python3 tests/session-recap-claim.test.py   (exit 0 pass / 1 fail)
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "session-recap" / "scripts" / "recap_claim.py"

spec = importlib.util.spec_from_file_location("recap_claim", SCRIPT)
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


def cli(*argv: str) -> tuple[int, str]:
    out, old = io.StringIO(), sys.argv
    sys.argv = ["recap_claim.py", *argv]
    try:
        with redirect_stdout(out):
            code = rc.main()
    finally:
        sys.argv = old
    return code, out.getvalue()


SESSION = "aaaa-bbbb-cccc"
STALE_S = 15 * 60

# 1. concurrency: 8 simultaneous claimers, exactly one winner — repeated 20
# rounds. One round passes on timing luck (the original create-then-write
# implementation went green locally, then produced 5-of-8 winners on the
# 2-core CI runner when a claimer read a nascent claim as corrupt→stale and
# unlinked it); the repeat makes the test actually sensitive to the race.
bad_rounds = []
for rnd in range(20):
    dr = Path(tempfile.mkdtemp(prefix=f"claim-race-{rnd}-"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        with redirect_stdout(io.StringIO()):
            codes = list(pool.map(
                lambda _: rc.claim(dr, SESSION, STALE_S), range(8)))
    claim_ok = ((dr / rc.CLAIM_NAME).exists() and
                json.loads((dr / rc.CLAIM_NAME).read_text())["session"]
                == SESSION)
    leftover_tmp = list(dr.glob(".recap-claim-*"))
    if codes.count(0) != 1 or codes.count(1) != 7 or not claim_ok \
            or leftover_tmp:
        bad_rounds.append((rnd, codes, claim_ok, leftover_tmp))
    d1 = dr  # last round's dir feeds the lifecycle checks below
check("race: exactly one of 8 concurrent claims wins, every round (x20), "
      "claim intact, no temp litter",
      not bad_rounds, str(bad_rounds[:3]))

# 2. a fresh claim blocks a later claimer
code, msg = cli("claim", SESSION, "--state-dir", str(d1))
check("fresh claim blocks re-claim", code == 1 and "in-flight" in msg, msg)

# 3. release --stamp: stamp written, claim dropped, session now skipped
code, msg = cli("release", SESSION, "--stamp", "--state-dir", str(d1))
check("release --stamp exits 0", code == 0 and "stamped" in msg, msg)
check("release --stamp writes the stamp",
      (d1 / rc.STAMP_NAME).read_text().strip() == SESSION)
check("release --stamp drops the claim", not (d1 / rc.CLAIM_NAME).exists())
code, msg = cli("claim", SESSION, "--state-dir", str(d1))
check("stamped session skips as already-recapped",
      code == 1 and "already-recapped" in msg, msg)

# ...but a NEW session claims fine over an old stamp
code, _ = cli("claim", "dddd-next", "--state-dir", str(d1))
check("new session claims over an old stamp", code == 0)

# 4. failure-path release (no --stamp): claim dropped, no stamp, retry wins
code, msg = cli("release", "dddd-next", "--state-dir", str(d1))
check("bare release exits 0 without stamping",
      code == 0 and "stamped" not in msg
      and (d1 / rc.STAMP_NAME).read_text().strip() == SESSION, msg)
code, _ = cli("claim", "dddd-next", "--state-dir", str(d1))
check("retry after failure-path release wins", code == 0)

# 5. stale claim (dead worker) is reclaimed
d2 = Path(tempfile.mkdtemp(prefix="claim-stale-"))
(d2 / rc.CLAIM_NAME).write_text(json.dumps(
    {"session": SESSION, "ts": time.time() - STALE_S - 60, "pid": 1}))
code, msg = cli("claim", SESSION, "--state-dir", str(d2))
check("stale claim reclaimed", code == 0 and "claimed" in msg, msg)
check("reclaim refreshed the ts",
      time.time() - json.loads((d2 / rc.CLAIM_NAME).read_text())["ts"] < 60)

# 6. corrupt claim file counts as stale
d3 = Path(tempfile.mkdtemp(prefix="claim-corrupt-"))
(d3 / rc.CLAIM_NAME).write_text("not json{")
code, msg = cli("claim", SESSION, "--state-dir", str(d3))
check("corrupt claim treated as stale and reclaimed", code == 0, msg)

# 6b. defensive loop exit: if every link round collides (claim keeps
# reading stale), the claimer gives up with exit 1 instead of spinning
d4 = Path(tempfile.mkdtemp(prefix="claim-lostrace-"))
real_link = rc.os.link


def always_busy(*a, **k):
    raise FileExistsError


rc.os.link = always_busy
try:
    with redirect_stdout(io.StringIO()):
        code = rc.claim(d4, SESSION, STALE_S)
finally:
    rc.os.link = real_link
check("exhausted re-race gives up with exit 1", code == 1)
check("temp file cleaned up even on give-up",
      not list(d4.glob(".recap-claim-*")))

# 7. state_dir default resolution reaches <workspace>/state (no override)
resolved = rc.state_dir(None)
check("state_dir default resolves under the workspace",
      resolved.name == "state", str(resolved))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All recap_claim checks passed.")
