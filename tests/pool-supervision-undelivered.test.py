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
import subprocess
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
    # One routed task, still live in tasks/: what the card is about.
    inbox = rem.pd.deliveries_dir(ws, WID)
    inbox.mkdir(parents=True)
    (inbox / "task-owed1.txt").write_bytes(b"")
    (ws / "tasks").mkdir()
    (ws / "tasks" / "task-owed1.txt").write_text("id: task-owed1\ntask: x\n")
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
leftover = pool(fenced)
(leftover / "tasks" / "task-owed1.txt").unlink()  # archived or gone: only its sentinel remains
out, _, _ = card(leftover, IDLE)
check("a seat owed only by a leftover sentinel (no live task file) is not carded",
      (out.get("outcome"), out.get("pane")), ("cleared", "no-live-task"))
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

# --- the owner clears the fence from the card (#6303) -------------------------------------
from hitl.schema import ActionReply, STATUS_EXPIRED, STATUS_RESOLVED  # noqa: E402


def fenced_card(name):
    sock = tmp / f"{name}.sock"
    tpk.prepare_guard(sock)
    (tpk.guard_path(sock) / "mutex").unlink()
    ws = pool(sock)
    out, t, req = card(ws, IDLE)
    return sock, ws, req, HitlManager(HitlStore(ws / "state" / "hitl" / "requirements"))


def press(m, req, action_id):
    m.apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision, action_id=action_id,
                               guard=req.guard))


class Runner(Tmux):
    """An idle pane, and a recovery that exits `rc` instead of running."""

    def __init__(self, rc=0):
        super().__init__(IDLE)
        self.rc = rc

    def __call__(self, argv, **kw):
        if "recover-episode" in argv:
            self.calls.append(list(argv))
            return type("Done", (), {"returncode": self.rc, "stdout": "", "stderr": ""})()
        return super().__call__(argv, **kw)

    def recoveries(self):
        return [c for c in self.calls if "recover-episode" in c]


def _real_with_idle_pane():
    """The real recovery, and an idle pane for the card raised after it refuses."""
    def run(argv, **kw):
        if "recover-episode" in argv:
            return subprocess.run(argv, **kw)
        return Tmux(IDLE)(argv, **kw)
    return run


def offered_again(m, why):
    """The one active card after a press that settled nothing: it still has the button, and says why."""
    active = m.active()
    return (len(active), wc.FENCE_ACTION in [a.id for a in active[0].actions] if active else None,
            why in active[0].message if active else None)


def recover_script(*args):
    return subprocess.run([sys.executable, str(REPO / "src" / "tmux_pane_keys.py"), *args],
                          capture_output=True, text=True, timeout=30)


def claimed_ticket(sock, name):
    """A send that claimed its ticket and never reported: the fence stands for this one."""
    tpk.prepare_guard(sock)
    d = tmp / f"tmux-pane-keys.{name}"
    d.mkdir()
    tpk.begin_guard(sock, d / "ticket")
    (d / "ticket").rename(d / "ticket.claimed")
    return d / "ticket"


sock, ws_f, req, m = fenced_card("fence-press")
check("the fence card offers Clear the send fence", [a.id for a in req.actions][:1], [wc.FENCE_ACTION])
check("and keeps the terminal recovery line", f"tmux_pane_keys.py recover {sock}" in req.message, True)
check("it stays a core-blocked card, so reminders keep running", req.kind, "core-blocked")
check("it records the fence it was raised for", (req.subject or {}).get("fence_episode"), tpk.fence_episode(sock))
r = Runner()
check("unpressed, nothing is cleared", (wc.drive_fence_clears(ws_f, runner=r, manager=m), r.recoveries()), ({}, []))
press(m, req, wc.FENCE_ACTION)
check("pressed on an uncertain fence, the real recovery runs and reports cleared",
      wc.drive_fence_clears(ws_f, manager=m), {req.id: "cleared"})
check("the fence then reads clear", tpk.fence_status(sock), "clear")
check("and the card is resolved", m.get(req.id).status, STATUS_RESOLVED)

sock, ws_f, req, m = fenced_card("fence-busy")
press(m, req, wc.FENCE_ACTION)
r = Runner()
with mock.patch.object(wc.tmux_pane_keys, "fence_status", return_value="busy"):
    out_busy = wc.drive_fence_clears(ws_f, runner=r, manager=m)
check("a send in flight at press time is refused and no recovery runs",
      (out_busy, r.recoveries()), ({req.id: "refused"}, []))
