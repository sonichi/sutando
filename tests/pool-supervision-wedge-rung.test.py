#!/usr/bin/env python3
"""pool_supervision's wedge rung: a live worker session that will not progress.

A worker whose session answers alive never reaches the death ladder, so a limit
menu, a permission dialog or a frozen turn used to hold its work with nobody
acting. The wedge rung reads the pane the caller captured (pane_gate + cli_wedge)
and what the worker owes (task_dispatch): sustained past the stale line it asks
once per episode for the owner — a gate or limit asks for a terminal card, abnormal text asks for
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
HUMAN = ps.CARD_HUMAN
LOGIN = getattr(ps, "CARD_LOGIN", "card_login")
LOGGED_OUT = getattr(ps, "PANE_LOGGED_OUT", "logged-out")
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


def run(seq, period=300.0, ack=True):
    """Feed one observation per tick; return the decisions in order. With `ack`, a
    card decision is acknowledged as the remedy does once the card exists."""
    st, out, t = ps.SupervisionState(), [], 1000.0
    for o in seq:
        st, d = ps.evaluate(st, {"w": o}, t)
        out.append(d["w"])
        if ack and d["w"] in (CAUSE, FROZEN, LOGIN, HUMAN):
            st = ps.acknowledge(st, ["w"])
        t += period
    return out, st


N = ps.NOTHING
E = ps.ESCALATE

# --- the policy ----------------------------------------------------------------

got, _ = run([obs(GATE)] * 5)
check("a gate held with work outstanding asks for a card at the sustain, once, never restarts",
      got, [N, N, HUMAN, N, N])

got, _ = run([obs(LIMIT)] * 4)
check("a limit is a human's wait-or-spend decision: card, never restart", got, [N, N, HUMAN, N])

got, _ = run([obs(ABN)] * 5)
check("abnormal text sustained asks once for a card naming its cause, never a restart",
      got, [N, N, CAUSE, N, N])

got, _ = run([obs(WORK, "same")] * 5)
check("a turn whose raw frame never changes is stuck: the 2nd identical frame is the 1st detection",
      got, [N, N, N, FROZEN, N])

got, _ = run([obs(ABN)] * 5, ack=False)
check("a card not yet raised is asked for again every tick", got, [N, N, CAUSE, CAUSE, CAUSE])

got, st = run([obs(WORK, "a")] * 3 + [obs(WORK, "b")])
check("a CHANGED frame id clears the frozen reading", (got, st.workers["w"].wedge_consecutive,
                                                      st.workers["w"].last_pane_id), ([N] * 4, 0, "b"))
got, _ = run([obs(WORK, "a")] * 3 + [obs(WORK, "b")] * 4)
check("...and the sustain starts over from the new frame", got, [N, N, N, N, N, N, FROZEN])

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

# --- an expired login: the CLI's own words, decided on the tick that reads them ------

got, _ = run([obs(LOGGED_OUT, work=False)] * 4)
check("a logged-out session owing nothing is carded at once, once per episode, never restarted",
      got, [LOGIN, N, N, N])

got, _ = run([obs(LOGGED_OUT)] * 3, ack=False)
check("the login card is asked for again every tick until it exists", got, [LOGIN, LOGIN, LOGIN])

got, _ = run([obs(LOGGED_OUT, work=False)] * 2, period=20.0)
check("no stale line: a login banner is not a timing question", got, [LOGIN, N])

got, st = run([obs(LOGGED_OUT, work=False)] * 2 + [obs(IDLE, work=False)])
check("/login run: the idle pane clears the login episode",
      (got, st.workers["w"].wedge_consecutive, st.workers["w"].wedge_escalated), ([LOGIN, N, N], 0, False))

got, _ = run([obs(LOGGED_OUT, work=False)] * 2 + [obs(IDLE, work=False)] + [obs(LOGGED_OUT, work=False)])
check("...and a second expiry is a new episode with its own card", got, [LOGIN, N, N, LOGIN])

got, _ = run([obs(LOGGED_OUT, work=False, paused=True)] * 3)
check("owner-paused outranks the login card", got, [N, N, N])

unanswered = ps.Observation(beat=ps.LIVE, session_alive=None, pane=LOGGED_OUT)
got, _ = run([unanswered] * 2)
check("a pane under an UNANSWERED session probe is no evidence: no login card", got, [N, N])

# --- one decision per (episode, kind): a pane that moves straight into another wedge ---

got, st = run([obs(LOGGED_OUT, work=False), obs(ABN), obs(ABN), obs(ABN)])
check("an acknowledged login card does not silence the abnormal text that follows it",
      (got, getattr(st.workers["w"], "wedge_kind", None)), ([LOGIN, N, CAUSE, N], "abnormal"))

got, _ = run([obs(LOGGED_OUT, work=False), obs(LIMIT), obs(LIMIT), obs(LIMIT)])
check("...nor a limit that follows it: that cards at the episode's sustain", got, [LOGIN, N, HUMAN, N])

got, _ = run([obs(ABN)] * 3 + [obs(LOGGED_OUT)] * 2)
check("an expiry after an acknowledged cause card is carded on the tick that reads it",
      got, [N, N, CAUSE, LOGIN, N])

got, _ = run([obs(LOGGED_OUT, work=False)] * 2 + [obs(ABN)] * 2 + [obs(LOGGED_OUT, work=False)])
check("returning to a kind already carded in the episode is a change of kind too",
      got, [LOGIN, N, CAUSE, N, LOGIN])

legacy = ps.SupervisionState(last_sample_at=1000.0, workers={"w": ps.WorkerEvidence(
    wedge_first_detected_at=400.0, wedge_consecutive=3, wedge_escalated=True)})
_, d = ps.evaluate(legacy, {"w": obs(ABN)}, 1300.0)
check("a state file from before the kind was kept: the raised card stays the episode's one",
      d["w"], N)

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


# --- a stale limit banner over a session that is still progressing -----------------
# After a re-login the CLI can leave its limit banner painted under a live session.

STALE = ("  \u26a0 Usage limit reached \u00b7 continuing automatically at 2am \u00b7 esc to cancel\n"
         "  \u26a0 /usage-credits to continue now\n")
RULE = "\u2500" * 40 + "\n"


def framed(body):
    return f"{body}{RULE}\u276f \n{RULE}{STALE}{FOOTER}\n"


def frame_obs(text):
    """What observe() hands the ladder for this capture: its kind and its frame id."""
    return obs(classify(text, workspace=ws), sup.frame_id(text))


def turn_done(i):
    """The pane after turn i finished: every earlier turn is still on screen above it."""
    return framed("".join(f"\u25cf Sent a cross-session message ({k}).\n"
                          f"\u273b Worked for {k + 2}s \u00b7 done 2:{10 + k:02d} PM\n"
                          for k in range(i + 1)))


def thinking(i):
    return framed(f"\u25cf Sent a cross-session message.\n"
                  f"\u2733 Thinking\u2026 ({i}s \u00b7 esc to interrupt)\n")


check("a live turn under a stale banner reads as working", classify(thinking(3), workspace=ws), WORK)
check("an idle seat under a stale banner reads as a wait", classify(turn_done(0), workspace=ws), GATE)
got, _ = run([frame_obs(thinking(i)) for i in range(6)])
check("a live turn under a stale banner is no gate: no card", got, [N] * 6)
got, _ = run([frame_obs(turn_done(i)) for i in range(6)])
check("turns finishing under a stale banner are progress: no card", got, [N] * 6)
got, _ = run([frame_obs(turn_done(0))] * 4)
check("control: a banner on a frozen screen, work owed, is carded", got, [N, N, HUMAN, N])
got, _ = run([obs(GATE, "r:n", work=False)] * 4)
check("...and with no work owed it is not", got, [N] * 4)
got, _ = run([frame_obs(framed(f"\u25cf Paused.\n\u273b Worked for {n}s \u00b7 done 2:10 PM\n"))
              for n in range(2, 6)])
check("a frozen screen whose only change is a clock is not progress", got, [N, N, HUMAN, N])
got, _ = run([frame_obs(turn_done(0))] * 2 + [frame_obs(turn_done(1))] * 4)
check("a move clears the sustain, which starts over once the frame settles",
      got, [N, N, N, N, N, HUMAN])
got, _ = run([obs(WORK, "turn:t")] + [obs(GATE, "dialog:d")] * 3)
check("entering a gate changes the frame, which is not progress under it", got, [N, N, N, HUMAN])
got, _ = run([obs(LIMIT, "f1")] * 3)
check("a bare raw id carries no progress evidence", got, [N, N, HUMAN])
got, _ = run([obs(LIMIT, "r1:n1"), obs(LIMIT, "r1:n1"), obs(LIMIT, None)])
check("an unread frame id is no evidence of progress", got, [N, N, HUMAN])

# A worker genuinely held by the limit: idle prompt, the banner, no turn since, deliveries
# piling up. Only the footer's counters change, and they are status, not work.


def held(n_tasks, monitors):
    return (f"● Picked up the next delivery.\n✻ Worked for 4m 12s · done 1:36 PM\n"
            f"{RULE}❯ \n{RULE}{STALE}  ⏵⏵ bypass permissions on · {n_tasks} tasks"
            f" · {monitors} monitor · ← for agents\n")


HELD = [held(23, 1), held(24, 1), held(24, 2), held(25, 2)]
check("a held worker under the banner reads as a wait", classify(HELD[0], workspace=ws), GATE)
got, _ = run([frame_obs(f) for f in HELD])
check("a held worker whose footer counters tick is still carded", got, [N, N, HUMAN, N])
carded = ps.SupervisionState(last_sample_at=1000.0, workers={"w": ps.WorkerEvidence(
    wedge_first_detected_at=400.0, wedge_consecutive=26, wedge_escalated=True,
    wedge_kind="human", last_pane_id="064e072fcb5c")})
st, out = carded, []
for i, f in enumerate(HELD):
    st, d = ps.evaluate(st, {"w": frame_obs(f)}, 1095.0 + 95 * i)
    out.append(d["w"])
check("an episode already carded under a bare id stays carded and asks no more",
      (out, st.workers["w"].wedge_kind, st.workers["w"].wedge_escalated), ([N] * 4, "human", True))

REFUSED = (f"\u276f check the inbox\n  \u23bf  You've hit your session limit \u00b7 resets 2am\n"
           f"\u273b Worked for 0s\n\u276f \n{FOOTER}\n")
for label, text in (("a refused limit turn", REFUSED), ("the limit menu", PANES["limit menu"][0]),
                    ("a permission dialog", PANES["permission"][0])):
    got, _ = run([frame_obs(text)] * 4)
    check(f"control: {label} on a frozen screen, work owed, is carded", got, [N, N, HUMAN, N])

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


def observed(frames):
    """observe() over captured frames, each fed to the ladder: the real observer's id."""
    return run([sup.observe(ws, 1000.0 + 300 * i, runner=Stub(f))[WID]
                for i, f in enumerate(frames)])[0]


