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
import runtime_models
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


PICKER = (REPO / "tests" / "fixtures" / "pane-claude-model-picker.txt").read_text()


class Run:
    def __init__(self, rc=0, out=""):
        self.calls = []
        self.rc, self.out = rc, out

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, self.rc, self.out, self.out)


class Hang(Run):
    """A restart that never finishes in time."""

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        raise subprocess.TimeoutExpired(argv, kw.get("timeout") or 0)


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
        # The runtime's own model list, as its /model picker showed it on a live host.
        runtime_models.record(self.ws, "claude", runtime_models.parse_picker(PICKER, "claude"))
        self.launch = mock.patch.object(core_limit_hold, "launch_identity", lambda ws, config_dir=None: ("claude-opus-5-5", None))
        self.launch.start()

    def tearDown(self):
        self.which.stop()
        self.launch.stop()
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
                         ["switch_model:sonnet-5-1m-context", "switch_model:fable-5-1", "switch_model:haiku",
                          "retry_now", "add_account", "refresh_options", "restart_core", "open_terminal"],
                         "the runtime's own list, minus the model in use; no purchase")
        kinds = {a.id: a.kind for a in req.actions}
        self.assertEqual({k for i, k in kinds.items() if i != "open_terminal"}, {recovery.ACTION_KIND})
        self.assertEqual(req.actions[-1].kind, "open_terminal", "the terminal stays last and host-only")
        self.assertIn("tap a model below", req.message, "no runtime button here (no codex CLI), so none named")

    def test_switch_buttons_skip_the_model_in_use_and_codex_offers_the_runtime_instead(self):
        (self.ws / "state" / "model-switch.json").write_text(json.dumps({"model": "claude-sonnet-5[1m]"}))
        ids = [a.id for a in self.card().actions]
        self.assertNotIn("switch_model:sonnet-5-1m-context", ids)
        self.assertIn("switch_model:opus-5-5", ids)
        ids = [a.id for a in self.card(runtime="codex").actions]
        self.assertFalse([i for i in ids if i.startswith("switch_model:")])
        self.assertIn("switch_runtime:claude", ids)
        with mock.patch.object(RA.shutil, "which", return_value="/usr/local/bin/codex"):
            self.assertIn("switch_runtime:codex", [a.id for a in self.card().actions])

    def test_the_card_shows_exactly_the_runtimes_own_model_list(self):
        """Displayed options == the runtime's option list fixture (its /model picker), by exact name,
        minus the model in use; the line names every one with the in-use and capped ones marked."""
        rows = runtime_models.parse_picker(PICKER, "claude")
        self.assertEqual([r["label"] for r in rows],
                         ["Default (Opus 5.5)", "Opus 5.5", "Sonnet 5 (1M context)", "Fable 5.1", "Haiku 4.5"])
        req = self.card()
        shown = [a.label for a in req.actions if a.id.startswith("switch_model:")]
        self.assertEqual(shown, ["Switch to Sonnet 5 (1M context)", "Switch to Fable 5.1", "Switch to Haiku 4.5"])
        self.assertIn("This runtime offers: Default (Opus 5.5) · Opus 5.5 (in use) · Sonnet 5 (1M context) · "
                      "Fable 5.1 · Haiku 4.5.", req.message)
        capped = self.card(kind="turn-rejected", prompt=FABLE_REFUSED)
        self.assertNotIn("switch_model:fable-5-1", [a.id for a in capped.actions], "the capped model is not offered")
        self.assertIn("Fable 5.1 (hit its limit)", capped.message)

    def test_a_tap_sets_exactly_the_model_its_button_names(self):
        req = self.card()
        self.click(req, "switch_model:haiku")
        run = Run()
        [receipt] = self.run_pending(run)
        argv = run.calls[0][0]
        self.assertEqual(argv[2], "claude-haiku-4-5", "Haiku 4.5, the model the button names")
        self.assertEqual(receipt["recovery"]["model"], "claude-haiku-4-5")
        self.assertIn("Switched the core to Haiku 4.5", self.mgr.get(req.id).message)

    def test_the_allowlist_is_the_list_shown(self):
        req = self.card()
        for key in ("opus-5-5", "default-recommended", "gpt-6-sol", "sonnet"):
            with self.subTest(key=key):
                with self.mgr.store.locked():
                    cur = self.mgr.get(req.id)
                    cur.actions = [tui_gate.recovery_action(f"switch_model:{key}")] + cur.actions
                    self.mgr.store.save(cur)
                self.assertEqual(RA._execute("switch_model", key, self.mgr.get(req.id), self.target(), Run(),
                                             time.time(), [])[0], "refused", key)

    def test_without_the_runtimes_list_no_model_is_offered(self):
        (self.ws / "state" / "runtime-models.claude.json").unlink()
        self.assertFalse([a for a in self.card().actions if a.id.startswith("switch_model:")])

    def test_live_seat_fable_refusal_card(self):
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        ids = [a.id for a in req.actions]
        self.assertEqual(ids, ["retry_now", "add_account", "refresh_options", "open_terminal"],
                         "a seat shares the core's config dir: no model switch, no core action, no purchase")
        self.assertIn(SEAT, req.title)
        self.assertIn(recovery.SEAT_MODEL_LOCKED, req.message, "the card says what the notice says")

    def test_live_seat_rate_limit_menu_card_offers_only_the_wait_choices(self):
        req = self.card(kind="selection", prompt=LIMIT_MENU, seat=SEAT)
        labels = {a.id: a.label for a in req.actions}
        # The menu's own choices in the runtime's own wording, minus the purchase.
        self.assertEqual(labels["opt1"], "Stop and wait for limit to reset")
        self.assertEqual(labels["opt2"], "Wait here, then continue automatically at Nov 15 at 6am")
        self.assertNotIn("opt3", labels, "Add funds is never a one-tap keystroke")
        self.assertEqual(req.subject["option_for_action"], {"opt1": 0, "opt2": 1})
        self.assertNotIn("buy_credits", labels)
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
        self.assertEqual([a.id for a in req.actions], ["refresh_options", "restart_core", "open_terminal"])
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
    def test_switch_runs_once_writes_receipt_and_audit_and_closes_the_card_once_verified(self):
        sonnet = {"id": "claude-sonnet-5[1m]", "display": "Sonnet 5 (1M context)"}
        req = self.card()
        self.click(req, "switch_model:sonnet-5-1m-context")
        run = Run()
        [receipt] = self.run_pending(run)
        self.assertEqual(run.calls[0][0][1:4], [str(REPO / "scripts" / "switch-model.sh"), sonnet["id"], "--confirm"])
        self.assertEqual((receipt["outcome"], receipt["recovery"]), ("done", {"state": "running", "model": sonnet["id"]}))
        card = self.mgr.get(req.id)
        self.assertEqual(card.status, STATUS_IN_PROGRESS, "done is said only once the core is seen working")
        self.assertIn(f"Switched the core to {sonnet['display']}; checking", card.message)
        self.assertNotIn("✓ Done.", fallback_body(card))
        RA.verify_step(self.mgr, self.ws, CORE, blocked=False)
        M.resolve_escalations(self.mgr, CORE, "❯ \n", "claude", workspace=self.ws)
        card = self.mgr.get(req.id)
        self.assertEqual(card.status, STATUS_RESOLVED)
        self.assertIn(RA.WORKING_AGAIN, card.message)
        self.assertTrue(fallback_body(card).endswith("✓ Done."))
        self.assertEqual(self.run_pending(run), [])
        self.assertEqual(len(run.calls), 1, "once per id")
        [line] = self.audit()
        self.assertEqual((line["action_id"], line["outcome"], line["sender"], line["session"]),
                         ("switch_model:sonnet-5-1m-context", "done", OWNER, CORE))

    def test_a_seat_switch_is_never_run(self):
        # /model in a seat saves the default in the config dir it shares with the core and every seat.
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        with self.mgr.store.locked():
            cur = self.mgr.get(req.id)
            cur.actions = [tui_gate.recovery_action("switch_model:opus")] + cur.actions
            self.mgr.store.save(cur)
        self.click(self.mgr.get(req.id), "switch_model:opus")
        run, owed = Run(), []
        [receipt] = self.run_pending(run, seat=SEAT, owed=owed)
        self.assertEqual((receipt["outcome"], run.calls, owed), ("refused", [], []))
        self.assertIn(recovery.SEAT_MODEL_LOCKED, self.mgr.get(req.id).message)

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

    def test_an_id_outside_the_runtimes_own_list_never_runs(self):
        req = self.card(runtime="codex")
        with self.mgr.store.locked():
            cur = self.mgr.get(req.id)
            cur.actions = [tui_gate.recovery_action("switch_model:sonnet")] + cur.actions
            self.mgr.store.save(cur)
        self.click(self.mgr.get(req.id), "switch_model:sonnet")
        run = Run()
        [receipt] = self.run_pending(run)
        self.assertEqual((receipt["outcome"], run.calls), ("refused", []), "Codex has no model switch")

    def test_step_up_actions_never_run(self):
        self.assertNotIn("buy_credits", [a.id for a in self.card().actions], "a purchase is never offered")
        req = self.card(prompt=LIMIT + "add_account")
        self.click(req, "add_account")
        run = Run()
        [receipt] = self.run_pending(run)
        self.assertEqual((receipt["outcome"], run.calls, self.keys), ("refused", [], []))
        self.assertEqual(self.mgr.get(req.id).status, STATUS_IN_PROGRESS, "the same card says so; no new card")

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
        self.assertEqual(self.mgr.get(crashed.id).status, STATUS_IN_PROGRESS, "the card says so; no new card")

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

    def _hold(self, seat=None, source="provider-record"):
        now = time.time()
        core_limit_hold._write_hold(self.ws, {"status": "held", "kind": "usage-limit", "model": "opus",
                                              "until": now + 3600, "started_at": now - 60, "set_at": now - 60,
                                              "source": source}, seat)
        return now

    def test_retry_on_a_live_core_at_its_limit_screen_restarts_it_as_the_bare_reply_does(self):
        now = self._hold(source=core_limit_hold.PANE_SOURCE)
        self.assertIsNone(RA.hold_card_step(self.mgr, self.ws, CORE, "claude"), "the screen's card is the one")
        req = self.card()
        self.click(req, "retry_now")
        run = Run()
        [receipt] = self.run_pending(run, now=now)
        self.assertEqual((receipt["outcome"], run.calls[0][0][-1]), ("done", "--restart"))
        self.assertEqual(core_limit_hold.read_hold(self.ws)["status"], "cleared")

    def test_a_seats_retry_restarts_it_by_its_pools_command(self):
        now = self._hold(seat=SEAT_SESSION, source=core_limit_hold.PANE_SOURCE)
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        self.click(req, "retry_now")
        run = Run()
        target = RA.Target(self.ws, SOCK, SEAT_SESSION, seat=SEAT, retry_cmd=["python3", "pool_manage.py", "retry", "w1"])
        [receipt] = RA.run_pending(self.mgr, target, runner=run, now=now)
        self.assertEqual((receipt["outcome"], run.calls[0][0]), ("done", ["python3", "pool_manage.py", "retry", "w1"]))
        self.assertEqual(core_limit_hold.read_hold(self.ws, SEAT_SESSION)["status"], "cleared")

    def test_a_live_cores_failed_restart_keeps_it_paused(self):
        for rc in (1, None):
            with self.subTest(rc=rc):
                self.tearDown()
                self.setUp()
                now = self._hold(source=core_limit_hold.PANE_SOURCE)
                req = self.card()
                self.click(req, "retry_now")
                [receipt] = self.run_pending(Run(rc=1) if rc else Hang(), now=now)
                self.assertEqual(receipt["outcome"], "failed")
                self.assertIsNotNone(core_limit_hold.active_hold(self.ws, now), "a failed restart left no pause")
                self.assertIn("still paused", self.mgr.get(req.id).message)

    def test_a_seats_failed_restart_keeps_it_paused(self):
        now = self._hold(seat=SEAT_SESSION, source=core_limit_hold.PANE_SOURCE)
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        self.click(req, "retry_now")
        target = RA.Target(self.ws, SOCK, SEAT_SESSION, seat=SEAT, retry_cmd=["python3", "pool_manage.py", "retry", "w1"])
        [receipt] = RA.run_pending(self.mgr, target, runner=Run(rc=1), now=now)
        self.assertEqual(receipt["outcome"], "failed")
        self.assertIsNotNone(core_limit_hold.active_hold(self.ws, now, SEAT_SESSION))
        outcome_line = self.mgr.get(req.id).message.rsplit("\n\n", 1)[-1]
        self.assertIn("still paused", outcome_line)
        self.assertNotIn("continues when the limit resets", outcome_line)
        self.assertIsNone(core_limit_hold.read_hold(self.ws), "the seat's failure touched the core's hold")

    def test_the_keystroke_driver_and_a_recovered_core_leave_an_owed_click_alone(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        sent = []
        M.drive_escalations(self.mgr, CORE, UNKNOWN, "blocked-human", lambda k: sent.append(k) or True)
        M.resolve_escalations(self.mgr, CORE, "❯ \n", "claude", workspace=self.ws)
        self.assertEqual((sent, self.mgr.get(req.id).status), ([], STATUS_IN_PROGRESS))


class OneCardPerIncident(Base):
    """One block, one card: an action that does not lift it edits that card, never a new one."""

    def test_a_done_switch_that_does_not_unblock_keeps_the_same_card_and_says_so_once(self):
        req = self.card()
        self.click(req, "switch_model:sonnet-5-1m-context")
        [receipt] = self.run_pending(Run())
        self.assertEqual(receipt["outcome"], "done")
        for _ in range(4):  # later ticks still see the limit screen and escalate as they would
            again = M.escalate(self.mgr, "blocked-human", "awaiting user: usage-limit", "usage-limit", LIMIT,
                               CORE, runtime="claude", workspace=self.ws)
            self.assertEqual(again.id, req.id, "a second card for the same incident")
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=True), [], "too early to say")
        later = time.time() + RA.VERIFY_S + 1
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=True, now=later), [req.id])
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=True, now=later + 60), [], "said once")
        card = self.mgr.get(req.id)
        self.assertEqual(card.message.count(RA.STILL_BLOCKED), 1)
        self.assertNotIn("✓ Done.", fallback_body(card))
        self.assertEqual(len([r for r in self.mgr.active() if (r.subject or {}).get("session") == CORE]), 1)

    def test_a_failed_action_edits_the_same_card(self):
        now = self._hold_live()
        req = self.card()
        self.click(req, "retry_now")
        [receipt] = self.run_pending(Run(rc=1), now=now)
        self.assertEqual(receipt["outcome"], "failed")
        again = M.escalate(self.mgr, "blocked-human", "awaiting user: usage-limit", "usage-limit", LIMIT,
                           CORE, runtime="claude", workspace=self.ws)
        self.assertEqual(again.id, req.id)
        self.assertIn("still paused", self.mgr.get(req.id).message)

    def _hold_live(self):
        now = time.time()
        core_limit_hold._write_hold(self.ws, {"status": "held", "kind": "usage-limit", "model": "opus",
                                              "until": now + 3600, "started_at": now - 60, "set_at": now - 60,
                                              "source": core_limit_hold.PANE_SOURCE})
        return now

    def test_the_monitor_tick_runs_the_verification(self):
        req = self.card()
        self.click(req, "switch_model:haiku")
        self.run_pending(Run())
        with mock.patch.object(RA, "VERIFY_S", 0.0):
            self.assertEqual(M.verify_step(self.mgr, self.ws, CORE, True), [req.id])


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
                         ["retry_now", "switch_model:sonnet-5-1m-context", "switch_model:fable-5-1",
                          "switch_model:haiku", "add_account", "refresh_options"])
        self.assertIs(a.subject.get("remind"), False)
        for word in COMMANDS + PURCHASE_WORDS:
            self.assertNotIn(word, a.message)
        core_limit_hold.clear(self.ws)
        self.assertIsNone(RA.hold_card_step(self.mgr, self.ws, CORE, "claude"))
        self.assertEqual(self.mgr.get(a.id).status, STATUS_RESOLVED)


