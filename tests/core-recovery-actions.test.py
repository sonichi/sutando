#!/usr/bin/env python3
"""A blocked core's or worker seat's card carries buttons, not terminal commands, and a tap runs
on the host that owns the session, once, from a fixed allowlist.

P0-4: a limit card offers Switch to <model> (the runtime's list), Retry now (a refused turn),
the limit menu's wait-only choices, and for the core Switch runtime and Restart core; the
purchase is its own `purchase`-kind button and no other text names a purchase or a command.
Open terminal stays, last, as the client-local action. Fixtures: two live worker cards
(tests/fixtures/core_recovery/).
P0-5: the reply handler routes a tap to state/recovery/actions/<id>.json; the monitor watching
that session runs it once, within its TTL, writes a C3 receipt and an audit line, and edits the
same card. Forged, replayed, stale, foreign-seat and step-up taps never run.
Contract: contracts/core_recovery.v1/action.json and receipt.json, compared literally.
"""
import importlib.util as u
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _helpers.hermetic_env import isolate_os_environ

isolate_os_environ()

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
sys.path.insert(0, str(SRC))
spec = u.spec_from_file_location("ciw_recovery", SRC / "core-input-watch.py")
M = u.module_from_spec(spec)
spec.loader.exec_module(M)

import core_limit_hold
import recovery_actions as RA
from hitl import recovery, tui_gate
from hitl.manager import HitlManager, HitlStore
from hitl.projector import fallback_body
from hitl.replies import REPLY_FIELD, HitlReplyHandler, actions_dir as driver_dir
from hitl.schema import STATUS_EXPIRED, STATUS_IN_PROGRESS, STATUS_RESOLVED, ActionReply

OWNER = "@owner:s.space"
DM = "!dm:s.space"
CORE = "sutando-core"
SEAT_SESSION = "sutando-worker-b35461a4"
SEAT = "worker Mira (b35461a4)"
SOCK = "/tmp/x.sock"
CONTRACTS = REPO / "contracts" / "core_recovery.v1"
FIXTURES = REPO / "tests" / "fixtures" / "core_recovery"
FABLE_REFUSED = (FIXTURES / "seat-fable-refused.txt").read_text().strip()
LIMIT_MENU = (FIXTURES / "seat-rate-limit-options.txt").read_text().rstrip("\n")
LIMIT = "  ⎿  You've hit your usage limit · resets 3pm\n✻ Cooked for 1s"
UNKNOWN = "  Something unexpected\n  ❯ do the thing?\n  Esc to cancel"
# Owner-facing text must name no terminal command (design rule 8) and no purchase (iOS 3.1.1).
COMMANDS = ("/login", "/usage-credits", "/model", "codex login", "tmux -S", "attach")
PURCHASE_WORDS = ("credit", "Plan & usage", "funds")
SECRET = "sk-ant-oat01-SECRETSECRETSECRET"


class Run:
    def __init__(self, rc=0, out=""):
        self.calls = []
        self.rc, self.out = rc, out

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, self.rc, self.out, self.out)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        for d in ("state", "tasks", "results"):
            (self.ws / d).mkdir()
        self.mgr = HitlManager(HitlStore(self.ws / "state" / "hitl" / "requirements"))
        self.handler = HitlReplyHandler(self.mgr, OWNER, workspace=self.ws, log=lambda m: None)
        self.keys = []
        self.which = mock.patch.object(RA.shutil, "which", return_value=None)  # PATH never decides
        self.which.start()

    def tearDown(self):
        self.which.stop()
        self.tmp.cleanup()

    def card(self, kind="usage-limit", prompt=LIMIT, seat=None, runtime="claude", fresh=True):
        """A new card for this state: a limit card is one per session, so an open one is closed."""
        session = SEAT_SESSION if seat else CORE
        for r in self.mgr.active() if fresh else []:
            if (r.subject or {}).get("session") == session and r.status == "pending":
                self.mgr.resolve(r.id)
        return M.escalate(self.mgr, "blocked-human", f"awaiting user: {kind}", kind, prompt, session,
                          seat=seat, runtime=runtime, workspace=self.ws)

    def payload(self, req, aid):
        return {"hitl_id": req.id, "expected_revision": req.revision, "action_id": aid, "guard": req.guard}

    def click(self, req, aid, actor=OWNER, room=DM, payload=None):
        self.handler.offer({"type": "message.created", "event_id": "$c", "actor_id": actor, "room_id": room,
                            "content": {REPLY_FIELD: payload or self.payload(req, aid)}})
        return self.handler.last_outcome

    def target(self, seat=None):
        return RA.Target(self.ws, SOCK, SEAT_SESSION if seat else CORE, seat=seat,
                         send_key=lambda k: self.keys.append(k) or True)

    def run_pending(self, runner, now=None, seat=None, owed=None):
        return RA.run_pending(self.mgr, self.target(seat), runner=runner, now=now, follow_ups=owed)

    def audit(self):
        p = recovery.recovery_dir(self.ws) / RA.AUDIT_FILE
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