check("observed: turns finishing under a stale banner are progress: no card",
      observed([turn_done(i) for i in range(6)]), [N] * 6)
check("observed: a held worker whose footer counters tick is still carded",
      observed(HELD), [N, N, HUMAN, N])

# The same worker when what ticks is ABOVE the composer: the done line's monitor count, or
# the status glyph the CLI cycles. Nothing finished, so the wait is one frame, not a swing.


def held_above(monitors, glyph="\u273b"):
    return framed(f"\u25cf Picked up the next delivery.\n{glyph} Worked for 4m 12s \u00b7 done 1:36 PM"
                  f" \u00b7 {monitors} monitor still running\n")


TICKING = [held_above(1), held_above(2), held_above(3), held_above(2)]
CYCLING = [held_above(1, g) for g in "\u273b\u2736\u2733\u273b"]
ADVANCING = [framed(f"\u25cf {w}.\n\u273b Worked for 4m 12s \u00b7 done 1:36 PM\n")
             for w in ("Read the inbox", "Drafted the reply", "Sent the reply", "Archived the task")]
check("a held worker whose only change above the composer is a counter is still carded",
      run([frame_obs(f) for f in TICKING])[0], [N, N, HUMAN, N])
check("a held worker whose only change above the composer is the status glyph is still carded",
      run([frame_obs(f) for f in CYCLING])[0], [N, N, HUMAN, N])
check("a worker whose turn text advances under the banner is not carded",
      run([frame_obs(f) for f in ADVANCING])[0], [N] * 4)
check("observed: a held worker whose counter above the composer ticks is still carded",
      observed(TICKING), [N, N, HUMAN, N])
check("observed: a worker whose turn text advances under the banner is not carded",
      observed(ADVANCING), [N] * 4)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — pool_supervision wedge rung")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