class PausedCardTime(Base):
    def test_a_later_retry_time_edits_the_one_card(self):
        now = time.time()
        hold = {"status": "held", "kind": "rate-limit", "model": "opus", "until": now + 300,
                "started_at": 1000.0, "set_at": 1000.0}
        core_limit_hold._write_hold(self.ws, hold)
        first = RA.hold_card_step(self.mgr, self.ws, CORE, "claude")
        core_limit_hold._write_hold(self.ws, dict(hold, until=now + 900, strikes=2))
        second = RA.hold_card_step(self.mgr, self.ws, CORE, "claude")
        self.assertEqual(second.id, first.id, "the same pause sent a second card")
        self.assertGreater(second.revision, first.revision)
        self.assertIn(core_limit_hold._when(now + 900), second.message)
        self.assertNotIn(core_limit_hold._when(now + 300), second.message, "the old retry time stayed")


class ExecutorEdges(Base):
    """Each way an action can fail or be refused says so on the same card, and runs nothing it must not."""

    def execute(self, verb, arg=None, req=None, seat=None, runner=None, retry_cmd=None):
        target = RA.Target(self.ws, SOCK, SEAT_SESSION if seat else CORE, seat=seat,
                           send_key=lambda k: self.keys.append(k) or True, retry_cmd=retry_cmd)
        return RA._execute(verb, arg, req, target, runner or Run(), time.time(), [])

    def test_a_seat_retry_without_its_pools_restart_is_refused(self):
        core_limit_hold._write_hold(self.ws, {"status": "held", "until": time.time() + 600, "started_at": 1},
                                    SEAT_SESSION)
        self.assertEqual(self.execute("retry_now", seat=SEAT)[0], "refused")

    def test_a_retry_with_nothing_paused_or_refused_is_refused(self):
        self.assertEqual(self.execute("retry_now", req=self.card(kind="unknown", prompt=UNKNOWN))[0], "refused")

    def test_a_switch_that_fails_or_never_finishes_keeps_the_model(self):
        req = self.card()
        self.assertEqual(self.execute("switch_model", "haiku", req, runner=Run(rc=6))[0], "failed")
        self.assertEqual(self.execute("switch_model", "haiku", req, runner=Hang())[0], "failed")

    def test_a_switch_on_a_refused_turn_resends_it_once_idle(self):
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED)
        owed = []
        target = RA.Target(self.ws, SOCK, CORE)
        out = RA._execute("switch_model", "haiku", req, target, Run(), time.time(), owed)
        self.assertEqual((out[0], owed), ("done", [RA.CONTINUE]))

    def test_restart_and_runtime_switch_never_act_on_a_seat(self):
        for verb, arg in (("restart_core", None), ("switch_runtime", "codex")):
            run = Run()
            self.assertEqual(self.execute(verb, arg, seat=SEAT, runner=run)[0], "refused")
            self.assertEqual(run.calls, [])

    def test_a_restart_that_never_finishes_or_is_held_or_fails_says_which(self):
        self.assertEqual(self.execute("restart_core", runner=Hang())[0], "failed")
        self.assertEqual(self.execute("restart_core", runner=Run(rc=core_limit_hold.HOLD_EXIT))[0], "refused")
        self.assertEqual(self.execute("switch_runtime", "codex", runner=Run(rc=3))[0], "failed")
        self.assertEqual(self.execute("switch_runtime", "codex", runner=Run())[3]["runtime"], "codex")

    def test_escape_and_an_unknown_verb(self):
        self.assertEqual(self.execute("escape_prompt")[0], "done")
        self.assertEqual(self.keys, ["Escape"])
        target = RA.Target(self.ws, SOCK, CORE, send_key=lambda k: False)
        self.assertEqual(RA._execute("escape_prompt", None, None, target, Run(), time.time(), [])[0], "failed")
        self.assertEqual(self.execute("no_such_verb")[0], "refused")

    def test_refresh_on_a_seat_reads_its_pane_and_types_nothing(self):
        out = self.execute("refresh_options", seat=SEAT)
        self.assertEqual(out[0], "done")
        self.assertFalse(runtime_models._probe_path(self.ws, SEAT_SESSION).exists())

    def test_a_runner_that_cannot_start_reads_as_no_exit_code(self):
        def boom(*a, **k):
            raise OSError("no such file")
        self.assertIsNone(RA._rc(boom, ["x"], {}, 1))

    def test_a_click_on_a_card_that_offers_no_recovery_is_refused(self):
        req = self.mgr.create(tui_gate.requirement_for("blocked-human", "selection", "❯ 1. A\n  2. B", CORE, "d",
                                                       "m", runtime="claude"))
        data = {"hitl_id": req.id, "action_id": "restart_core", "at": time.time()}
        self.assertEqual(RA._check(self.mgr, data, RA.Target(self.ws, SOCK, CORE), time.time())[1][0], "refused")
        self.assertEqual(RA._check(self.mgr, {"hitl_id": "hitl_gone", "action_id": "restart_core"},
                                   RA.Target(self.ws, SOCK, CORE), time.time())[1][0], "stale")

    def test_the_state_dir_of_a_seat_is_its_own(self):
        self.assertEqual(RA.Target(self.ws, SOCK, SEAT_SESSION, seat=SEAT).state_dir(),
                         self.ws / "state" / "seats" / SEAT_SESSION)
        self.assertEqual(RA.Target(self.ws, SOCK, CORE).state_dir(), self.ws / "state")

    def test_an_unreadable_card_is_kept_open(self):
        with mock.patch.object(RA, "_awaiting", side_effect=OSError("gone")):
            self.assertTrue(RA.awaiting_executor(self.ws, object()))

    def test_the_audit_keeps_its_bound(self):
        with mock.patch.object(RA, "AUDIT_KEEP", 3):
            for i in range(10):
                RA.audit(self.ws, {"at": i})
        lines = (recovery.recovery_dir(self.ws) / RA.AUDIT_FILE).read_text().splitlines()
        self.assertLessEqual(len(lines), 6)
        self.assertEqual(json.loads(lines[-1])["at"], 9)

    def test_an_audit_that_cannot_be_written_never_fails_the_action(self):
        (self.ws / "state" / "recovery").write_text("not a dir")
        RA.audit(self.ws, {"at": 1})

    def test_no_manager_runs_nothing(self):
        self.assertEqual(RA.run_pending(None, RA.Target(self.ws, SOCK, CORE)), [])

    def test_unreadable_replayed_and_other_sessions_hand_offs(self):
        d = recovery.actions_dir(self.ws)
        d.mkdir(parents=True)
        (d / "hitl_bad.json").write_text("{not json")
        req = self.card()
        self.click(req, "restart_core")
        [path] = [p for p in d.glob("*.json") if p.name != "hitl_bad.json"]
        recovery.write_receipt(self.ws, recovery.receipt(req.id, "i", "restart_core", "done", None, 1))
        run = Run()
        self.assertEqual(RA.run_pending(self.mgr, RA.Target(self.ws, SOCK, CORE), runner=run), [])
        self.assertEqual(run.calls, [], "a hand-off that already has a receipt is never run again")
        self.assertFalse(path.exists())

    def test_a_crash_leftover_with_its_receipt_is_only_cleared(self):
        d = recovery.actions_dir(self.ws)
        req = self.card()
        self.click(req, "restart_core")
        [path] = list(d.glob("*.json"))
        claimed = path.with_name(path.stem + RA.CLAIM_SUFFIX)
        path.rename(claimed)
        recovery.write_receipt(self.ws, recovery.receipt(req.id, "i", "restart_core", "done", None, 1))
        self.assertEqual(RA._interrupted(self.mgr, RA.Target(self.ws, SOCK, CORE), claimed, time.time()), [])
        self.assertFalse(claimed.exists())
        junk = d / ("hitl_junk" + RA.CLAIM_SUFFIX)
        junk.write_text("{")
        self.assertEqual(RA._interrupted(self.mgr, RA.Target(self.ws, SOCK, CORE), junk, time.time()), [])

    def test_a_tap_whose_hand_off_never_landed_closes_its_card_for_a_fresh_one(self):
        req = self.card()
        self.click(req, "restart_core")
        for p in recovery.actions_dir(self.ws).glob("*.json"):
            p.unlink()
        later = time.time() + RA.ORPHAN_CLICK_S + 5
        [receipt] = RA.run_pending(self.mgr, RA.Target(self.ws, SOCK, CORE), runner=Run(), now=later)
        self.assertEqual(receipt["outcome"], "failed")
        self.assertEqual(self.mgr.get(req.id).status, STATUS_EXPIRED, "the next tick raises its replacement")

    def test_unlink_of_a_missing_file_is_quiet(self):
        RA._unlink(self.ws / "nope")

    def test_session_env_reads_one_variable(self):
        ok = lambda *a, **k: subprocess.CompletedProcess(a, 0, "CLAUDE_CONFIG_DIR=/cfg\n", "")  # noqa: E731
        self.assertEqual(RA.session_env(SOCK, CORE, "CLAUDE_CONFIG_DIR", runner=ok), "/cfg")
        unset = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "unknown variable")  # noqa: E731
        self.assertIsNone(RA.session_env(SOCK, CORE, "CLAUDE_CONFIG_DIR", runner=unset))

        def boom(*a, **k):
            raise subprocess.TimeoutExpired("tmux", 5)
        self.assertIsNone(RA.session_env(SOCK, CORE, "CLAUDE_CONFIG_DIR", runner=boom))

    def test_the_paused_card_names_the_waiting_messages(self):
        core_limit_hold._write_hold(self.ws, {"status": "held", "kind": "usage-limit", "model": "opus",
                                              "until": time.time() + 600, "started_at": 5.0, "set_at": 5.0})
        req = RA.hold_card_step(self.mgr, self.ws, CORE, "claude", queued=2)
        self.assertIn("2", req.message.rsplit("\n\n", 1)[-1])

    def test_no_manager_raises_no_paused_card(self):
        self.assertIsNone(RA.hold_card_step(None, self.ws, CORE, "claude"))

    def test_an_unknown_model_in_use_is_none(self):
        with mock.patch.object(core_limit_hold, "launch_identity", side_effect=OSError("no config")):
            self.assertIsNone(RA.current_model(self.ws))

    def test_refresh_leaves_a_tapped_card_and_a_card_that_already_has_the_list(self):
        req = self.card()
        self.assertEqual(RA.refresh_limit_card(self.mgr, self.ws, CORE, "claude"), [], "already the list")
        self.click(req, "add_account")
        (self.ws / "state" / "runtime-models.claude.json").unlink()
        runtime_models.record(self.ws, "claude", runtime_models.parse_picker(PICKER, "claude")[:3])
        self.assertEqual(RA.refresh_limit_card(self.mgr, self.ws, CORE, "claude"), [], "a tapped card is not edited")
        self.assertEqual(RA.refresh_limit_card(self.mgr, self.ws, CORE, "codex"), [], "no Codex switch")

    def test_verify_skips_a_card_whose_state_was_already_said(self):
        req = self.card()
        self.click(req, "switch_model:haiku")
        self.run_pending(Run())
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=False), [req.id])
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=False), [])


