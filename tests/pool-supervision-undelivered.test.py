#!/usr/bin/env python3
"""An idle seat that owes work while its watcher beats is escalated, with the cause named.

Three worker seats once sat idle for up to an hour with a task routed to each, a live
watcher, and the supervisor deciding "nothing": typed delivery was refused because the
tmux socket's pane-keys guard had lost its mutex to macOS's nightly /tmp cleaner. So:
- the ladder reads idle + owed + live watcher as "undelivered" and asks for a card after
  UNDELIVERED_AFTER_S, once per episode; a pane that starts working clears it;
- the supervisor keeps the guard's files fresh and names a standing fence as the block;
- the card names the fence and its documented recovery, and never acts on the session;
- refreshing the guard never creates, clears or replaces anything.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
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
import tmux_pane_keys as tpk  # noqa: E402
from hitl.manager import HitlManager, HitlStore  # noqa: E402

fails = []


def check(name, got, want):
    ok = got == want
    print(f"  {'ok ' if ok else 'BAD'} {name}: {got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


def obs(pane=ps.PANE_IDLE, owed=True, watcher=ps.LIVE, block=None, pane_id="same"):
    return ps.Observation(beat=ps.LIVE, session_alive=True, watcher_beat=watcher, watcher_held=True,
                          work_outstanding=owed, pane=pane, pane_id=pane_id, delivery_block=block)


def run(observations, start=1000.0, step=ps.SAMPLE_PERIOD_S):
    st, out, t = ps.SupervisionState(), [], start
    for o in observations:
        st, d = ps.evaluate(st, {"w": o}, t)
        out.append(d["w"])
        if d["w"] in (ps.CARD_UNDELIVERED, ps.CARD_HUMAN):
            st = ps.acknowledge(st, ["w"])
        t += step
    return out


# --- the ladder ---------------------------------------------------------------------------
ticks = int(ps.UNDELIVERED_AFTER_S // ps.SAMPLE_PERIOD_S)
got = run([obs()] * (ticks + 3))
check("idle + owed + live watcher: nothing before UNDELIVERED_AFTER_S", got[:ticks], [ps.NOTHING] * ticks)
check("then one card", got[ticks], ps.CARD_UNDELIVERED)
check("and no repeat once it was raised", got[ticks + 1:], [ps.NOTHING] * 2)
check("an idle seat that owes nothing is healthy", set(run([obs(owed=False)] * (ticks + 2))), {ps.NOTHING})
check("unknown work is never escalated", set(run([obs(owed=None)] * (ticks + 2))), {ps.NOTHING})
check("a stale watcher is the watcher rung's case, not this one",
      ps.CARD_UNDELIVERED in run([obs(watcher=ps.STALE)] * (ticks + 2)), False)
# A working pane whose frame moved is progress (an unmoved one would be "stuck").
cleared = run([obs()] * ticks + [obs(pane=ps.PANE_WORKING, pane_id="moved")] + [obs()] * ticks)
check("a pane that starts working ends the episode; the clock restarts",
      ps.CARD_UNDELIVERED in cleared, False)

# Its clock starts when the seat becomes idle-and-owing, not when an earlier wedge began.
after_gate = run([obs(pane=ps.PANE_GATE)] * 3 + [obs()] * (ticks + 1))
check("a gate's card, then idle: no undelivered card until a full UNDELIVERED_AFTER_S idle",
      after_gate[3:], [ps.NOTHING] * ticks + [ps.CARD_UNDELIVERED])

# A pause is not time a delivery was refused: the clock starts over on resume.
paused = ps.Observation(beat=ps.LIVE, session_alive=True, watcher_beat=ps.LIVE, watcher_held=True,
                        work_outstanding=True, pane=ps.PANE_IDLE, pane_id="same", paused=True)
resumed = run([obs(), paused, paused] + [obs()] * (ticks + 1))
check("paused time does not age the undelivered clock",
      resumed[3:], [ps.NOTHING] * ticks + [ps.CARD_UNDELIVERED])
gate_kept = ps.evaluate(ps.SupervisionState(workers={"w": ps.WorkerEvidence(
    wedge_kind="human", wedge_consecutive=2, wedge_first_detected_at=1.0)}), {"w": paused}, 1000.0)[0]
check("a pause leaves another ladder's evidence exactly as it was",
      (gate_kept.workers["w"].wedge_kind, gate_kept.workers["w"].wedge_consecutive), ("human", 2))

# --- the guard: kept fresh, never recreated -----------------------------------------------
tmp = Path(tempfile.mkdtemp(prefix="pool-undelivered-"))
sock = tmp / "tmux.sock"
mutex = tpk.prepare_guard(sock)
guard = tpk.guard_path(sock)
old = time.time() - 5 * 86400
(guard / "uncertain").write_text("")
for f in (mutex, guard / "uncertain"):
    os.utime(f, (old, old))
check("refresh touches every regular file in the guard", tpk.refresh_guard(sock), 2)
check("so the mutex is no longer old enough for tmp_cleaner", time.time() - mutex.stat().st_mtime < 60, True)
check("and a standing fence marker stays (it is refreshed, not cleared)", (guard / "uncertain").exists(), True)
os.utime(mutex, (old, old))
tpk.prepare_guard(sock)
check("a send refreshes the mutex too", time.time() - mutex.stat().st_mtime < 60, True)
(guard / "uncertain").unlink()
mutex.unlink()
check("refresh never recreates a lost mutex", (tpk.refresh_guard(sock), mutex.exists()), (0, False))
check("a guard that lost its mutex reads as a delivery block", sup.delivery_block(sock), "pane-key-fence")
check("and reading it still creates nothing", mutex.exists(), False)
clean = tmp / "clean.sock"
check("no guard at all is no block", sup.delivery_block(clean), None)
check("refresh of an absent guard is a no-op", tpk.refresh_guard(tmp / "absent.sock"), 0)

fresh = tmp / "fresh.sock"
tpk.prepare_guard(fresh)
with mock.patch.object(tpk.os, "utime", side_effect=OSError("read-only")):
    check("a file that cannot be touched is skipped, not raised", tpk.refresh_guard(fresh), 0)

buf = io.StringIO()
with mock.patch.object(sup, "tick", return_value={
        "decisions": {}, "observations": {}, "not_supervised": [], "auth_expired": [], "resumed": False,
        "routing": {"alarm": None}, "delivery_blocks": {"5d6e7f80aaaa": "pane-key-fence"}}), \
        contextlib.redirect_stdout(buf):
    sup.main(["--workspace", str(tmp), "--sweep"])
check("the sweep's text report names a blocked seat and its cause",
      "5d6e7f80  typed delivery is blocked (pane-key-fence)" in buf.getvalue(), True)

# --- the card -----------------------------------------------------------------------------
WID = "5d6e7f8091a2b3c4d5e6f708192a3b4c"
NAME = f"sutando-worker-{WID}"
FOOTER = "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"


class Tmux:
    ACTING = ("kill-session", "send-keys", "new-session", "respawn-pane", "respawn-window")

    def __init__(self, pane):
        self.pane, self.calls = pane, []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out = self.pane if "capture-pane" in argv else ""
        return type("Done", (), {"returncode": 0, "stdout": out, "stderr": ""})()


def pool(socket):
    ws = Path(tempfile.mkdtemp(prefix="pool-undelivered-ws-"))
    (ws / "state").mkdir()
    (ws / "state" / "roster.json").write_text(json.dumps({"workers": {WID: {"state": "live", "label": "rev"}}}))
    wi.record_session(ws, WID, "s1", runtime="claude", relation=wi.RELATION_NEW)
    wi.start_incarnation(ws, WID, "s1", tmux_socket=str(socket), tmux_session=NAME)
    return ws


def card(ws, pane):
    t = Tmux(pane)
    m = HitlManager(HitlStore(ws / "state" / "hitl" / "requirements"))
    out = wc.raise_card(ws, WID, ps.CARD_UNDELIVERED, runner=t, routed=lambda *a: False, manager=m)
    return out, t, (m.get(out["hitl_id"]) if out.get("hitl_id") else None)


IDLE = f"❯ \n{FOOTER}\n"
fenced = tmp / "fenced.sock"
tpk.prepare_guard(fenced)
(tpk.guard_path(fenced) / "mutex").unlink()
out, t, req = card(pool(fenced), IDLE)
check("an idle owing seat behind a fence is carded", out.get("outcome"), "carded")
text = req.prompt if hasattr(req, "prompt") else json.dumps(req.__dict__, default=str)
check("the card names the fence", "pane-keys fence" in text, True)
check("and its documented recovery for this socket", f"tmux_pane_keys.py recover {fenced}" in text, True)
check("the card records the cause", (req.subject or {}).get("cause"), ["pane-key-fence"])
check("raising it never acts on the session", [c for c in t.calls if any(a in c for a in Tmux.ACTING)], [])
out, _, req = card(pool(tmp / "unblocked.sock"), IDLE)
check("with no fence the card says the cause is unidentified",
      "not identified" in (req.prompt if hasattr(req, "prompt") else str(req.__dict__)), True)
out, _, _ = card(pool(fenced), f"✻ Thinking… (12s · esc to interrupt)\n❯ \n{FOOTER}\n")
check("a seat that is working again is not carded", out.get("outcome"), "cleared")
AGENT = (f"❯ \n{FOOTER}\n  ⏺ main\n"
         "  ◯ general-purpose  Checking a mutant in the retire path                37m 31s · ↓ 159.5k tokens\n")
out, _, _ = card(pool(fenced), AGENT)
check("a seat whose background agent still runs is not carded", (out.get("outcome"), out.get("pane")),
      ("cleared", "background-agent"))
out, _, req = card(pool(fenced), IDLE)
check("the card's words are diagnostic, not a claim", "appears not to have been delivered" in json.dumps(req.__dict__, default=str), True)

# A pause that lands between the sweep and the remedy stops the card, as it does a gate's.
ws_p = pool(fenced)
raised = []
with mock.patch.object(rem.sup, "is_paused", return_value=True), \
        mock.patch.object(rem.wc, "raise_card", side_effect=lambda *a, **k: raised.append(a) or {}):
    rem.apply(ws_p, str(REPO), {WID: ps.CARD_UNDELIVERED}, runner=Tmux(IDLE))
check("a seat paused after the sweep is not carded", raised, [])

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — undelivered work escalates")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
