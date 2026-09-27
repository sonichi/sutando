#!/usr/bin/env python3
"""Two properties of the pool tick that a single call cannot show.

1. What a worker owes is resolved against the result archive in ONE index pass
   (task_dispatch.ready_result_filenames), not one lookup per owned task: a live
   inbox keeps hundreds of answered sentinels. A counting spy pins it.
2. A wedge card is acknowledged only once it exists. The tick persists its state
   before the remedy raises the card, so an episode marked escalated at decision
   time would stay silent forever after one unreadable capture. Here the first
   attempt fails, the persisted state still owes the card, the next tick raises
   it, and only then does the episode go quiet.
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
sup, ps, wi, pd = rem.sup, rem.ps, rem.wi, rem.pd
td = sup.td
from hitl.manager import HitlManager, HitlStore, default_store  # noqa: E402

WID = "w1"
SOCK = "/tmp/pool-wedge-ack.sock"
FOOTER = "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
ABNORMAL = f"  ⎿  API Error: 500 internal server error\n❯ \n{FOOTER}\n"


def pool():
    ws = Path(tempfile.mkdtemp(prefix="pool-wedge-ack-"))
    (ws / "state").mkdir()
    (ws / "state" / "roster.json").write_text(json.dumps(
        {"workers": {WID: {"state": "live", "label": WID}}}))
    wi.record_session(ws, WID, "s1", runtime="claude", relation=wi.RELATION_NEW)
    wi.start_incarnation(ws, WID, "s1", tmux_socket=SOCK, tmux_session=wi.tmux_session_name(WID))
    pd.deliveries_dir(ws, WID).mkdir(parents=True)
    pd.results_dir(ws).mkdir(parents=True)
    return ws


class Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class OneIndexPass(unittest.TestCase):
    def test_the_archive_is_indexed_once_for_the_whole_inbox(self):
        ws = pool()
        archive = pd.results_dir(ws) / "archive"
        archive.mkdir()
        for i in range(40):
            (pd.deliveries_dir(ws, WID) / f"task-{i:04d}.txt").write_bytes(b"")
            (archive / f"task-{i:04d}.txt").write_text("answered\n")
        calls = {"index": 0, "single": 0}
        orig_index, orig_single = td.index_result_candidates, td.has_ready_result

        def index_spy(*a, **k):
            calls["index"] += 1
            return orig_index(*a, **k)

        def single_spy(*a, **k):
            calls["single"] += 1
            return orig_single(*a, **k)
        td.index_result_candidates, td.has_ready_result = index_spy, single_spy
        try:
            owed = sup.work_outstanding(ws, WID)
        finally:
            td.index_result_candidates, td.has_ready_result = orig_index, orig_single
        self.assertFalse(owed)
        self.assertEqual(calls, {"index": 1, "single": 0},
                         "one index pass for 40 owned tasks, no per-task lookup")


class CardIsAcknowledgedOnlyOnceItExists(unittest.TestCase):
    def test_a_failed_first_attempt_is_retried_on_the_next_tick(self):
        ws = pool()
        (pd.deliveries_dir(ws, WID) / "task-owed.txt").write_bytes(b"")
        captures = {"card": 0}

        def runner(argv, **kw):
            if "capture-pane" in argv:
                if "-J" in argv:            # the card's own read, at action time
                    captures["card"] += 1
                    if captures["card"] == 1:
                        return Done(1, err="no server running")
                return Done(0, ABNORMAL)
            if "inbox-holders" in argv:
                return Done(0, "4242 session\n")
            return Done(0)

        store = HitlManager(HitlStore(default_store(ws)))
        decisions, outcomes, escalated = [], [], []
        now = 10_000.0
        for _ in range(6):
            tick = sup.tick(ws, now, runner=runner)
            decisions.append(tick["decisions"][WID])
            acted = rem.apply(ws, REPO, tick["decisions"], runner=runner,
                              spawn=lambda *a, **k: self.fail("no spawn for a wedge"))
            outcomes.append((acted.get("cards") or {}).get(WID, {}).get("outcome"))
            escalated.append(sup.load_state(ws).workers[WID].wedge_escalated)
            now += 300.0
        c = ps.CARD_CAUSE
        self.assertEqual(decisions, ["nothing", "nothing", c, c, "nothing", "nothing"])
        self.assertEqual(outcomes, [None, None, "indeterminate", "carded", None, None])
        self.assertEqual(escalated, [False, False, False, True, True, True],
                         "persisted state must still owe the card after a failed attempt")
        self.assertEqual(len([r for r in store.active()
                              if (r.subject or {}).get("worker_id") == WID]), 1)


if __name__ == "__main__":
    unittest.main()
