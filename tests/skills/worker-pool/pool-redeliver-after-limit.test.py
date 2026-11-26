#!/usr/bin/env python3
"""A seat whose limit hold lifts while it owes deliveries gets them re-offered once.

A task watcher announces a payload once per watcher lifetime, so deliveries that reached a
seat while its provider refused every turn are never announced again after the limit lifts:
the seat idles, owing them, until someone sends it something new. So:
- the ladder reads "the seat's own hold was released, and it is idle, owing, watcher live"
  as `redeliver`, once per release, and never while the hold is in force or the pane is not idle;
- the undelivered card still goes out on a later tick if the seat keeps idling;
- the remedy writes one fresh task through pool_ask (the front door a watcher announces),
  types nothing into the pane, and refuses a pane that is not idle.
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rem = _load("pool_remedy")
sup, ps, wi, wc = rem.sup, rem.ps, rem.wi, rem.wc

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {name}: {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


REDELIVER = getattr(ps, "REDELIVER", "redeliver")
RELEASED = 5000.0


def obs(pane=ps.PANE_IDLE, owed=True, watcher=ps.LIVE, released=RELEASED, paused=False):
    fields = dict(beat=ps.LIVE, session_alive=True, watcher_beat=watcher, watcher_held=True,
                  work_outstanding=owed, pane=pane, pane_id="same", paused=paused)
    if "limit_released_at" in ps.Observation.__dataclass_fields__:
        fields["limit_released_at"] = released
    return ps.Observation(**fields)


def run(observations, start=RELEASED + 60, state=None):
    st, out, t = state or ps.SupervisionState(), [], start
    for o in observations:
        st, d = ps.evaluate(st, {"w": o}, t)
        out.append(d["w"])
        if d["w"] == ps.CARD_UNDELIVERED:
            st = ps.acknowledge(st, ["w"])
        t += ps.SAMPLE_PERIOD_S
    return out, st


# --- the ladder ---------------------------------------------------------------------------
got, st = run([obs()] * 3)
check("a released hold over an idle owing seat redelivers on the first tick", got[0], REDELIVER)
check("and only once for that release", got[1:], [ps.NOTHING] * 2)
later = st.last_sample_at + ps.SAMPLE_PERIOD_S
again, _ = run([obs()], start=later, state=st)
check("a saved ladder remembers the release it already served", REDELIVER in again, False)
# A sample gap no longer than the sweep's period, so the tick is evidence and not a host resume.
nxt, _ = run([obs(released=later - 30)], start=later, state=st)
check("a later release is a new episode", nxt, [REDELIVER])

check("no release on record: nothing", run([obs(released=None)])[0], [ps.NOTHING])
check("a pane still at its limit is never redelivered to",
      REDELIVER in run([obs(pane=ps.PANE_LIMIT)] * 2)[0], False)
check("a pane at a gate is not redelivered to", REDELIVER in run([obs(pane=ps.PANE_GATE)] * 2)[0], False)
check("a working pane is not redelivered to", REDELIVER in run([obs(pane=ps.PANE_WORKING)])[0], False)
check("owing nothing: nothing", run([obs(owed=False)])[0], [ps.NOTHING])
check("unknown work: nothing", run([obs(owed=None)])[0], [ps.NOTHING])
check("no live watcher to announce it: not redelivered",
      REDELIVER in run([obs(watcher=ps.STALE)])[0], False)
check("paused: nothing", run([obs(paused=True)])[0], [ps.NOTHING])
check("a release older than the window is not this episode",
      run([obs()], start=RELEASED + ps.REDELIVER_WINDOW_S + 1)[0] if hasattr(ps, "REDELIVER_WINDOW_S")
      else ["missing"], [ps.NOTHING])

# The limit episode came first; its undelivered card was already raised before the release.
ticks = int(ps.UNDELIVERED_AFTER_S // ps.SAMPLE_PERIOD_S)
pre, st = run([obs(released=None)] * (ticks + 2), start=RELEASED - (ticks + 2) * ps.SAMPLE_PERIOD_S)
check("before the release the seat was carded as undelivered", ps.CARD_UNDELIVERED in pre, True)
post, st = run([obs()] * 2, state=st)
check("an already-raised card does not stop the redelivery", post, [REDELIVER, ps.NOTHING])

# Not carded yet: the redelivery comes first, and the card still follows if it does not help.
pre, st = run([obs(released=None)] * ticks, start=RELEASED - ticks * ps.SAMPLE_PERIOD_S)
check("not yet carded before the release", ps.CARD_UNDELIVERED in pre, False)
post, st = run([obs()] * 2, state=st)
check("redelivery takes the tick a card would have", post[0], REDELIVER)
check("and the card goes out if the seat still idles owing", post[1], ps.CARD_UNDELIVERED)

# --- the observation: the seat's own hold record ---------------------------------------------
WID = "5d6e7f8091a2b3c4d5e6f708192a3b4c"
NAME = f"sutando-worker-{WID}"


def pool():
    ws = Path(tempfile.mkdtemp(prefix="pool-redeliver-ws-"))
    (ws / "state").mkdir()
    (ws / "state" / "roster.json").write_text(json.dumps({"workers": {WID: {"state": "live", "label": "rev"}}}))
    wi.record_session(ws, WID, "s1", runtime="claude", relation=wi.RELATION_NEW)
    wi.start_incarnation(ws, WID, "s1", tmux_socket=str(ws / "tmux.sock"), tmux_session=NAME)
    inbox = rem.pd.deliveries_dir(ws, WID)
    inbox.mkdir(parents=True)
    (inbox / "task-owed1.txt").write_bytes(b"")
    (ws / "tasks").mkdir()
    (ws / "tasks" / "task-owed1.txt").write_text("id: task-owed1\ntask: x\n")
    return ws


def hold(ws, **rec):
    (ws / "state" / f"core-limit-hold.{NAME}.json").write_text(json.dumps(rec))


ws = pool()
reader = getattr(sup, "limit_released_at", None)
check("the supervisor reads a seat's hold", callable(reader), True)
if callable(reader):
    check("no hold record: None", reader(ws, WID), None)
    hold(ws, status="held", until=9e9)
    check("a hold in force: None", reader(ws, WID), None)
    hold(ws, status="cleared", released_at=RELEASED, cleared_by="cli-pane")
    check("a cleared hold: its release time", reader(ws, WID), RELEASED)

import core_limit_hold
for exc in (ImportError, OSError, ValueError):
    with mock.patch.object(core_limit_hold, "read_hold", side_effect=exc("x")):
        check(f"an unreadable hold ({exc.__name__}): None", sup.limit_released_at(ws, WID), None)

# Persisted between ticks, so the sweep and a delivery-time check share one episode.
saved = ps.SupervisionState(workers={"w": ps.WorkerEvidence(
    **({"redelivered_for": RELEASED} if "redelivered_for" in ps.WorkerEvidence.__dataclass_fields__ else {}))})
sup.save_state(ws, saved)
check("the served release survives save and load",
      getattr(sup.load_state(ws).workers["w"], "redelivered_for", None), RELEASED)

# --- the remedy ---------------------------------------------------------------------------
FOOTER = "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
IDLE = f"❯ \n{FOOTER}\n"
LIMIT = ("⎿  You've hit your limit · resets 3pm (America/Los_Angeles)\n"
         "     /upgrade to increase your usage limit.\n\n❯ \n" + FOOTER + "\n")


class Tmux:
    ACTING = ("kill-session", "send-keys", "new-session", "respawn-pane", "paste-buffer", "load-buffer")

    def __init__(self, pane):
        self.pane, self.calls = pane, []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out = self.pane if "capture-pane" in argv else ""
        return type("Done", (), {"returncode": 0, "stdout": out, "stderr": ""})()


redeliver = getattr(rem, "redeliver", None)
check("the remedy has a redeliver action", callable(redeliver), True)
if callable(redeliver):
    asked = []

    def fake_ask(workspace, to, question, **kw):
        asked.append((to, kw.get("allow_handoff")))
        return {"task_id": "task-fresh1", "to": to}

    ask_mod = rem._sibling("pool_ask")
    with mock.patch.object(ask_mod, "ask", side_effect=fake_ask):
        t = Tmux(IDLE)
        out = redeliver(ws, REPO, WID, runner=t)
        check("an idle owing seat gets one fresh task", (out.get("outcome"), out.get("task_id")),
              ("redelivered", "task-fresh1"))
        check("through pool_ask's folder path, to that worker", asked, [(WID, False)])
        check("and nothing is typed into the pane", [c for c in t.calls if any(a in c for a in Tmux.ACTING)], [])

        asked.clear()
        out = redeliver(ws, REPO, WID, runner=Tmux(LIMIT))
        check("a pane that still shows a limit is refused", (out.get("outcome"), asked),
              ("pane-not-idle", []))

        (ws / "tasks" / "task-owed1.txt").unlink()
        out = redeliver(ws, REPO, WID, runner=Tmux(IDLE))
        check("only a leftover sentinel owed: nothing is sent", (out.get("outcome"), asked),
              ("no-work-or-paused", []))

    (ws / "tasks" / "task-owed1.txt").write_text("id: task-owed1\ntask: x\n")
    with mock.patch.object(sup, "_open_tmux", return_value=(None, None)):
        out = redeliver(ws, REPO, WID, runner=Tmux(IDLE))
    check("a seat with no tmux session is unsupported", out.get("outcome"), "unsupported-or-no-session")

    with mock.patch.object(ask_mod, "ask", side_effect=OSError("disk full")):
        out = redeliver(ws, REPO, WID, runner=Tmux(IDLE))
    check("a failed delivery write is reported, not raised",
          (out.get("outcome"), out.get("why")), ("delivery-failed", "disk full"))

    with mock.patch.object(rem, "redeliver", return_value={"outcome": "redelivered"}) as rd, \
            mock.patch.object(rem.sup, "acknowledge_cards"):
        acted = rem.apply(ws, REPO, {WID: REDELIVER})
    check("apply routes the decision to the remedy", (rd.call_count, WID in acted.get("rearms", {})), (1, True))

if fails:
    print(f"\nFAIL: {len(fails)}")
    for f in fails:
        print("  " + f)
    sys.exit(1)
print("\nPASS")
