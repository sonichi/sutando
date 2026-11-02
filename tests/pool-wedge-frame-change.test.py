#!/usr/bin/env python3
"""A busy worker whose screen moves is never read as frozen.

"Stuck" means the same frame id on consecutive ticks, so the id must identify the
FRAME. tmux's own pane id (`%12`) is stable for the pane's life: wired into
`pane_id`, every busy worker would read as frozen and get a card on every poll.
This drives the real observe -> evaluate path over ticks where the screen moves
and asserts no wedge; the control fixes the id to a constant, as that mistake
would, and asserts the same ticks then DO read as frozen, so the first assertion
is the one that would catch it.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sup = _load("pool_supervise")
ps, wi, pd, cw = sup.ps, sup.wi, sup.pd, sup.cw

WID = "w1"
SOCK = "/tmp/pool-wedge-frame.sock"
FOOTER = "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"


class Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def pool():
    ws = Path(tempfile.mkdtemp(prefix="pool-wedge-frame-"))
    (ws / "state").mkdir()
    (ws / "state" / "roster.json").write_text(json.dumps(
        {"workers": {WID: {"state": "live", "label": WID}}}))
    wi.record_session(ws, WID, "s1", runtime="claude", relation=wi.RELATION_NEW)
    wi.start_incarnation(ws, WID, "s1", tmux_socket=SOCK, tmux_session=wi.tmux_session_name(WID))
    pd.deliveries_dir(ws, WID).mkdir(parents=True)
    (pd.deliveries_dir(ws, WID) / "task-owed.txt").write_bytes(b"")
    return ws


def ticks(ws, n=6):
    """n real ticks 300 s apart over a busy pane whose timer moves each time."""
    seen = {"i": 0}

    def runner(argv, **kw):
        if "capture-pane" in argv:
            seen["i"] += 1
            return Done(0, f"✻ Thinking… ({seen['i'] * 300}s · esc to interrupt)\n❯ \n{FOOTER}\n")
        if "inbox-holders" in argv:
            return Done(0, "4242 session\n")
        return Done(0)
    out, now = [], 50_000.0
    for _ in range(n):
        t = sup.tick(ws, now, runner=runner)
        out.append((t["decisions"][WID], t["wedged"]))
        now += 300.0
    return out


class AMovingScreenIsNotFrozen(unittest.TestCase):
    def test_a_changed_frame_every_tick_never_wedges(self):
        self.assertEqual(ticks(pool()), [(ps.NOTHING, [])] * 6)

    def test_control_a_constant_id_would_read_every_busy_worker_as_frozen(self):
        orig = cw.raw_state_id
        cw.raw_state_id = lambda text: "%12"
        try:
            got = ticks(pool())
        finally:
            cw.raw_state_id = orig
        self.assertIn(ps.CARD_FROZEN, [d for d, _ in got])
        self.assertEqual(got[-1][1], [WID])


if __name__ == "__main__":
    unittest.main()
