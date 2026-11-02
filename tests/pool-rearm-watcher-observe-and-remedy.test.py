#!/usr/bin/env python3
"""pool_supervise observes the watcher; pool_remedy re-arms it.

The observer reads the watcher beat as a file and consults the process table
only when the beat cannot vouch for the watcher: a session-role holder found
there is a watcher that beats nothing, not a lost one. `rearm_watcher` in a
tick's decisions runs the inbox's supervisor script, the one re-arm the design
allows, and the timer's JSON names it under `rearms` next to `recoveries`.
Every subprocess is a stub here: the tmux probe, the holder scan, and the
supervisor script are answered by argv, so the test pins what is asked of
each and how the answer is read, not this host's process table.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sys.path.insert(0, str(SCRIPTS))
sup = _load("pool_supervise")
rem = _load("pool_remedy")
pb = sup.pb
ps = sup.ps
wi = sup.wi

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {name}: {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


WID = "w1"
SOCK = "/tmp/pool-rearm-test.sock"


def scratch_pool():
    """A workspace with one enrolled worker whose run records a tmux locator."""
    ws = Path(tempfile.mkdtemp(prefix="pool-rearm-"))
    (ws / "state").mkdir()
    (ws / "state" / "roster.json").write_text(json.dumps(
        {"workers": {WID: {"state": "live", "label": "w1"}}}))
    wi.record_session(ws, WID, "s1", runtime="claude", relation=wi.RELATION_NEW)
    wi.start_incarnation(ws, WID, "s1", tmux_socket=SOCK,
                         tmux_session=wi.tmux_session_name(WID))
    return ws


class Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Stub:
    """Answers argv: the tmux probe alive, the holder scan by `holders`."""

    def __init__(self, holders="none\n", holders_err="", holders_rc=0):
        self.calls = []
        self.holders, self.holders_err, self.holders_rc = holders, holders_err, holders_rc

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[0] == "tmux":
            return Done(0)
        if "inbox-holders" in argv:
            return Done(self.holders_rc, self.holders, self.holders_err)
        if argv[0] == "bash" and argv[1].endswith("worker-watcher-supervisor.sh"):
            return Done(0)
        raise AssertionError(f"unexpected argv {argv}")

    def scans(self):
        return [c for c in self.calls if "inbox-holders" in c]


# --- observe -----------------------------------------------------------------

ws = scratch_pool()
now = time.time()

stub = Stub()
obs = sup.observe(ws, now, runner=stub)[WID]
check("no beat file reads ABSENT", obs.watcher_beat, ps.ABSENT)
check("an absent beat under a live session consults the table", len(stub.scans()), 1)
check("the scan names THIS worker's inbox",
      stub.scans()[0][-1], str(sup.pd.deliveries_dir(ws, WID)))
check("`none` with nothing undecided is a proven absence", obs.watcher_held, False)

stub = Stub(holders="4242 session\n")
obs = sup.observe(ws, now, runner=stub)[WID]
check("a session-role line is a holder", obs.watcher_held, True)

stub = Stub(holders="4242 standby\n")
obs = sup.observe(ws, now, runner=stub)[WID]
check("a standby alone is not a session-role holder", obs.watcher_held, False)

stub = Stub(holders="none\n", holders_err="undecided=2\n")
obs = sup.observe(ws, now, runner=stub)[WID]
check("undecided lines make the answer None, never False", obs.watcher_held, None)

stub = Stub(holders_rc=2, holders="unobserved\n")
obs = sup.observe(ws, now, runner=stub)[WID]
check("an unobservable table is None", obs.watcher_held, None)

pb.touch(pb.beat_path(ws, "watcher", WID))
# `now` was sampled before the touch; a beat newer than `now` reads as a clock fault.
os.utime(pb.beat_path(ws, "watcher", WID), (now - 1, now - 1))
stub = Stub()
obs = sup.observe(ws, now, runner=stub)[WID]
check("a live watcher beat reads LIVE", obs.watcher_beat, ps.LIVE)
check("a live beat never touches the process table", len(stub.scans()), 0)
check("...so held stays unasked", obs.watcher_held, None)

old = now - 200
os.utime(pb.beat_path(ws, "watcher", WID), (old, old))
stub = Stub()
obs = sup.observe(ws, now, runner=stub)[WID]
check("a beat past 90 s reads STALE and asks the table", (obs.watcher_beat, len(stub.scans())),
      (ps.STALE, 1))


class DeadSession(Stub):
    def __call__(self, argv, **kw):
        if argv[0] == "tmux":
            self.calls.append(list(argv))
            return Done(1, err="can't find session")
        return super().__call__(argv, **kw)


stub = DeadSession()
obs = sup.observe(ws, now, runner=stub)[WID]
check("a dead session has no watcher ladder: the table is not read",
      (obs.session_alive, len(stub.scans())), (False, 0))

# --- the state survives a round trip with the watcher fields ---------------------

st = ps.SupervisionState(last_sample_at=5.0, workers={WID: ps.WorkerEvidence(
    watcher_first_detected_at=1.0, watcher_consecutive=2, rearm_issued_at=3.0,
    watcher_escalated=True)})
sup.save_state(ws, st)
back = sup.load_state(ws)
check("the watcher evidence round-trips through the state file",
      back.workers[WID], st.workers[WID])
raw = json.loads(sup.state_path(ws).read_text())
del raw["workers"][WID]["watcher_consecutive"]
sup.state_path(ws).write_text(json.dumps(raw))
check("a state written before this rung loads with the watcher fields at rest",
      sup.load_state(ws).workers[WID].watcher_consecutive, 0)

# --- the tick reports what it saw ----------------------------------------------

os.utime(pb.beat_path(ws, "watcher", WID), (old, old))
out = sup.tick(ws, now, runner=Stub(), persist=False)
check("the tick's observation carries the watcher fields",
      (out["observations"][WID]["watcher_beat"], out["observations"][WID]["watcher_held"]),
      (ps.STALE, False))

# --- remedy ------------------------------------------------------------------

stub = Stub()
acted = rem.apply(ws, REPO, {WID: ps.REARM_WATCHER}, runner=stub)
sup_calls = [c for c in stub.calls if c[0] == "bash"]
check("rearm_watcher runs the inbox's supervisor script, once", len(sup_calls), 1)
check("...and reports it under rearms", acted["rearms"][WID]["outcome"], rem.SUPERVISED)
check("nothing was recovered or escalated", (acted["recoveries"], acted["escalations"]),
      ({}, []))

acted = rem.apply(ws, REPO, {WID: ps.ESCALATE}, runner=Stub())
check("escalate is still returned untouched, with rearms empty",
      (acted["escalations"], acted["rearms"]), ([WID], {}))

acted = rem.apply(ws, REPO, {WID: ps.NOTHING}, runner=Stub())
check("nothing acts on nothing", acted["rearms"], {})

# --- the timer's own JSON names the rung ----------------------------------------

env = {k: v for k, v in os.environ.items() if not k.startswith("SUTANDO_")}
r = subprocess.run([sys.executable, str(SCRIPTS / "pool_remedy.py"), "--workspace", str(ws),
                    "--repo", str(REPO), "--recipient", WID, "--dry-run"],
                   capture_output=True, text=True, env=env, timeout=60)
try:
    payload = json.loads(r.stdout)
except ValueError:
    payload = {}
check("a dry run's JSON carries `rearms`", "rearms" in payload, True)
check("...and the decision for this worker", WID in payload.get("decisions", {}), True)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — pool rearm_watcher observe + remedy (23 checks)")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
