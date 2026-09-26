#!/usr/bin/env python3
"""Every live worker seat gets its own core-input-watch, and its cards name the seat.

The core's input watcher is what turns a gate on a pane (a limit menu, a
permission dialog, a login) into a card for the owner, and only the core had
one. The pool tick now ensures one per live worker seat, idempotently by its
tmux session, spelled so the core launchers' liveness match (`--socket <sock>`)
never mistakes a worker's watcher for the core's and skips starting it. A card
raised with `--seat` names that seat and its terminal jump names its session;
without it the core's card is unchanged.
"""
import importlib.util
import json
import re
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "skills" / "worker-pool" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(REPO / "src"))


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rem = _load("pool_remedy", SCRIPTS / "pool_remedy.py")
wi = rem.wi
ciw = _load("core_input_watch", REPO / "src" / "core-input-watch.py")

WID = "7c54b230a8d94ea9b86f52d70134ac68"
SOCK = "/tmp/pool-input-watch-test.sock"
NAME = f"sutando-worker-{WID}"


class Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Tmux:
    """has-session answers from `alive`; every other tmux call succeeds and is recorded."""

    def __init__(self, alive):
        self.alive, self.calls = set(alive), []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if "has-session" in argv:
            return Done(0 if argv[-1].lstrip("=") in self.alive else 1, err="can't find session")
        return Done(0)

    def started(self):
        return [c for c in self.calls if "new-session" in c]


def scratch():
    ws = Path(tempfile.mkdtemp(prefix="pool-input-watch-"))
    (ws / "state").mkdir()
    (ws / "state" / "roster.json").write_text(json.dumps(
        {"workers": {WID: {"state": "live", "label": "comm"}}}))
    wi.record_session(ws, WID, "s1", runtime="claude", relation=wi.RELATION_NEW)
    wi.start_incarnation(ws, WID, "s1", tmux_socket=SOCK, tmux_session=NAME)
    return ws


def core_liveness_patterns():
    """The launchers' own `pgrep -f` regexes, read from the scripts, with the socket filled in."""
    out = []
    for f in ("src/agent/claude/cli/start-cli.sh", "src/agent/codex/cli/start-cli.sh"):
        for m in re.finditer(r'pgrep -f "(core-input-watch[^"]*)"', (REPO / f).read_text()):
            out.append(m.group(1).replace("${TMUX_SOCKET}", re.escape(SOCK)).replace("\\\\", "\\"))
    return out


class EnsurePerSeat(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(rem, "ensure_input_watch"), "pool_remedy has no ensure_input_watch")
        self.ws = scratch()

    def test_a_live_seat_without_one_gets_one(self):
        t = Tmux({NAME})
        out = rem.ensure_input_watch(self.ws, REPO, WID, runner=t)
        self.assertEqual(out["outcome"], rem.WATCHING)
        [cmd] = t.started()
        self.assertEqual(cmd[1:7], ["-S", SOCK, "new-session", "-d", "-s", NAME + "-input"])
        words = shlex.split(cmd[-1])
        self.assertTrue(words[1].endswith("src/core-input-watch.py"), words)
        self.assertIn(f"--socket={SOCK}", words)
        self.assertIn(f"--session={NAME}", words)
        self.assertIn(f"--out={self.ws}/state/core-supervisor.{NAME}.json", words)
        self.assertIn(f"--seat=worker comm ({WID})", words)

    def test_idempotent_by_its_tmux_session(self):
        t = Tmux({NAME, NAME + "-input"})
        self.assertEqual(rem.ensure_input_watch(self.ws, REPO, WID, runner=t)["outcome"], rem.WATCHING)
        self.assertEqual(t.started(), [])

    def test_a_seat_that_is_gone_gets_none(self):
        t = Tmux(set())
        self.assertEqual(rem.ensure_input_watch(self.ws, REPO, WID, runner=t)["outcome"], rem.NOT_RUNNING)
        self.assertEqual(t.started(), [])

    def test_only_seats_that_answered_alive(self):
        t = Tmux({NAME})
        got = rem.ensure_input_watches(self.ws, REPO, {WID: {"session_alive": True},
                                                       "dead": {"session_alive": False}}, runner=t)
        self.assertEqual(list(got), [WID])

    def test_the_core_launchers_never_read_it_as_the_core_watcher(self):
        pats = core_liveness_patterns()
        self.assertEqual(len(pats), 2, pats)
        cmd = rem.input_watch_command(self.ws, REPO, WID, SOCK, "worker comm")
        argv = " ".join(cmd)
        for p in pats:
            self.assertIsNone(re.search(p, argv), p)
        core = f"python3 {REPO}/src/core-input-watch.py --socket {SOCK} --session sutando-core --out x"
        for p in pats:
            self.assertIsNotNone(re.search(p, core), f"control: {p} must still match the core's own")


class SeatOnTheCard(unittest.TestCase):
    def manager(self):
        from hitl.manager import HitlManager, HitlStore
        return HitlManager(HitlStore(Path(tempfile.mkdtemp(prefix="hitl-seat-"))))

    def test_a_seat_names_itself_and_its_terminal(self):
        req = ciw.escalate(self.manager(), "blocked-human", "awaiting user: selection", "selection",
                           "Pick one:\n❯ 1. Stop and wait\n  2. Switch\n", NAME, seat="worker comm")
        self.assertEqual(req.device, {"id": NAME, "name": "worker comm"})
        self.assertTrue(req.title.startswith("worker comm · "), req.title)
        self.assertIn(NAME, req.message)
        self.assertEqual((req.subject or {}).get("seat"), "worker comm")
        jump = [a for a in req.actions if a.kind == "open_terminal"]
        self.assertEqual([a.label for a in jump], [f"Open terminal ({NAME})"])

    def test_without_a_seat_the_core_card_is_unchanged(self):
        req = ciw.escalate(self.manager(), "blocked-human", "d", "selection", "Pick one:", "sutando-core")
        self.assertEqual(req.device, {"id": "sutando-core", "name": "sutando-core"})
        self.assertNotIn("seat", req.subject or {})
        self.assertEqual([a.label for a in req.actions if a.kind == "open_terminal"], ["Open terminal"])

    def test_two_seats_on_one_dialog_are_two_cards(self):
        m = self.manager()
        a = ciw.escalate(m, "blocked-human", "d", "selection", "Pick one:", NAME, seat="worker comm")
        b = ciw.escalate(m, "blocked-human", "d", "selection", "Pick one:", "sutando-worker-b", seat="worker b")
        self.assertNotEqual(a.id, b.id)


if __name__ == "__main__":
    unittest.main()
