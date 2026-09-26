#!/usr/bin/env python3
"""A wedged live worker seat reaches the owner as a card; nothing acts on its session.

Owner decision (Chi, relayed on #4755): a fresh session does not cure a network
error, a rate limit, an API error or a retry, and a frozen turn may need only an
Escape — which is never typed automatically. So:

- no wedge decision ever reaches kill-session, send-keys or a spawn;
- abnormal text raises a card quoting the banner line and naming the cause; on a
  seat routed through the credential proxy, a retry / API / network cause offers
  the proxy restart src/restart.sh performs, never the worker's session;
- a frozen turn raises a card offering "Send Escape", typed only after the owner
  presses it, and only if the pane still shows the frame the card was raised for.
Every subprocess is a stub answered by argv; the hitl store is a scratch one.
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
sup, ps, wi = rem.sup, rem.ps, rem.wi
wc = getattr(rem, "wc", None)
from hitl.manager import HitlManager, HitlStore  # noqa: E402
from hitl.schema import ActionReply  # noqa: E402

WID = "7c54b230a8d94ea9b86f52d70134ac68"
SOCK = "/tmp/pool-wedge-cards.sock"
NAME = f"sutando-worker-{WID}"
FOOTER = "⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
BANNER = "⎿  API Error: 500 internal server error"
PANES = {
    "gate": "  Do you want to proceed?\n  ❯ 1. Yes\n    2. No\n  Esc to cancel\n",
    "limit": f"  ⎿  You've hit your session limit · resets 12:10pm\n❯ \n{FOOTER}\n",
    "abnormal": f"  {BANNER}\n❯ \n{FOOTER}\n",
    "frozen": f"✻ Thinking… (12s · esc to interrupt)\n❯ \n{FOOTER}\n",
}
MOVED = f"✻ Thinking… (47s · esc to interrupt)\n❯ \n{FOOTER}\n"
CODEX_FROZEN = "◦ Working (2m • esc to interrupt)\n› \n"
CODEX_PICKER = "◦ Working (2m • esc to interrupt)\n› 4. gpt-5.5 (current)\nPress enter to confirm or esc to go back\n"
CODEX_ABNORMAL = "API Error: 500 internal server error\n› \n"
CAUSE = getattr(ps, "CARD_CAUSE", "card_cause")
FROZEN = getattr(ps, "CARD_FROZEN", "card_frozen")


class Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


class Tmux:
    """Every session alive; capture-pane shows `pane`; anything that could act on a
    session (kill-session, send-keys, new-session) is recorded."""

    ACTING = ("kill-session", "send-keys", "new-session", "respawn-pane", "respawn-window")

    def __init__(self, pane):
        self.pane, self.calls = pane, []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if "capture-pane" in argv:
            return Done(0, self.pane)
        if "inbox-holders" in argv:
            return Done(0, "4242 session\n")
        return Done(0)

    def acted(self):
        return [c for c in self.calls if any(a in c for a in self.ACTING)]


def never_spawn(*a, **k):
    raise AssertionError("spawn must never run for a wedge")


def pool(runtime="claude"):
    ws = Path(tempfile.mkdtemp(prefix="pool-wedge-cards-"))
    (ws / "state").mkdir()
    row = {"state": "live", "label": "comm"}
    if runtime != "claude":
        row["runtime"] = runtime
    (ws / "state" / "roster.json").write_text(json.dumps(
        {"workers": {WID: row}}))
    if runtime == "codex":
        wi.create_worker(ws, runtime="codex", worker_id=WID, tmux_socket=SOCK)
    else:
        wi.record_session(ws, WID, "s1", runtime="claude", relation=wi.RELATION_NEW)
        wi.start_incarnation(ws, WID, "s1", tmux_socket=SOCK, tmux_session=NAME)
    return ws


def manager(ws):
    return HitlManager(HitlStore(ws / "state" / "hitl" / "requirements"))


def card(ws, which, pane, routed=False):
    t = Tmux(pane)
    out = wc.raise_card(ws, WID, which, runner=t, routed=lambda *a: routed, manager=manager(ws))
    return out, t, manager(ws).get(out.get("hitl_id", "")) if out.get("hitl_id") else None


def press(ws, req, action_id):
    manager(ws).apply_action(ActionReply(hitl_id=req.id, expected_revision=req.revision,
                                         action_id=action_id, guard=req.guard))


class NoWedgeActsOnTheSession(unittest.TestCase):
    def test_the_policy_never_decides_a_restart(self):
        self.assertFalse(hasattr(ps, "RESTART_WEDGED"), "the restart decision still exists")
        kinds = (ps.PANE_GATE, ps.PANE_LIMIT, ps.PANE_ABNORMAL, ps.PANE_WORKING,
                 ps.PANE_IDLE, ps.PANE_UNKNOWN, None)
        seen = set()
        for pane in kinds:
            st, t = ps.SupervisionState(), 1000.0
            for _ in range(12):
                o = ps.Observation(beat=ps.LIVE, session_alive=True, watcher_beat=ps.LIVE,
                                   work_outstanding=True, pane=pane, pane_id="same")
                st, d = ps.evaluate(st, {"w": o}, t)
                seen.add(d["w"])
                t += 300.0
        self.assertEqual(seen - {ps.NOTHING, ps.ESCALATE, CAUSE, FROZEN}, set())
        self.assertNotIn(ps.RECOVER, seen)

    def test_no_wedge_decision_reaches_kill_send_keys_or_spawn(self):
        self.assertIsNotNone(wc, "no wedge card module")
        for which in (ps.ESCALATE, CAUSE, FROZEN):
            for label, pane in PANES.items():
                ws, t = pool(), Tmux(pane)
                rem.apply(ws, REPO, {WID: which}, runner=t, spawn=never_spawn)
                self.assertEqual(t.acted(), [], (which, label))


class FrozenCard(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(wc, "no wedge card module")
        self.ws = pool()

    def test_offers_send_escape_and_types_nothing_by_itself(self):
        out, t, req = card(self.ws, FROZEN, PANES["frozen"])
        self.assertEqual(out["outcome"], "carded")
        self.assertIn(("send_escape", "Send Escape"), [(a.id, a.label) for a in req.actions])
        self.assertIn(f"Open terminal ({NAME})", [a.label for a in req.actions])
        self.assertEqual(req.device["name"], f"worker comm ({WID})")
        self.assertEqual(t.acted(), [])
        t2 = Tmux(PANES["frozen"])
        self.assertEqual(wc.drive_escapes(self.ws, runner=t2, manager=manager(self.ws)), {})
        self.assertEqual(t2.acted(), [], "an unpressed card must type nothing")

    def test_pressed_and_still_frozen_types_one_escape(self):
        _, _, req = card(self.ws, FROZEN, PANES["frozen"])
        press(self.ws, req, "send_escape")
        t = Tmux(PANES["frozen"])
        self.assertEqual(wc.drive_escapes(self.ws, runner=t, manager=manager(self.ws)), {req.id: "sent"})
        self.assertEqual(t.acted(), [["tmux", "-S", SOCK, "send-keys", "-t", f"={NAME}:0", "Escape"]])
        self.assertEqual(wc.drive_escapes(self.ws, runner=Tmux(PANES["frozen"]),
                                          manager=manager(self.ws)), {}, "typed once")

    def test_pressed_after_the_pane_moved_refuses_and_types_nothing(self):
        _, _, req = card(self.ws, FROZEN, PANES["frozen"])
        press(self.ws, req, "send_escape")
        for moved in (MOVED, PANES["gate"], f"❯ \n{FOOTER}\n"):
            t = Tmux(moved)
            ws_out = wc.drive_escapes(self.ws, runner=t, manager=manager(self.ws))
            self.assertEqual(t.acted(), [], moved)
            if ws_out:
                self.assertEqual(ws_out, {req.id: "refused"})
        after = manager(self.ws).get(req.id)
        self.assertEqual(after.status, "expired")
        self.assertIn("nothing was typed", after.subject.get("refused", ""))


class CauseCard(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(wc, "no wedge card module")
        self.ws = pool()

    def test_quotes_the_banner_line_and_names_the_cause(self):
        _, t, req = card(self.ws, CAUSE, PANES["abnormal"])
        self.assertIn(BANNER.lstrip("⎿ ").strip(), req.message)
        self.assertIn("api-error", req.message)
        self.assertIn("api-error", req.title)
        self.assertTrue(any("API Error: 500" in ln for ln in req.subject["cause_lines"]))
        self.assertEqual(t.acted(), [])

    def test_routed_through_the_proxy_offers_the_proxy_restart(self):
        _, t, req = card(self.ws, CAUSE, PANES["abnormal"], routed=True)
        self.assertEqual([a.id for a in req.actions], ["restart_credential_proxy", "open_terminal"])
        remedy = "launchctl kickstart -k gui/$(id -u)/com.sutando.credential-proxy"
        self.assertIn(remedy, req.message)
        self.assertIn("src/restart.sh", req.message)
        self.assertEqual(req.subject["remedy"], remedy)
        self.assertTrue(req.turn_on_action)
        self.assertEqual(t.acted(), [])
        restart_sh = (REPO / "src" / "restart.sh").read_text()
        self.assertIn('_PROXY_LABEL="com.sutando.credential-proxy"', restart_sh)
        self.assertIn('launchctl kickstart -k "$_PROXY_SERVICE"', restart_sh)

    def test_not_routed_names_the_cause_with_no_remedy_button(self):
        _, _, req = card(self.ws, CAUSE, PANES["abnormal"], routed=False)
        self.assertEqual([a.id for a in req.actions], ["open_terminal"])
        self.assertIsNone(req.subject["remedy"])

    def test_a_cleared_seat_closes_its_pending_card(self):
        _, _, req = card(self.ws, CAUSE, PANES["abnormal"])
        self.assertEqual(wc.resolve_cleared(self.ws, {WID}, manager=manager(self.ws)), [req.id])


class CodexCards(unittest.TestCase):
    def setUp(self):
        self.ws = pool("codex")

    def test_frozen_codex_turn_has_codex_runtime_and_escape_checks_it_again(self):
        out, t, req = card(self.ws, FROZEN, CODEX_FROZEN)
        self.assertEqual(out["outcome"], "carded")
        self.assertEqual(req.runtime, "codex")
        self.assertEqual(t.acted(), [])
        press(self.ws, req, "send_escape")
        recheck = Tmux(CODEX_FROZEN)
        self.assertEqual(wc.drive_escapes(self.ws, runner=recheck, manager=manager(self.ws)),
                         {req.id: "sent"})
        self.assertEqual(recheck.acted(),
                         [["tmux", "-S", SOCK, "send-keys", "-t", f"={NAME}:0", "Escape"]])

    def test_codex_picker_over_a_working_marker_clears_frozen_card(self):
        # Codex's selected › row is a gate even when the old working marker remains.
        # The Claude adapter would misread this same pane as a working turn.
        out, t, req = card(self.ws, FROZEN, CODEX_PICKER)
        self.assertEqual(out, {"worker_id": WID, "outcome": "cleared", "pane": ps.PANE_GATE})
        self.assertIsNone(req)
        self.assertEqual(t.acted(), [])

    def test_codex_api_error_never_offers_the_claude_proxy_restart(self):
        def proxy_probe(*_args):
            raise AssertionError("Codex must not inspect the Anthropic proxy")

        t = Tmux(CODEX_ABNORMAL)
        out = wc.raise_card(self.ws, WID, CAUSE, runner=t, routed=proxy_probe,
                            manager=manager(self.ws))
        req = manager(self.ws).get(out["hitl_id"])
        self.assertEqual(req.runtime, "codex")
        self.assertEqual([a.id for a in req.actions], ["open_terminal"])
        self.assertIsNone(req.subject["remedy"])
        self.assertFalse(req.turn_on_action)

    def test_escape_refuses_if_the_workers_runtime_changed(self):
        _, _, req = card(self.ws, FROZEN, CODEX_FROZEN)
        press(self.ws, req, "send_escape")
        roster = self.ws / "state" / "roster.json"
        data = json.loads(roster.read_text())
        data["workers"][WID]["runtime"] = "claude"
        roster.write_text(json.dumps(data))
        recheck = Tmux(CODEX_FROZEN)
        self.assertEqual(wc.drive_escapes(self.ws, runner=recheck, manager=manager(self.ws)),
                         {req.id: "refused"})
        self.assertEqual(recheck.acted(), [])

    def test_missing_roster_row_never_guesses_the_runtime_or_opens_a_card(self):
        roster = self.ws / "state" / "roster.json"
        data = json.loads(roster.read_text())
        del data["workers"][WID]
        roster.write_text(json.dumps(data))
        t = Tmux(CODEX_FROZEN)

        out = wc.raise_card(self.ws, WID, FROZEN, runner=t, manager=manager(self.ws))

        self.assertEqual(out, {"worker_id": WID, "outcome": "indeterminate",
                               "probe": "worker runtime unavailable"})
        self.assertEqual(t.calls, [])
        self.assertEqual(manager(self.ws).active(), [])


class CardEdges(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(wc, "no wedge card module")
        self.ws = pool()

    def test_an_unreadable_capture_raises_no_card(self):
        def boom(argv, **kw):
            raise OSError("tmux gone")
        out = wc.raise_card(self.ws, WID, CAUSE, runner=boom, routed=lambda *a: False,
                            manager=manager(self.ws))
        self.assertEqual(out["outcome"], "indeterminate")

    def test_a_cause_with_no_banner_quotes_the_matching_line(self):
        _, _, req = card(self.ws, CAUSE, f"  Compacting conversation…\n❯ \n{FOOTER}\n")
        self.assertIn("Compacting conversation", req.message)

    def test_an_escape_tmux_refuses_is_reported_and_closes_the_card(self):
        _, _, req = card(self.ws, FROZEN, PANES["frozen"])
        press(self.ws, req, "send_escape")

        def send_fails(argv, **kw):
            if "send-keys" in argv:
                raise OSError("server exited")
            return Done(0, PANES["frozen"])
        self.assertEqual(wc.drive_escapes(self.ws, runner=send_fails, manager=manager(self.ws)),
                         {req.id: "failed"})
        self.assertEqual(manager(self.ws).get(req.id).status, "expired")

    def test_the_module_loads_on_its_own(self):
        saved = {k: sys.modules.pop(k) for k in ("pool_supervise", "pool_wedge_cards") if k in sys.modules}
        try:
            spec = importlib.util.spec_from_file_location("pool_wedge_cards", SCRIPTS / "pool_wedge_cards.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["pool_wedge_cards"] = mod
            spec.loader.exec_module(mod)
            self.assertEqual(mod.ESCAPE_ACTION, "send_escape")
        finally:
            sys.modules.update(saved)


if __name__ == "__main__":
    unittest.main()