check("and that card expires", m.get(req.id).status, STATUS_EXPIRED)
check("but the owner is offered the button again", offered_again(m, "a send was in flight"), (1, True, True))

sock, ws_f, req, m = fenced_card("fence-cleared-meanwhile")
press(m, req, wc.FENCE_ACTION)
r = Runner()
with mock.patch.object(wc.tmux_pane_keys, "fence_status", return_value="clear"):
    out_clear = wc.drive_fence_clears(ws_f, runner=r, manager=m)
check("a fence already clear only closes the card", (out_clear, r.recoveries()), ({req.id: "already-clear"}, []))

# An old approval never settles a newer send (the real guard, no fake recovery).
sock, ws_f, req, m = fenced_card("fence-a-then-b")
press(m, req, wc.FENCE_ACTION)
check("fence A is recovered by other means", recover_script("recover", str(sock)).returncode, 0)
ticket_b = claimed_ticket(sock, "b")
check("a new interrupted send B stands", tpk.fence_status(sock), "uncertain")
check("the old approval is refused for B", wc.drive_fence_clears(ws_f, runner=_real_with_idle_pane(), manager=m),
      {req.id: "changed"})
check("B's pending record survives", (tpk.guard_path(sock) / "pending.json").exists(), True)
check("B's fence still stands", tpk.fence_status(sock), "uncertain")
check("the new card is for B", (m.active()[0].subject or {}).get("fence_episode"), str(ticket_b))
check("and says this is a newer send", offered_again(m, "newer interrupted send"), (1, True, True))

# The compare is under the recovery lock: a fence that changes after the status probe is refused there too.
sock_c = tmp / "fence-probe-race.sock"
ticket_c = claimed_ticket(sock_c, "c")
got = recover_script("recover-episode", str(sock_c), "/some/other/ticket")
check("recover-episode for another fence exits FENCE_CHANGED", got.returncode, tpk.FENCE_CHANGED)
check("and clears nothing", tpk.fence_episode(sock_c), str(ticket_c))
check("recover-episode with no fence named refuses too", recover_script("recover-episode", str(sock_c)).returncode,
      tpk.FENCE_CHANGED)
check("for the fence it names, it clears", recover_script("recover-episode", str(sock_c), str(ticket_c)).returncode, 0)
check("and the fence reads clear", tpk.fence_status(sock_c), "clear")

# A failed recovery leaves the owner a live card, and the retry works.
sock, ws_f, req, m = fenced_card("fence-recovery-fails")
press(m, req, wc.FENCE_ACTION)
check("a recovery that fails reports failed", wc.drive_fence_clears(ws_f, runner=Runner(rc=125), manager=m),
      {req.id: "failed"})
check("that card expires", m.get(req.id).status, STATUS_EXPIRED)
check("the fence is still there", tpk.fence_status(sock), "uncertain")
check("and the owner has a live card, saying it failed", offered_again(m, "recovery itself failed"), (1, True, True))
retry = m.active()[0]
press(m, retry, wc.FENCE_ACTION)
check("pressing that card on the next sweep clears the fence", wc.drive_fence_clears(ws_f, manager=m),
      {retry.id: "cleared"})
check("and the fence reads clear", tpk.fence_status(sock), "clear")

# The pane cannot be read when the replacement is due: the debt stays until a card exists.
class UnreadPane(Runner):
    def __call__(self, argv, **kw):
        if "capture-pane" in argv:
            return type("Done", (), {"returncode": 1, "stdout": "", "stderr": ""})()
        return super().__call__(argv, **kw)


sock, ws_f, req, m = fenced_card("fence-pane-unread")
press(m, req, wc.FENCE_ACTION)
check("a failed recovery with an unread pane reports failed",
      wc.drive_fence_clears(ws_f, runner=UnreadPane(rc=125), manager=m), {req.id: "failed"})
check("no replacement exists, so the old card is kept", [c.id for c in m.active()], [req.id])
check("and the fence still stands", tpk.fence_status(sock), "uncertain")
r = UnreadPane(rc=0)
check("still unread: still owed, and that press runs no second recovery",
      (wc.drive_fence_clears(ws_f, runner=r, manager=m), r.recoveries(), [c.id for c in m.active()]),
      ({req.id: "failed"}, [], [req.id]))
r = Runner(rc=0)
check("the pane reads again: the replacement is raised, with no recovery",
      (wc.drive_fence_clears(ws_f, runner=r, manager=m), r.recoveries()), ({req.id: "failed"}, []))