class CardButtons(Base):
    def test_core_limit_card_offers_runtime_neutral_buttons_and_names_them(self):
        req = self.card()
        self.assertEqual([a.id for a in req.actions],
                         ["switch_model:opus", "switch_model:sonnet", "switch_model:haiku", "switch_model:fable",
                          "add_account", "buy_credits", "restart_core", "open_terminal"])
        kinds = {a.id: a.kind for a in req.actions}
        self.assertEqual(kinds["buy_credits"], recovery.PURCHASE_KIND)
        self.assertEqual({k for i, k in kinds.items() if i not in ("buy_credits", "open_terminal")},
                         {recovery.ACTION_KIND})
        self.assertEqual(req.actions[-1].kind, "open_terminal", "the terminal stays last and host-only")
        self.assertIn("tap a model below", req.message, "no runtime button here (no codex CLI), so none named")

    def test_switch_buttons_skip_the_model_in_use_and_codex_offers_the_runtime_instead(self):
        (self.ws / "state" / "model-switch.json").write_text(json.dumps({"model": "sonnet"}))
        self.assertNotIn("switch_model:sonnet", [a.id for a in self.card().actions])
        ids = [a.id for a in self.card(runtime="codex").actions]
        self.assertFalse([i for i in ids if i.startswith("switch_model:")])
        self.assertIn("switch_runtime:claude", ids)
        with mock.patch.object(RA.shutil, "which", return_value="/usr/local/bin/codex"):
            self.assertIn("switch_runtime:codex", [a.id for a in self.card().actions])

    def test_live_seat_fable_refusal_card(self):
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        ids = [a.id for a in req.actions]
        self.assertEqual(ids, ["switch_model:opus", "switch_model:sonnet", "switch_model:haiku",
                               "retry_now", "add_account", "buy_credits", "open_terminal"],
                         "the capped model is not offered; a seat gets no core action")
        self.assertIn(SEAT, req.title)

    def test_live_seat_rate_limit_menu_card_offers_only_the_wait_choices(self):
        req = self.card(kind="selection", prompt=LIMIT_MENU, seat=SEAT)
        labels = {a.id: a.label for a in req.actions}
        self.assertEqual(labels["opt1"], "Wait for reset")
        self.assertEqual(labels["opt2"], "Continue automatically at Nov 15 at 6am")
        self.assertNotIn("opt3", labels, "Add funds is never a one-tap keystroke")
        self.assertEqual(req.subject["option_for_action"], {"opt1": 0, "opt2": 1})
        self.assertIn("buy_credits", labels)
        self.assertFalse([i for i in labels if i.startswith("switch_model:")], "no /model under an open menu")

    def test_owner_facing_text_names_no_command_and_no_purchase(self):
        cards = [self.card(), self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT),
                 self.card(kind="selection", prompt=LIMIT_MENU, seat=SEAT),
                 self.card(kind="unknown", prompt=UNKNOWN), self.card(runtime="codex")]
        for req in cards:
            texts = [req.message, req.title] + [a.label for a in req.actions if a.kind != recovery.PURCHASE_KIND]
            for text in texts:
                for word in COMMANDS + PURCHASE_WORDS:
                    self.assertNotIn(word, text, f"{req.subject.get('gate')}: {text}")
        for runtime in ("claude", "codex"):
            self.assertNotIn("codex login", tui_gate.sign_in_copy(runtime, core=True)[1])

    def test_the_menu_keystroke_answers_only_the_same_menu(self):
        req = self.card(kind="selection", prompt=LIMIT_MENU, seat=SEAT)
        self.click(req, "opt2")
        sent = []
        M.drive_escalations(self.mgr, SEAT_SESSION, LIMIT_MENU, "blocked-human", lambda k: sent.append(k) or True)
        self.assertEqual(sent, ["Down", "Enter"])
        self.assertIsNone(tui_gate.keys_for(self.mgr.get(req.id), "❯ 1. Something else\n  2. Else", "blocked-human"))

    def test_core_unparsed_prompt_offers_restart_and_a_seat_card_without_controls_is_unchanged(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.assertEqual([a.id for a in req.actions], ["restart_core", "open_terminal"])
        self.assertIn("Tap Restart core", req.message)
        legacy = M.escalate(self.mgr, "blocked-human", "awaiting user: usage-limit", "usage-limit", LIMIT,
                            SEAT_SESSION, seat=SEAT, runtime="claude")
        self.assertEqual([a.kind for a in legacy.actions], ["open_terminal"])

    def test_recovery_kinds_are_not_client_local_kinds(self):
        # cinny's useHitlLocalAction LOCAL_KINDS never leave the desktop client.
        local = {"authenticate", "open_terminal", "restart_core"}
        self.assertFalse({recovery.ACTION_KIND, recovery.PURCHASE_KIND} & local)


class Routing(Base):
    def test_owner_tap_lands_as_a_recovery_action_file(self):
        req = self.card()
        self.assertEqual(self.click(req, "switch_model:haiku"), "applied")
        [path] = list(recovery.actions_dir(self.ws).glob("*.json"))
        body = json.loads(path.read_text())
        self.assertEqual((body["hitl_id"], body["action_id"], body["sender"], body["room"]),
                         (req.id, "switch_model:haiku", OWNER, DM))
        self.assertFalse(list(driver_dir(self.ws).glob("*.json")), "not a keystroke for the driver")

    def test_a_menu_choice_stays_a_keystroke(self):
        req = self.card(kind="selection", prompt=LIMIT_MENU, seat=SEAT)
        self.click(req, "opt1")
        self.assertFalse(recovery.actions_dir(self.ws).exists())

    def test_forged_and_replayed_taps_are_refused(self):
        req = self.card()
        self.click(req, "restart_core", actor="@guest:s.space")
        self.assertFalse(recovery.actions_dir(self.ws).exists(), "a non-owner never resolves")
        payload = self.payload(req, "restart_core")
        self.assertEqual(self.click(req, "restart_core", payload=payload), "applied")
        self.assertEqual(self.click(req, "restart_core", payload=payload), "rejected", "a replay is stale")
        other = self.card(kind="unknown", prompt=UNKNOWN)
        self.assertEqual(self.click(other, "switch_model:haiku"), "rejected", "an id the card did not offer")


class Executor(Base):
    def test_switch_runs_once_writes_receipt_and_audit_and_closes_the_card(self):
        req = self.card()
        self.click(req, "switch_model:sonnet")
        run = Run()
        [receipt] = self.run_pending(run)
        self.assertEqual(run.calls[0][0][1:4], [str(REPO / "scripts" / "switch-model.sh"), "sonnet", "--confirm"])
        self.assertEqual((receipt["outcome"], receipt["recovery"]), ("done", {"state": "running", "model": "sonnet"}))
        card = self.mgr.get(req.id)
        self.assertEqual(card.status, STATUS_RESOLVED)
        self.assertIn("Switched the core to sonnet.", card.message)
        self.assertTrue(fallback_body(card).endswith("✓ Done."))
        self.assertEqual(self.run_pending(run), [])
        self.assertEqual(len(run.calls), 1, "once per id")
        [line] = self.audit()
        self.assertEqual((line["action_id"], line["outcome"], line["sender"], line["session"]),
                         ("switch_model:sonnet", "done", OWNER, CORE))

    def test_a_seat_switch_targets_the_seat_and_resumes_its_refused_turn(self):
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        self.click(req, "switch_model:opus")
        run, owed = Run(), []
        [receipt] = self.run_pending(run, seat=SEAT, owed=owed)
        argv = run.calls[0][0]
        self.assertEqual(argv[argv.index("--session") + 1], SEAT_SESSION)
        self.assertEqual(argv[argv.index("--state-dir") + 1], str(self.ws / "state" / "seats" / SEAT_SESSION))
        self.assertEqual((receipt["outcome"], owed), ("done", [RA.CONTINUE]))

    def test_seat_retry_resends_the_refused_turn(self):
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        self.click(req, "retry_now")
        owed = []
        [receipt] = self.run_pending(Run(), seat=SEAT, owed=owed)
        self.assertEqual((receipt["outcome"], owed), ("done", [RA.CONTINUE]))

    def test_each_monitor_runs_only_its_own_sessions_taps(self):
        seat_req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        self.click(seat_req, "retry_now")
        self.assertEqual(self.run_pending(Run()), [], "the core's monitor leaves a seat's tap alone")
        self.assertTrue(list(recovery.actions_dir(self.ws).glob("*.json")))
        self.assertEqual(len(self.run_pending(Run(), seat=SEAT)), 1)

    def test_a_seat_card_never_runs_a_core_action(self):
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        with self.mgr.store.locked():
            cur = self.mgr.get(req.id)
            cur.actions = cur.actions + [tui_gate.recovery_action("restart_core")]
            self.mgr.store.save(cur)
        self.click(self.mgr.get(req.id), "restart_core")
        run = Run()
        [receipt] = self.run_pending(run, seat=SEAT)
        self.assertEqual((receipt["outcome"], run.calls), ("refused", []))

    def test_step_up_actions_never_run(self):
        for aid in ("buy_credits", "add_account"):
            with self.subTest(aid=aid):
                req = self.card(prompt=LIMIT + aid)
                self.click(req, aid)
                run = Run()
                [receipt] = self.run_pending(run)
                self.assertEqual((receipt["outcome"], run.calls, self.keys), ("refused", [], []))
                self.assertEqual(self.mgr.get(req.id).status, STATUS_EXPIRED)

    def test_restart_and_runtime_switch_run_the_launcher_from_outside_the_core(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        run = Run()
        [receipt] = self.run_pending(run)
        argv, kw = run.calls[0]
        self.assertEqual(argv, ["bash", str(SRC / "agent" / "start-cli.sh"), "--restart"])
        self.assertEqual((kw["env"]["SUTANDO_TMUX_SOCKET"], kw["env"]["SUTANDO_TMUX_SESSION"]), (SOCK, CORE))
        self.assertNotIn("SUTANDO_CORE_RUNTIME", kw["env"])
        self.assertEqual(receipt["outcome"], "done")
        req = self.card(runtime="codex")
        self.click(req, "switch_runtime:claude")
        run = Run()
        [receipt] = self.run_pending(run)
        self.assertEqual(run.calls[0][0][2:], ["--runtime", "claude", "--restart"])
        self.assertEqual(receipt["recovery"]["runtime"], "claude")

    def test_restart_refused_while_held(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        [receipt] = self.run_pending(Run(rc=core_limit_hold.HOLD_EXIT))
        self.assertEqual(receipt["outcome"], "refused")

    def test_unknown_id_stale_card_expired_tap_and_crash_leftover_never_run(self):
        d = recovery.actions_dir(self.ws)
        d.mkdir(parents=True)
        (d / "hitl_x.json").write_text(json.dumps({"hitl_id": "hitl_x", "action_id": "rm -rf /"}))
        run = Run()
        [receipt] = self.run_pending(run)
        self.assertEqual((receipt["outcome"], run.calls), ("refused", []))

        closed = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(closed, "restart_core")
        self.mgr.resolve(closed.id)
        [receipt] = self.run_pending(run)
        self.assertEqual((receipt["outcome"], run.calls), ("stale", []))

        late = self.card(kind="unknown", prompt=UNKNOWN + " ")
        self.click(late, "restart_core")
        [receipt] = self.run_pending(run, now=time.time() + RA.ACTION_TTL_S + 5)
        self.assertEqual((receipt["outcome"], run.calls), ("stale", []))

        crashed = self.card(kind="unknown", prompt=UNKNOWN + "  ")
        self.click(crashed, "restart_core")
        [path] = list(d.glob("*.json"))
        path.rename(path.with_name(path.stem + RA.CLAIM_SUFFIX))
        [receipt] = self.run_pending(run)
        self.assertEqual((receipt["outcome"], run.calls), ("failed", []))
        self.assertEqual(self.mgr.get(crashed.id).status, STATUS_EXPIRED)

    def test_nothing_a_subprocess_prints_reaches_the_card_receipt_or_audit(self):
        req = self.card()
        self.click(req, "switch_model:haiku")
        [receipt] = self.run_pending(Run(rc=1, out=f"token={SECRET}"))
        stored = [json.dumps(receipt), self.mgr.get(req.id).message,
                  (recovery.recovery_dir(self.ws) / RA.AUDIT_FILE).read_text()]
        for text in stored:
            self.assertNotIn(SECRET, text)
        self.assertEqual(sorted(receipt), sorted(json.loads((CONTRACTS / "receipt.json").read_text())))

    def test_retry_lifts_a_hold(self):
        now = time.time()
        core_limit_hold._write_hold(self.ws, {"status": "held", "kind": "usage-limit", "model": "opus",
                                              "until": now + 3600, "reset_at": now + 3500,
                                              "started_at": now - 60, "set_at": now - 60})
        req = RA.hold_card_step(self.mgr, self.ws, CORE, "claude")
        self.click(req, "retry_now")
        [receipt] = self.run_pending(Run(), now=now)
        self.assertEqual(receipt["outcome"], "done")
        self.assertEqual(core_limit_hold.read_hold(self.ws)["status"], "cleared")

    def test_the_keystroke_driver_and_a_recovered_core_leave_an_owed_click_alone(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        sent = []
        M.drive_escalations(self.mgr, CORE, UNKNOWN, "blocked-human", lambda k: sent.append(k) or True)
        M.resolve_escalations(self.mgr, CORE, "❯ \n", "claude", workspace=self.ws)
        self.assertEqual((sent, self.mgr.get(req.id).status), ([], STATUS_IN_PROGRESS))


class PausedCard(Base):
    def hold(self, started, model="opus"):
        now = time.time()
        core_limit_hold._write_hold(self.ws, {"status": "held", "kind": "usage-limit", "model": model,
                                              "until": now + 3600, "reset_at": now + 3500,
                                              "started_at": started, "set_at": started})

    def test_one_card_per_hold_and_it_closes_when_the_hold_lifts(self):
        self.hold(1000.0)
        a = RA.hold_card_step(self.mgr, self.ws, CORE, "claude")
        b = RA.hold_card_step(self.mgr, self.ws, CORE, "claude")
        self.assertEqual(a.id, b.id)
        self.assertEqual([x.id for x in a.actions],
                         ["retry_now", "switch_model:sonnet", "switch_model:haiku", "switch_model:fable",
                          "add_account", "buy_credits"])
        self.assertIs(a.subject.get("remind"), False)
        for word in COMMANDS + PURCHASE_WORDS:
            self.assertNotIn(word, a.message)
        core_limit_hold.clear(self.ws)
        self.assertIsNone(RA.hold_card_step(self.mgr, self.ws, CORE, "claude"))
        self.assertEqual(self.mgr.get(a.id).status, STATUS_RESOLVED)


class Contract(Base):
    def test_action_fixture_is_a_recovery_click_and_its_hand_off_keeps_every_field(self):
        wire = json.loads((CONTRACTS / "action.json").read_text())[REPLY_FIELD]
        reply = ActionReply.from_wire(wire)
        self.assertEqual(recovery.parse(reply.action_id), ("switch_model", "sonnet"))
        body = json.loads(recovery.write_action(self.ws, reply, OWNER, DM, now=1).read_text())
        self.assertEqual({k: body[k] for k in wire}, wire)

    def test_receipt_writer_produces_the_fixture(self):
        fixture = json.loads((CONTRACTS / "receipt.json").read_text())
        req = self.card()
        self.click(req, fixture["action_id"])
        [receipt] = self.run_pending(Run(), now=fixture["at"])
        self.assertEqual(receipt, {**fixture, "hitl_id": req.id, "incident": RA.incident_for(req)})
        self.assertEqual(recovery.read_receipt(self.ws, req.id), receipt)
        self.assertEqual(set(recovery.OUTCOMES), {"done", "refused", "stale", "failed"})


if __name__ == "__main__":
    unittest.main()