class SeatRetryCopy(Base):
    def test_a_held_workers_card_says_retry_restarts_it(self):
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        self.assertIn(tui_gate.RETRY_SEAT_LINE, req.message)
        self.assertNotIn(tui_gate.RETRY_LINE, req.message)
        self.assertIn(tui_gate.RETRY_LINE, self.card(kind="turn-rejected", prompt=FABLE_REFUSED).message)

    def test_a_seat_retry_says_checking_until_the_seat_is_seen_working(self):
        core_limit_hold._write_hold(self.ws, {"status": "held", "until": time.time() + 600, "started_at": 1,
                                              "source": core_limit_hold.PANE_SOURCE}, SEAT_SESSION)
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        self.click(req, "retry_now")
        target = RA.Target(self.ws, SOCK, SEAT_SESSION, seat=SEAT, retry_cmd=["python3", "pool_manage.py", "retry", "w"])
        [receipt] = RA.run_pending(self.mgr, target, runner=Run())
        self.assertEqual(receipt["outcome"], "done")
        card = self.mgr.get(req.id)
        self.assertIn(f"Restarting {SEAT}; checking that it is working again.", card.message)
        self.assertNotIn(RA.WORKING_AGAIN, card.message)

    def test_an_interrupted_action_is_never_followed_by_working_again(self):
        req = self.card()
        self.click(req, "restart_core")
        [path] = list(recovery.actions_dir(self.ws).glob("*.json"))
        path.rename(path.with_name(path.stem + RA.CLAIM_SUFFIX))
        [receipt] = self.run_pending(Run())
        self.assertEqual(receipt["outcome"], "failed")
        RA.verify_step(self.mgr, self.ws, CORE, blocked=False)
        msg = self.mgr.get(req.id).message
        self.assertIn(RA.CLEARED, msg)
        self.assertNotIn(RA.WORKING_AGAIN, msg, "it claimed the interrupted action worked")


