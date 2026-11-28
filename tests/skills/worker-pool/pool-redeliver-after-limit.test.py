#!/usr/bin/env python3
"""A seat whose limit hold lifts while it owes deliveries gets them re-offered once.

A task watcher announces a payload once per watcher lifetime, so deliveries that reached a
seat while its provider refused every turn are never announced again after the limit lifts:
the seat idles, owing them, until someone sends it something new. So:
- the ladder reads "the seat's own hold was released, and it is idle, owing, watcher live"
  as `redeliver` until that release's wake task is queued, and never while the hold is in force
  or the pane is not idle; a refused remedy leaves the release eligible;
- the undelivered card and the redelivery are decided and retried independently, so a failing
  card post never holds back the wake task, nor a failing queue write the card;
- the remedy writes one fresh task through pool_ask (the front door a watcher announces),
  types nothing into the pane, and refuses a pane that is not idle; one task id per release,
  so a retry after an unclear outcome queues nothing twice.
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


def decide(state, observations, now):
    """(state, decisions, workers due a redelivery) for one tick."""
    st, d = ps.evaluate(state, observations, now)
    if hasattr(ps, "redeliveries"):
        return st, d, ps.redeliveries(st, observations, now)
    red = [w for w, x in d.items() if x == REDELIVER]
    return st, {w: (ps.NOTHING if x == REDELIVER else x) for w, x in d.items()}, red


def act(decisions, red, runner):
    """The sweep's apply: the decisions, and the redeliveries beside them."""
    if "redeliveries" in rem.apply.__code__.co_varnames:
        return rem.apply(ws, REPO, decisions, redeliveries=red, runner=runner)
    merged = {w: (REDELIVER if w in red and x == ps.NOTHING else x) for w, x in decisions.items()}
    return rem.apply(ws, REPO, merged, runner=runner)


def tick_entry(decision, redeliver):
    """A tick as one value: the decision, REDELIVER alone, or both as a pair."""
    if not redeliver:
        return decision
    return REDELIVER if decision == ps.NOTHING else (decision, REDELIVER)


def run(observations, start=RELEASED + 60, state=None, served=True):
    """`served`: each redelivery's remedy queued its wake task, as the sweep's apply records it."""
    st, out, t = state or ps.SupervisionState(), [], start
    for o in observations:
        st, d, red = decide(st, {"w": o}, t)
        out.append(tick_entry(d["w"], "w" in red))
        if d["w"] == ps.CARD_UNDELIVERED:
            st = ps.acknowledge(st, ["w"])
        if "w" in red and served:
            st = ps.acknowledge_redelivered(st, {"w": o.limit_released_at})
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
# By then the seat has idled owing past the card threshold, so its due card shares the first tick.
nxt, _ = run([obs(released=later - 30)] * 2, start=later, state=st)
check("a later release is a new episode", nxt, [(ps.CARD_UNDELIVERED, REDELIVER), ps.NOTHING])
check("a release whose remedy did not queue stays eligible", run([obs()] * 2, served=False)[0],
      [REDELIVER, REDELIVER])

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

# Not carded yet and the card is due: the card and the redelivery are both decided that tick.
pre, st = run([obs(released=None)] * ticks, start=RELEASED - ticks * ps.SAMPLE_PERIOD_S)
check("not yet carded before the release", ps.CARD_UNDELIVERED in pre, False)
post, st = run([obs()] * 2, state=st)
check("a due card and the redelivery share the tick", post, [(ps.CARD_UNDELIVERED, REDELIVER), ps.NOTHING])

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
        return {"task_id": kw.get("task_id"), "to": to}

    ask_mod = rem._sibling("pool_ask")
    with mock.patch.object(ask_mod, "ask", side_effect=fake_ask):
        t = Tmux(IDLE)
        out = redeliver(ws, REPO, WID, runner=t)
        check("an idle owing seat gets one fresh task", (out.get("outcome"), out.get("task_id")),
              ("redelivered", rem.redeliver_task_id(WID, RELEASED)))
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
        acted = act({WID: ps.NOTHING}, [WID], None)
    check("apply routes a redelivery to the remedy",
          (rd.call_count, WID in acted.get("redeliveries", acted.get("rearms", {}))), (1, True))
    with mock.patch.object(rem, "redeliver") as rd, mock.patch.object(rem, "suspension", return_value="quit"):
        act({WID: ps.NOTHING}, [WID], None)
    check("a suspended pool redelivers nothing", rd.call_count, 0)

