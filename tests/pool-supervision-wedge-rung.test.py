#!/usr/bin/env python3
"""pool_supervision's wedge rung: a live worker session that will not progress.

A worker whose session answers alive never reaches the death ladder, so a limit
menu, a permission dialog or a frozen turn used to hold its work with nobody
acting. The wedge rung reads the pane the caller captured (pane_gate + cli_wedge)
and what the worker owes (task_dispatch): sustained past the stale line it asks
once per episode for the owner — a gate or limit escalates, abnormal text asks for
a card naming its cause, a frozen turn for a card offering Escape. No wedge kind
restarts a session. Every subprocess here is a stub answered by argv.
"""
import importlib.util
import json
import sys
import tempfile
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
ps, wi, pd = sup.ps, sup.wi, sup.pd

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {name}: {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


CAUSE = getattr(ps, "CARD_CAUSE", "card_cause")
FROZEN = getattr(ps, "CARD_FROZEN", "card_frozen")
GATE, LIMIT, ABN, WORK, IDLE = (getattr(ps, n, v) for n, v in (
    ("PANE_GATE", "gate"), ("PANE_LIMIT", "limit"), ("PANE_ABNORMAL", "abnormal"),
    ("PANE_WORKING", "working"), ("PANE_IDLE", "idle")))


def obs(pane, pane_id="f1", work=True, **kw):
    """A live session with a live watcher: only the wedge rung can speak."""
    fields = dict(beat=ps.LIVE, session_alive=True, watcher_beat=ps.LIVE)
    extra = dict(work_outstanding=work, pane=pane, pane_id=pane_id)
    try:
        return ps.Observation(**fields, **extra, **kw)
    except TypeError:          # the parent commit has no such fields
        return ps.Observation(**fields, **kw)


def run(seq, period=300.0):
    """Feed one observation per tick; return the decisions in order."""
    st, out, t = ps.SupervisionState(), [], 1000.0
    for o in seq:
        st, d = ps.evaluate(st, {"w": o}, t)
        out.append(d["w"])
        t += period
    return out, st


N = ps.NOTHING
E = ps.ESCALATE

# --- the policy ----------------------------------------------------------------

got, _ = run([obs(GATE)] * 5)
check("a gate held with work outstanding escalates at the sustain, once, never restarts",
      got, [N, N, E, N, N])

got, _ = run([obs(LIMIT)] * 4)
check("a limit is a human's wait-or-spend decision: escalate, never restart", got, [N, N, E, N])

got, _ = run([obs(ABN)] * 5)
check("abnormal text sustained asks once for a card naming its cause, never a restart",
      got, [N, N, CAUSE, N, N])

got, _ = run([obs(WORK, "same")] * 5)
check("a turn whose raw frame never changes is stuck: the 2nd identical frame is the 1st detection",
      got, [N, N, N, FROZEN, N])

got, _ = run([obs(WORK, f"f{i}") for i in range(6)])
check("a turn whose frame moves is working, whatever the queue says", got, [N] * 6)

got, _ = run([obs(GATE, work=False)] * 5)
check("an idle worker parked on a limit owes nothing: not wedged", got, [N] * 5)

got, _ = run([obs(IDLE)] * 5)
check("at its prompt with work queued is the watcher rung's case, not a wedge", got, [N] * 5)

got, _ = run([obs(ABN), obs(ABN), obs(IDLE, work=False), obs(ABN), obs(ABN)])
check("one clean tick clears the wedge clock", got, [N, N, N, N, N])

got, _ = run([obs(ABN)] * 3, period=20.0)
check("the sustain is not enough: the 90 s stale line must also pass", got, [N, N, N])

got, _ = run([obs(ABN, paused=True)] * 5)
check("owner-paused outranks the wedge rung", got, [N] * 5)

# The watcher rung still speaks when the pane is healthy.
lost = ps.Observation(beat=ps.LIVE, session_alive=True, watcher_beat=ps.ABSENT,
                      watcher_held=False)
got, _ = run([lost] * 3)
check("a lost watcher with no wedge data still re-arms (rung untouched)", got, [N, N, ps.REARM_WATCHER])

# --- the state file keeps the wedge clock ------------------------------------------

ws = Path(tempfile.mkdtemp(prefix="pool-wedge-"))
_, st = run([obs(ABN)] * 3)
sup.save_state(ws, st)
check("the wedge evidence round-trips through the state file",
      sup.load_state(ws).workers.get("w"), st.workers.get("w"))

# --- the observation: this worker's own queue --------------------------------------

WID = "w1"
SOCK = "/tmp/pool-wedge-test.sock"
(ws / "state" / "roster.json").write_text(json.dumps(
    {"workers": {WID: {"state": "live", "label": "comm"}}}))
wi.record_session(ws, WID, "s1", runtime="claude", relation=wi.RELATION_NEW)
wi.start_incarnation(ws, WID, "s1", tmux_socket=SOCK, tmux_session=wi.tmux_session_name(WID))
inbox = pd.deliveries_dir(ws, WID)
inbox.mkdir(parents=True, exist_ok=True)
(ws / "tasks").mkdir(exist_ok=True)
(ws / "tasks" / "task-core-1.txt").write_text("the core's own queue")

wo = getattr(sup, "work_outstanding", lambda *a: "missing")
results = pd.results_dir(ws)
results.mkdir(parents=True, exist_ok=True)
check("the core's queue is not this worker's work", wo(ws, WID), False)
(inbox / "task-a.txt").write_text("")
check("a sentinel handed to it with no reply yet is work", wo(ws, WID), True)
(inbox / "task-a.txt").rename(inbox / "task-a.accepted")
check("an accepted sentinel with no reply is work in flight", wo(ws, WID), True)
(results / "task-a.txt").write_text("done\n")
check("a sentinel whose reply is ready is spent, whatever its suffix", wo(ws, WID), False)

# --- the observation: the pane, read by the core's readers ------------------------

FOOTER = "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
PANES = {
    "permission": ("  Do you want to proceed?\n  ❯ 1. Yes\n    2. No\n  Esc to cancel\n", GATE),
    "limit menu": ("  ⎿  You've hit your weekly limit · resets Sep 27 at 2am\n"
                   "  What do you want to do?\n  ❯ 1. Stop and wait for limit to reset\n"
                   "    2. Switch to extra usage\n  Enter to confirm · Esc to cancel\n", GATE),
    "limit banner": (f"  ⎿  You've hit your session limit · resets 12:10pm\n❯ \n{FOOTER}\n", LIMIT),
    "api error": (f"  ⎿  API Error: 500 internal server error\n❯ \n{FOOTER}\n", ABN),
    "working": (f"✻ Thinking… (12s · esc to interrupt)\n❯ \n{FOOTER}\n", WORK),
    "idle": (f"❯ \n{FOOTER}\n", IDLE),
}
classify = getattr(sup, "classify_pane_text", None)
for label, (text, want) in PANES.items():
    check(f"pane: {label}", classify(text, workspace=ws) if classify else "missing", want)


class Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Stub:
    """tmux answers alive; capture-pane shows `pane`; kill-session is recorded."""

    def __init__(self, pane):
        self.pane, self.calls = pane, []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[0] == "tmux" and "capture-pane" in argv:
            return Done(0, self.pane)
        if argv[0] == "tmux":
            return Done(0)
        if "inbox-holders" in argv:
            return Done(0, "4242 session\n")
        raise AssertionError(f"unexpected argv {argv}")

    def killed(self):
        return [c for c in self.calls if "kill-session" in c]


(inbox / "task-b.txt").write_text("")
o = sup.observe(ws, 1000.0, runner=Stub(PANES["permission"][0]))[WID]
check("observe reads the worker's pane and queue",
      (getattr(o, "pane", None), getattr(o, "work_outstanding", None)), (GATE, True))
check("observe carries a raw frame id for the stuck test", bool(getattr(o, "pane_id", None)), True)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — pool_supervision wedge rung")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