check("the old card is closed", m.get(req.id).status, STATUS_EXPIRED)
check("and the owner has a live card, saying it failed", offered_again(m, "recovery itself failed"), (1, True, True))
retry = m.active()[0]
press(m, retry, wc.FENCE_ACTION)
check("pressing it clears the fence", wc.drive_fence_clears(ws_f, manager=m), {retry.id: "cleared"})
check("and the fence reads clear", tpk.fence_status(sock), "clear")

# A marker-only fence keeps its identity through housekeeping, and a later marker is another fence.
sock_m = tmp / "fence-marker-only.sock"
tpk.prepare_guard(sock_m)
(tpk.guard_path(sock_m) / "uncertain").touch()
marker = tpk.fence_episode(sock_m)
tpk.refresh_guard(sock_m)
check("refresh_guard leaves the fence's identity alone", tpk.fence_episode(sock_m), marker)
check("recovery naming it clears it", recover_script("recover-episode", str(sock_m), marker).returncode, 0)
check("the marker-only fence reads clear", tpk.fence_status(sock_m), "clear")
(tpk.guard_path(sock_m) / "uncertain").touch()
check("a later marker is another fence", recover_script("recover-episode", str(sock_m), marker).returncode,
      tpk.FENCE_CHANGED)
check("and it stands", tpk.fence_status(sock_m), "uncertain")


# Every unreadable shape: a refused approval repairs nothing, and the right one recovers it.
def no_mutex(g):
    tpk.prepare_guard(Path(str(g).removesuffix(".pane-keys-lock")))
    (g / "mutex").unlink()


def mutex_directory(g):
    (g / "mutex").mkdir(parents=True)


def dangling(g):
    g.symlink_to(g.with_name(g.name + ".gone"))


def plain_file(g):
    g.write_text("x")


def link_to_a_file(g):
    g.with_name(g.name + ".f").write_text("x")
    g.symlink_to(g.with_name(g.name + ".f"))


def corrupt_pending(g):
    tpk.prepare_guard(Path(str(g).removesuffix(".pane-keys-lock")))
    (g / "pending.json").write_text("{")


def oversized_pending(g):
    tpk.prepare_guard(Path(str(g).removesuffix(".pane-keys-lock")))
    (g / "pending.json").write_text("x" * 5000)


def shape_of(g):
    return g.is_symlink(), g.is_dir(), (g / "mutex").is_file(), (g / "mutex").is_dir()


for make in (no_mutex, mutex_directory, dangling, plain_file, link_to_a_file, corrupt_pending, oversized_pending):
    n = make.__name__
    sock_s, guard_s = tmp / f"shape-{n}.sock", tpk.guard_path(tmp / f"shape-{n}.sock")
    make(guard_s)
    check(f"{n}: the fence stands", tpk.fence_status(sock_s), "uncertain")
    ep, shape = tpk.fence_episode(sock_s), shape_of(guard_s)
    check(f"{n}: another episode is refused", recover_script("recover-episode", str(sock_s), "other").returncode,
          tpk.FENCE_CHANGED)
    check(f"{n}: nothing was repaired", (tpk.fence_status(sock_s), shape_of(guard_s), tpk.fence_episode(sock_s)),
          ("uncertain", shape, ep))
    check(f"{n}: its own episode recovers", recover_script("recover-episode", str(sock_s), ep).returncode, 0)
    check(f"{n}: and it reads clear", tpk.fence_status(sock_s), "clear")

# Two missing-mutex fences are different fences.
sock_a = tmp / "fence-missing-twice.sock"
no_mutex(tpk.guard_path(sock_a))
ep_a = tpk.fence_episode(sock_a)
check("missing-mutex A is recovered by other means", recover_script("recover", str(sock_a)).returncode, 0)
(tpk.guard_path(sock_a) / "mutex").unlink()
check("A's approval is refused for B", recover_script("recover-episode", str(sock_a), ep_a).returncode,
      tpk.FENCE_CHANGED)
check("and B is not repaired", (tpk.guard_path(sock_a) / "mutex").exists(), False)
sock_n = tmp / "fence-no-guard.sock"
check("with no guard there is no fence to approve", recover_script("recover-episode", str(sock_n), "x").returncode,
      tpk.FENCE_CHANGED)
check("and none is created", os.path.lexists(tpk.guard_path(sock_n)), False)

out, _, req = card(pool(tmp / "unfenced.sock"), IDLE)
check("a card with no fence offers no clear button", wc.FENCE_ACTION in [a.id for a in req.actions], False)

print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURE(S)'} — undelivered work escalates")
for f in fails:
    print("   ", f)
sys.exit(1 if fails else 0)