# --- refusal, then idle: the release is served only once its wake task is queued -----------
if callable(redeliver):
    queued = []

    def durable_ask(workspace, to, question, **kw):
        tid = kw.get("task_id") or f"task-fresh{len(queued) + 1}"
        (Path(workspace) / "tasks" / f"{tid}.txt").write_text(f"id: {tid}\ntask: wake\n")
        queued.append(tid)
        return {"task_id": tid, "to": to}

    def full_tick(pane_text, state):
        """One tick as the sweep runs it: decide, persist, remedy, then read back what was kept."""
        st, d, red = decide(state, {WID: obs()}, state.last_sample_at + ps.SAMPLE_PERIOD_S
                            if state.last_sample_at else RELEASED + 60)
        sup.save_state(ws, st)
        acted = act(d, red, Tmux(pane_text))
        done = acted.get("redeliveries", acted.get("rearms", {})).get(WID, {}).get("outcome")
        return d[WID], WID in red, done, acted.get("cards", {}).get(WID, {}).get("outcome"), sup.load_state(ws)

    def sweep(pane_text, state):
        d, red, done, _, st = full_tick(pane_text, state)
        return tick_entry(d, red), done, st

    hold(ws, status="cleared", released_at=RELEASED, cleared_by="cli-pane")
    sup.save_state(ws, ps.SupervisionState())
    with mock.patch.object(ask_mod, "ask", side_effect=durable_ask):
        first = sweep(LIMIT, ps.SupervisionState())
        check("a refused remedy reports the refusal", first[:2], (REDELIVER, "pane-not-idle"))
        second = sweep(IDLE, first[2])
        check("the same release is decided again once the pane is idle", second[:2], (REDELIVER, "redelivered"))
        check("and exactly one wake task is queued", len(queued), 1)
        third = sweep(IDLE, second[2])
        check("a served release is not decided again", third[0], ps.NOTHING)
        again = redeliver(ws, REPO, WID, runner=Tmux(IDLE))
        check("a second remedy for the same release queues nothing more",
              (again.get("outcome"), len(queued)), ("redelivered", 1))

    # An uncertain write: the task landed but the call raised. The retry finds it and asks no more.
    for p in (ws / "tasks").glob("task-redeliver-*.txt"):
        p.unlink()
    queued.clear()
    sup.save_state(ws, ps.SupervisionState())

    def lands_then_fails(workspace, to, question, **kw):
        durable_ask(workspace, to, question, **kw)
        raise OSError("route failed after the write")

    with mock.patch.object(ask_mod, "ask", side_effect=lands_then_fails):
        first = sweep(IDLE, ps.SupervisionState())
    check("an uncertain write is not marked served", first[:2], (REDELIVER, "delivery-failed"))
    with mock.patch.object(ask_mod, "ask", side_effect=durable_ask):
        second = sweep(IDLE, first[2])
    check("its retry finds the queued wake task and asks no more", (second[1], len(queued)), ("redelivered", 1))
    check("then the release is served", sweep(IDLE, second[2])[0], ps.NOTHING)

    # The real pool_ask writes the wake task under the release's own id, and refuses a bad one.
    try:
        ask_mod.ask(ws, WID, "x", allow_handoff=False, task_id="not/a/task")
        check("pool_ask refuses an id that is not a task id", "accepted", "refused")
    except ValueError:
        check("pool_ask refuses an id that is not a task id", "refused", "refused")
    hold(ws, status="cleared", released_at=RELEASED + 7, cleared_by="cli-pane")
    real = redeliver(ws, REPO, WID, runner=Tmux(IDLE))
    wake = rem.redeliver_task_id(WID, RELEASED + 7)
    check("end to end, the wake task is queued under that id",
          (real.get("outcome"), real.get("task_id"), (ws / "tasks" / f"{wake}.txt").is_file()),
          ("redelivered", wake, True))

    # The card and the wake task retry independently: a failure of one never holds the other back.
    span = ticks + 4
    baseline, _ = run([obs(released=None)] * span)
    due = baseline.index(ps.CARD_UNDELIVERED) if ps.CARD_UNDELIVERED in baseline else None

    def scenario(card_ok, ask_ok):
        """`card_ok(i)` / `ask_ok(i)`: whether the card post / queue write succeeds on tick i."""
        for p in (ws / "tasks").glob("task-redeliver-*.txt"):
            p.unlink()
        hold(ws, status="cleared", released_at=RELEASED, cleared_by="cli-pane")
        sup.save_state(ws, ps.SupervisionState())
        queued.clear()
        posts, ticks_seen, state, i = [], [], ps.SupervisionState(), [0]

        def card(workspace, worker_id, which, **kw):
            posts.append((i[0], which, card_ok(i[0])))
            return {"worker_id": worker_id, "outcome": "carded" if card_ok(i[0]) else "failed"}

        def ask(workspace, to, question, **kw):
            if not ask_ok(i[0]):
                raise OSError("queue write failed")
            return durable_ask(workspace, to, question, **kw)

        with mock.patch.object(ask_mod, "ask", side_effect=ask), \
                mock.patch.object(wc, "raise_card", side_effect=card):
            for n in range(span):
                i[0] = n
                d, red, done, carded, state = full_tick(IDLE, state)
                ticks_seen.append((d, red, done))
        return posts, ticks_seen

    posts, seen = scenario(card_ok=lambda n: False, ask_ok=lambda n: n > due)
    check("card post always fails: the card is retried from its threshold",
          sorted({n for n, _, _ in posts}) == list(range(due, span)), True)
    check("and the wake is retried past the threshold, queued once the write works",
          (len(queued), [n for n, (_, _, done) in enumerate(seen) if done == "redelivered"]), (1, [due + 1]))
    check("after which the release is served", [r for _, r, _ in seen[due + 2:]], [False] * (span - due - 2))

    posts, seen = scenario(card_ok=lambda n: True, ask_ok=lambda n: False)
    check("queue always fails: the card is raised once, at its threshold",
          [(n, w) for n, w, _ in posts], [(due, ps.CARD_UNDELIVERED)])
    check("and the wake is retried on every tick", all(r for _, r, _ in seen), True)
    check("with nothing queued", len(queued), 0)

    posts, seen = scenario(card_ok=lambda n: True, ask_ok=lambda n: True)
    check("both succeed: one card and one wake",
          ([(n, w) for n, w, _ in posts], len(queued), [n for n, (_, _, done) in enumerate(seen) if done]),
          ([(due, ps.CARD_UNDELIVERED)], 1, [0]))

    with mock.patch.object(sup, "limit_released_at", return_value=None):
        check("no release to serve: nothing is sent", redeliver(ws, REPO, WID, runner=Tmux(IDLE)).get("outcome"),
              "no-release")

if fails:
    print(f"\nFAIL: {len(fails)}")
    for f in fails:
        print("  " + f)
    sys.exit(1)
print("\nPASS")