class MonitorStepsNeverFail(Base):
    """Each recovery step the monitor tick calls fails open: the monitor keeps ticking."""

    def test_every_step_survives_its_module_raising(self):
        with mock.patch("sys.stderr"):
            with mock.patch.object(RA, "run_pending", side_effect=RuntimeError("x")):
                self.assertIsNone(M.recovery_step(self.mgr, self.ws, SOCK, CORE, "claude", None))
            with mock.patch.object(runtime_models, "probe_step", side_effect=RuntimeError("x")):
                self.assertEqual(M.models_step(self.mgr, self.ws, SOCK, CORE, "claude", None, "running", None,
                                               None, ""), "")
            with mock.patch.object(RA, "verify_step", side_effect=RuntimeError("x")):
                self.assertEqual(M.verify_step(self.mgr, self.ws, CORE, True), [])
            import core_crash_loop
            with mock.patch.object(core_crash_loop, "step", side_effect=RuntimeError("x")):
                self.assertEqual(M.crash_loop_step(self.mgr, self.ws, CORE, "claude", "", time.time()), [])
            with mock.patch.object(core_limit_hold, "hold_while_wedged", side_effect=RuntimeError("x")):
                settled = ("blocked-human", "turn-rejected", "You've hit your usage limit")
                self.assertEqual(M.limit_reply_step(self.ws, settled, settled, LIMIT, "claude"), [])

    def test_no_manager_means_no_step(self):
        self.assertIsNone(M.recovery_step(None, self.ws, SOCK, CORE, "claude", None))
        self.assertEqual(M.verify_step(None, self.ws, CORE, True), [])

    def test_a_refused_turn_owed_after_an_action_is_returned_to_the_tick(self):
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED)
        self.click(req, "switch_model:haiku")
        with mock.patch.object(M, "send_keys", lambda *a: True), \
                mock.patch.object(RA.subprocess, "run", Run()):
            self.assertEqual(M.recovery_step(self.mgr, self.ws, SOCK, CORE, "claude", None), RA.CONTINUE)


class Contract(Base):
    def test_action_fixture_is_a_recovery_click_and_its_hand_off_keeps_every_field(self):
        wire = json.loads((CONTRACTS / "action.json").read_text())[REPLY_FIELD]
        reply = ActionReply.from_wire(wire)
        self.assertEqual(recovery.parse(reply.action_id), ("switch_model", "sonnet-5-1m-context"))
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
