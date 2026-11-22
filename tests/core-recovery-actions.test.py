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
import os
import shutil
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
import detached_action
import recovery_actions as RA
import recovery_gate
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


class Spawned:
    """Stands in for starting a detached process: records it, starts nothing."""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, argv, **kw):
        if self.fail:
            raise OSError("cannot start")
        self.calls.append((argv, kw))
        return mock.Mock()

    def command(self, i=-1):
        argv = self.calls[i][0]
        return argv[argv.index("--") + 1:]

    def key(self, i=-1):
        argv = self.calls[i][0]
        return argv[argv.index("--key") + 1]

    def gate(self, i=-1):
        argv = self.calls[i][0]
        return argv[argv.index("--after-gate") + 1] if "--after-gate" in argv else None


#: The list's read time and host key the fixtures were made with, so its row tokens are fixed.
LIST_AT = 1763730000.0
LIST_KEY = bytes(32)


def record_list(ws, rows=None):
    key = ws / "state" / "recovery" / "option-key"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(LIST_KEY)
    runtime_models.record(ws, "claude", runtime_models.parse_picker(PICKER, "claude") if rows is None else rows,
                          now=LIST_AT)


def sw(ws, slug):
    """The id of the button for the row the picker shows as `slug` (its token in the recorded list)."""
    slugs = [r["key"] for r in runtime_models.parse_picker(PICKER, "claude")]
    return "switch_model:" + runtime_models.entries(ws, "claude")[slugs.index(slug)]["key"]


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
        record_list(self.ws)
        self.launch = mock.patch.object(core_limit_hold, "launch_identity", lambda ws, config_dir=None: ("claude-opus-5-5", None))
        self.launch.start()
        self.spawned = Spawned()  # a detached restart is recorded, never started, in tests
        self.spawn = mock.patch.object(RA, "spawn_detached", self.spawned)
        self.spawn.start()
        # Not a desktop install unless a test says so, wherever this checkout lives.
        self.desktop = mock.patch.object(RA.sutando_config, "desktop_app_support", return_value=None)
        self.desktop.start()

    def tearDown(self):
        self.which.stop()
        self.launch.stop()
        self.spawn.stop()
        self.desktop.stop()
        self.tmp.cleanup()

    def answer(self, outcome, rc=None, reason=None, i=-1):
        """The detached restart's own terminal report, as its wrapper (or the pool) writes it."""
        detached_action.report(self.ws, self.spawned.key(i), outcome, reason, rc)

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

    def post(self, req, room=DM):
        """What the projector records when it posts the card: the room it went to."""
        if not self.mgr.store.projection(req.id).get("event_id"):
            self.mgr.record_projection(req.id, self.mgr.get(req.id).revision, f"$card-{req.id}", {"room_id": room})

    def click(self, req, aid, actor=OWNER, room=DM, payload=None, posted_in=DM):
        self.post(req, posted_in)
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
                         [sw(self.ws, "sonnet-5-1m-context"), sw(self.ws, "fable-5-1"), sw(self.ws, "haiku"),
                          "retry_now", "add_account", "refresh_options", "restart_core", "open_terminal"],
                         "the runtime's own list, minus the model in use; no purchase")
        kinds = {a.id: a.kind for a in req.actions}
        self.assertEqual({k for i, k in kinds.items() if i != "open_terminal"}, {recovery.ACTION_KIND})
        self.assertEqual(req.actions[-1].kind, "open_terminal", "the terminal stays last and host-only")
        self.assertIn("tap a model below", req.message, "no runtime button here (no codex CLI), so none named")

    def test_switch_buttons_skip_the_model_in_use_and_codex_offers_the_runtime_instead(self):
        (self.ws / "state" / "model-switch.json").write_text(json.dumps({"model": "claude-sonnet-5[1m]"}))
        ids = [a.id for a in self.card().actions]
        self.assertNotIn(sw(self.ws, "sonnet-5-1m-context"), ids)
        self.assertIn(sw(self.ws, "opus-5-5"), ids)
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
        self.assertNotIn(sw(self.ws, "fable-5-1"), [a.id for a in capped.actions], "the capped model is not offered")
        self.assertIn("Fable 5.1 (hit its limit)", capped.message)

    def test_a_tap_sets_exactly_the_model_its_button_names(self):
        req = self.card()
        self.click(req, sw(self.ws, "haiku"))
        run = Run()
        [receipt] = self.run_pending(run)
        argv = run.calls[0][0]
        self.assertEqual(argv[2], "claude-haiku-4-5", "Haiku 4.5, the model the button names")
        self.assertEqual(receipt["recovery"]["model"], "claude-haiku-4-5")
        self.assertIn("Switched the core to Haiku 4.5", self.mgr.get(req.id).message)

    def test_the_allowlist_is_the_list_shown(self):
        req = self.card()
        # A label, a model id or a slug is never parsed back into an action.
        for key in ("opus-5-5", "claude-opus-5-5", "Opus 5.5", "o4b876be2aaa6\n"):
            with self.subTest(key=key):
                self.assertIsNone(recovery.parse(f"switch_model:{key}"))
        # A well-formed token that is not a row of the list as recorded runs nothing.
        for key in ("o0123456789ab",):
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
        self.assertEqual(self.click(req, sw(self.ws, "haiku")), "applied")
        [path] = list(recovery.actions_dir(self.ws).glob("*.json"))
        body = json.loads(path.read_text())
        self.assertEqual((body["hitl_id"], body["action_id"], body["sender"], body["room"]),
                         (req.id, sw(self.ws, "haiku"), OWNER, DM))
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
        self.assertEqual(self.click(other, sw(self.ws, "haiku")), "rejected", "an id the card did not offer")


class Executor(Base):
    def test_switch_runs_once_writes_receipt_and_audit_and_closes_the_card_once_verified(self):
        sonnet = {"id": "claude-sonnet-5[1m]", "display": "Sonnet 5 (1M context)"}
        req = self.card()
        self.click(req, sw(self.ws, "sonnet-5-1m-context"))
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
                         (sw(self.ws, "sonnet-5-1m-context"), "done", OWNER, CORE))

    def test_a_seat_switch_is_never_run(self):
        # /model in a seat saves the default in the config dir it shares with the core and every seat.
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        with self.mgr.store.locked():
            cur = self.mgr.get(req.id)
            cur.actions = [tui_gate.recovery_action(sw(self.ws, "haiku"))] + cur.actions
            self.mgr.store.save(cur)
        listed = sw(self.ws, "haiku")
        self.assertIsNotNone(runtime_models.entry(self.ws, "claude", listed[13:]), "a key of the recorded list")
        self.assertIn(listed[13:], RA.switch_offer(self.ws, "claude"), "the core would run this very key")
        self.click(self.mgr.get(req.id), listed)
        run, owed = Run(), []
        [receipt] = self.run_pending(run, seat=SEAT, owed=owed)
        self.assertEqual((receipt["outcome"], run.calls, owed), ("refused", [], []))
        self.assertIn(recovery.SEAT_MODEL_LOCKED, receipt["reason"], "refused by the seat guard, not the list")

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
            cur.actions = [tui_gate.recovery_action("switch_model:o0123456789ab")] + cur.actions
            self.mgr.store.save(cur)
        self.click(self.mgr.get(req.id), "switch_model:o0123456789ab")
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
        card = self.mgr.get(req.id)
        self.assertEqual((card.status, card.subject.get("superseded")), (STATUS_RESOLVED, True),
                         "a card takes one tap: it says why and is closed for a fresh one")

    def test_restart_and_runtime_switch_run_detached_from_the_monitor_they_restart(self):
        # The launcher's --restart closes the tmux session the monitor runs in: it is never the
        # monitor's child, and it acts only once the monitor has recorded the tap (its gate claim).
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        run = Run()
        [receipt] = self.run_pending(run)
        self.assertEqual(run.calls, [], "nothing runs inside the monitor's own process tree")
        argv, kw = self.spawned.calls[0]
        self.assertTrue(kw["start_new_session"])
        self.assertEqual(argv[1:3], [str(SRC / "detached_action.py"), "run"])
        self.assertEqual(self.spawned.command(), ["bash", str(SRC / "agent" / "start-cli.sh"), "--restart"])
        gate = self.spawned.gate()
        self.assertEqual((recovery_gate.state(self.ws, gate) or {}).get("state"), recovery_gate.FINISHED,
                         "the tap was fully recorded before the child is allowed to act")
        self.assertEqual((kw["env"]["SUTANDO_TMUX_SOCKET"], kw["env"]["SUTANDO_TMUX_SESSION"]), (SOCK, CORE))
        self.assertNotIn("SUTANDO_CORE_RUNTIME", kw["env"])
        self.assertEqual(receipt["outcome"], "done")
        card = self.mgr.get(req.id)
        self.assertIn("Restarting the core; checking that it is working again.", card.message)
        self.assertNotIn("receipt", card.subject, "no outcome on the card until the restart answers")
        req = self.card(runtime="codex")
        self.click(req, "switch_runtime:claude")
        [receipt] = self.run_pending(Run())
        self.assertEqual(self.spawned.command()[2:], ["--runtime", "claude", "--restart"])
        self.assertEqual(receipt["recovery"]["runtime"], "claude")

    def test_a_restart_whose_hand_off_was_not_confirmed_says_nothing_ran(self):
        now = self._hold(source=core_limit_hold.PANE_SOURCE)
        req = self.card()
        self.click(req, "retry_now")
        self.run_pending(Run(), now=now)
        self.answer(detached_action.FAILED, reason=detached_action.HANDOFF)
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=True, now=now + 1), [req.id])
        card = self.mgr.get(req.id)
        self.assertIn("not recorded in time, so nothing was restarted", card.message)
        self.assertEqual(card.subject["receipt"]["outcome"], "failed")
        self.assertIsNotNone(core_limit_hold.active_hold(self.ws, now + 2), "the lifted hold is back")

    def test_a_seats_retry_hands_its_gate_claim_to_the_pool(self):
        now = self._hold(seat=SEAT_SESSION, source=core_limit_hold.PANE_SOURCE)
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        self.click(req, "retry_now")
        run = Run()
        target = RA.Target(self.ws, SOCK, SEAT_SESSION, seat=SEAT, retry_cmd=["python3", "pool_manage.py", "retry", "w1"])
        RA.run_pending(self.mgr, target, runner=run, now=now)
        env = run.calls[0][1]["env"]
        self.assertTrue(env[detached_action.GATE_ENV].startswith(f"card:{req.id}:retry_now:"))

    def test_a_restart_held_by_a_limit_says_so_once_it_answers(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        self.run_pending(Run())
        self.answer(detached_action.FAILED, rc=core_limit_hold.HOLD_EXIT, reason="exit")
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=True), [req.id])
        card = self.mgr.get(req.id)
        self.assertIn("paused for a provider limit", card.message)
        self.assertEqual((card.subject["receipt"]["outcome"], card.status), ("failed", STATUS_RESOLVED))
        self.assertEqual(recovery.read_receipt(self.ws, req.id)["outcome"], "failed", "the receipt on disk too")

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
        [handoff] = list(d.glob("*.json"))
        tapped = json.loads(handoff.read_text())["at"]
        [receipt] = self.run_pending(run, now=tapped + 601)  # the TTL is ten minutes, literally
        self.assertEqual((receipt["outcome"], run.calls, self.spawned.calls), ("stale", [], []))

        crashed = self.card(kind="unknown", prompt=UNKNOWN + "  ")
        self.click(crashed, "restart_core")
        [path] = list(d.glob("*.json"))
        path.rename(path.with_name(path.stem + RA.CLAIM_SUFFIX))
        [receipt] = self.run_pending(run)
        self.assertEqual((receipt["outcome"], run.calls, self.spawned.calls), ("failed", [], []))
        card = self.mgr.get(crashed.id)
        self.assertEqual((card.status, card.subject.get("superseded")), (STATUS_RESOLVED, True),
                         "the card says so and a fresh one follows")

    def test_nothing_a_subprocess_prints_reaches_the_card_receipt_or_audit(self):
        req = self.card()
        self.click(req, sw(self.ws, "haiku"))
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
        self.assertEqual((receipt["outcome"], run.calls, self.spawned.command()[-1]), ("done", [], "--restart"))
        self.assertTrue(self.spawned.calls[0][1]["start_new_session"], "a live core's Retry is detached too")
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
        # Could not start: failed at once. Started, then failed or never answered: the monitor that
        # comes back reads that, the pause returns and the card ends on why.
        for how in ("no-start", "failed", "expired"):
            with self.subTest(how=how):
                self.tearDown()
                self.setUp()
                self.spawned.fail = how == "no-start"
                now = self._hold(source=core_limit_hold.PANE_SOURCE)
                req = self.card()
                self.click(req, "retry_now")
                [receipt] = self.run_pending(Run(), now=now)
                if how == "no-start":
                    self.assertEqual(receipt["outcome"], "failed")
                    self.assertIn("still paused", self.mgr.get(req.id).message)
                else:
                    self.assertEqual(receipt["outcome"], "done")
                    if how == "failed":
                        self.answer(detached_action.FAILED, rc=1, reason="exit")
                    later = now + (detached_action.DEADLINE_S + 1 if how == "expired" else 5)
                    self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=True, now=later), [req.id])
                    self.assertIn("did not complete" if how == "failed" else "couldn't confirm",
                                  self.mgr.get(req.id).message)
                self.assertIsNotNone(core_limit_hold.active_hold(self.ws, now + 6), "a failed restart left no pause")

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
        self.click(req, sw(self.ws, "sonnet-5-1m-context"))
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
        self.assertIn("— Still blocked.", fallback_body(card))
        fresh = M.escalate(self.mgr, "blocked-human", "awaiting user: usage-limit", "usage-limit", LIMIT,
                           CORE, runtime="claude", workspace=self.ws)
        self.assertNotEqual(fresh.id, req.id, "the still-blocked card is closed; its text points at this one")
        self.assertEqual([r.id for r in self.mgr.active() if (r.subject or {}).get("session") == CORE], [fresh.id])

    def test_after_a_failed_action_a_fresh_card_takes_the_next_tap(self):
        # A card takes one tap (in_progress -> terminal only): a failed action closes it, and the
        # next tick's card has live controls, so the owner can act again from their phone.
        now = self._hold_live()
        self.spawned.fail = True
        req = self.card()
        self.click(req, "retry_now")
        [receipt] = self.run_pending(Run(), now=now)
        self.assertEqual(receipt["outcome"], "failed")
        msg = self.mgr.get(req.id).message
        self.assertIn("still paused", msg)
        self.assertNotIn('reply "retry now" here', msg, "never tells the owner to tap a card that takes no tap")
        self.assertIn(RA.NEW_CARD, msg)
        again = M.escalate(self.mgr, "blocked-human", "awaiting user: usage-limit", "usage-limit", LIMIT,
                           CORE, runtime="claude", workspace=self.ws)
        self.assertNotEqual(again.id, req.id)
        self.spawned.fail = False
        self.assertEqual(self.click(again, "retry_now"), "applied", "the fresh card takes the tap")
        [receipt] = self.run_pending(Run(), now=now)
        self.assertEqual(receipt["outcome"], "done")

    def _hold_live(self):
        now = time.time()
        core_limit_hold._write_hold(self.ws, {"status": "held", "kind": "usage-limit", "model": "opus",
                                              "until": now + 3600, "started_at": now - 60, "set_at": now - 60,
                                              "source": core_limit_hold.PANE_SOURCE})
        return now

    def test_the_monitor_tick_runs_the_verification(self):
        req = self.card()
        self.click(req, sw(self.ws, "haiku"))
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
                         ["retry_now", sw(self.ws, "sonnet-5-1m-context"), sw(self.ws, "fable-5-1"),
                          sw(self.ws, "haiku"), "add_account", "refresh_options"])
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
        self.assertEqual(self.execute("switch_model", sw(self.ws, "haiku")[13:], req, runner=Run(rc=6))[0], "failed")
        self.assertEqual(self.execute("switch_model", sw(self.ws, "haiku")[13:], req, runner=Hang())[0], "failed")

    def test_a_switch_on_a_refused_turn_resends_it_once_idle(self):
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED)
        owed = []
        target = RA.Target(self.ws, SOCK, CORE)
        out = RA._execute("switch_model", sw(self.ws, "haiku")[13:], req, target, Run(), time.time(), owed)
        self.assertEqual((out[0], owed), ("done", [RA.CONTINUE]))

    def test_restart_and_runtime_switch_never_act_on_a_seat(self):
        for verb, arg in (("restart_core", None), ("switch_runtime", "codex")):
            run = Run()
            self.assertEqual(self.execute(verb, arg, seat=SEAT, runner=run)[0], "refused")
            self.assertEqual(run.calls, [])

    def test_a_restart_that_cannot_start_or_fails_or_never_answers_says_which(self):
        self.spawned.fail = True
        self.assertEqual(self.execute("restart_core")[0], "failed")
        self.spawned.fail = False
        out = self.execute("switch_runtime", "codex")
        self.assertEqual((out[0], out[3]["runtime"]), ("done", "codex"))
        for outcome, rc, reason, words in ((detached_action.FAILED, 3, "exit", "did not complete (exit 3)"),
                                           (detached_action.FAILED, None, "could not start", "did not complete."),
                                           (detached_action.FAILED, None, "The pool's own sentence.",
                                            "The pool's own sentence.")):
            with self.subTest(words=words):
                req = self.card(kind="unknown", prompt=UNKNOWN + words)
                self.click(req, "restart_core")
                self.run_pending(Run())
                self.answer(outcome, rc=rc, reason=reason)
                RA.verify_step(self.mgr, self.ws, CORE, blocked=True)
                self.assertIn(words, self.mgr.get(req.id).message)

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
        shutil.rmtree(self.ws / "state" / "recovery")
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
        card = self.mgr.get(req.id)
        self.assertTrue(card.terminal and card.subject.get("superseded"), "the next tick raises its replacement")

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
        record_list(self.ws, runtime_models.parse_picker(PICKER, "claude")[:3])
        self.assertEqual(RA.refresh_limit_card(self.mgr, self.ws, CORE, "claude"), [], "a tapped card is not edited")
        self.assertEqual(RA.refresh_limit_card(self.mgr, self.ws, CORE, "codex"), [], "no Codex switch")

    def test_verify_skips_a_card_whose_state_was_already_said(self):
        req = self.card()
        self.click(req, sw(self.ws, "haiku"))
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
        run = Run()
        [receipt] = RA.run_pending(self.mgr, target, runner=run)
        self.assertEqual(receipt["outcome"], "done")
        card = self.mgr.get(req.id)
        self.assertIn("Restarting this worker; checking that it is working again.", card.message)
        self.assertNotIn("b35461a4", card.message.rsplit("\n\n", 1)[-1], "the done line shows no raw worker id")
        self.assertNotIn(RA.WORKING_AGAIN, card.message)
        key = run.calls[0][1]["env"][detached_action.KEY_ENV]
        self.assertEqual(RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=False), [],
                         "checking until the pool's restart answers")
        detached_action.report(self.ws, key, detached_action.DONE)
        RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=False)
        self.assertIn(RA.WORKING_AGAIN, self.mgr.get(req.id).message)

    def test_an_interrupted_action_is_never_followed_by_working_again(self):
        req = self.card()
        self.click(req, "restart_core")
        [path] = list(recovery.actions_dir(self.ws).glob("*.json"))
        path.rename(path.with_name(path.stem + RA.CLAIM_SUFFIX))
        [receipt] = self.run_pending(Run())
        self.assertEqual(receipt["outcome"], "failed")
        RA.verify_step(self.mgr, self.ws, CORE, blocked=False)
        card = self.mgr.get(req.id)
        self.assertNotIn(RA.WORKING_AGAIN, card.message, "it claimed the interrupted action worked")
        self.assertIn("— Not done.", fallback_body(card))


class SeatRetryEscFirst(Base):
    """A held worker whose session is alive is woken in place (Escape, then `continue` once idle);
    the pool's restart runs only when it is still blocked after the verify window."""

    RETRY = ["python3", "pool_manage.py", "retry", "w1"]

    def _tap(self):
        now = time.time()
        core_limit_hold._write_hold(self.ws, {"status": "held", "kind": "usage-limit", "until": now + 3600,
                                              "started_at": now - 60, "set_at": now - 60,
                                              "source": core_limit_hold.PANE_SOURCE}, SEAT_SESSION)
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
        self.click(req, "retry_now")
        target = RA.Target(self.ws, SOCK, SEAT_SESSION, seat=SEAT, retry_cmd=self.RETRY,
                           send_key=lambda k: self.keys.append(k) or True)
        return now, req, target

    def test_escape_unblocks_a_live_worker_without_a_restart(self):
        now, req, target = self._tap()
        run, owed = Run(), []
        [receipt] = RA.run_pending(self.mgr, target, runner=run, now=now, follow_ups=owed)
        self.assertEqual((receipt["outcome"], receipt["recovery"]["path"]), ("done", RA.PATH_ESC))
        self.assertEqual((self.keys, owed, run.calls), (["Escape"], [RA.CONTINUE], []), "no restart was run")
        self.assertEqual(core_limit_hold.read_hold(self.ws, SEAT_SESSION)["status"], "cleared")
        self.assertEqual(self.audit()[-1]["path"], RA.PATH_ESC)
        self.assertIn(RA.ESC_LINE, self.mgr.get(req.id).message)
        RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=False, now=now + 5, target=target, runner=run)
        card = self.mgr.get(req.id)
        self.assertIn(RA.WORKING_AGAIN, card.message)
        self.assertNotIn(RA.FALLBACK_LINE, card.message)
        self.assertEqual(run.calls, [], "an unblocked worker is never restarted")

    def test_a_worker_still_blocked_after_escape_falls_back_to_its_pools_restart(self):
        now, req, target = self._tap()
        run = Run()
        RA.run_pending(self.mgr, target, runner=run, now=now, follow_ups=[])
        self.assertEqual(RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=True, now=now + 5,
                                        target=target, runner=run), [], "still inside the verify window")
        self.assertEqual(run.calls, [])
        self.assertEqual(RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=True, now=now + RA.VERIFY_S + 1,
                                        target=target, runner=run), [req.id])
        self.assertEqual(run.calls[0][0], self.RETRY, "the pool's restart ran")
        env = run.calls[0][1]["env"]
        self.assertTrue(env[detached_action.GATE_ENV].startswith(f"card:{req.id}:retry_now:"))
        card = self.mgr.get(req.id)
        self.assertIn(RA.FALLBACK_LINE, card.message)
        self.assertFalse(card.terminal, "the card stays on checking until the restart answers")
        receipt = recovery.read_receipt(self.ws, req.id)
        self.assertEqual((receipt["outcome"], receipt["recovery"]["path"]), ("done", RA.PATH_FALLBACK))
        self.assertEqual([a.get("path") for a in self.audit()], [RA.PATH_ESC, RA.PATH_FALLBACK])
        RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=True, now=now + RA.VERIFY_S + 2,
                       target=target, runner=run)
        self.assertEqual(len(run.calls), 1, "the fallback restart runs once")
        detached_action.report(self.ws, env[detached_action.KEY_ENV], detached_action.DONE)
        RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=False, now=now + RA.VERIFY_S + 3,
                       target=target, runner=run)
        self.assertIn(RA.WORKING_AGAIN, self.mgr.get(req.id).message)

    def test_a_failed_fallback_restart_keeps_the_worker_paused(self):
        now, req, target = self._tap()
        RA.run_pending(self.mgr, target, runner=Run(), now=now, follow_ups=[])
        RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=True, now=now + RA.VERIFY_S + 1,
                       target=target, runner=Run(rc=1))
        receipt = recovery.read_receipt(self.ws, req.id)
        self.assertEqual((receipt["outcome"], receipt["recovery"]["path"]), ("failed", RA.PATH_FALLBACK))
        self.assertIsNotNone(core_limit_hold.active_hold(self.ws, now + RA.VERIFY_S + 2, SEAT_SESSION))
        self.assertTrue(self.mgr.get(req.id).terminal, "closed for a fresh card")

    def test_a_dead_session_is_restarted_at_once(self):
        now, req, _ = self._tap()
        target = RA.Target(self.ws, SOCK, SEAT_SESSION, seat=SEAT, retry_cmd=self.RETRY)  # takes no key
        run = Run()
        [receipt] = RA.run_pending(self.mgr, target, runner=run, now=now, follow_ups=[])
        self.assertEqual((receipt["recovery"]["path"], run.calls[0][0]), (RA.PATH_RESTART, self.RETRY))

    def _escaped(self):
        now, req, target = self._tap()
        RA.run_pending(self.mgr, target, runner=Run(), now=now, follow_ups=[])
        return now, req, target

    def test_a_fallback_whose_claim_is_not_saved_starts_no_restart(self):
        now, req, target = self._escaped()
        run = Run()
        with mock.patch.object(self.mgr.store, "save", side_effect=OSError("disk full")):
            for later in (RA.VERIFY_S + 1, RA.VERIFY_S + 30):
                try:  # the monitor's own wrapper (M.verify_step) swallows what escapes here
                    RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=True, now=now + later,
                                   target=target, runner=run)
                except OSError:
                    pass
        self.assertEqual(run.calls, [], "a restart ran with no record of it on the card")

    def test_a_fallback_interrupted_after_its_dispatch_never_restarts_again(self):
        now, req, target = self._escaped()

        class Died(BaseException):
            pass

        calls = []

        def dies(argv, **kw):
            calls.append(argv)
            raise Died()  # the monitor goes down mid-restart, before anything after it is recorded

        with self.assertRaises(Died):
            RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=True, now=now + RA.VERIFY_S + 1,
                           target=target, runner=dies)
        run = Run()
        for later in (RA.VERIFY_S + 30, 2 * RA.VERIFY_S + 5):
            RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=True, now=now + later,
                           target=target, runner=run)
        self.assertEqual((len(calls), run.calls), (1, []), "the restart was dispatched again")

    def test_a_card_still_claiming_a_dispatched_fallback_is_reconciled_not_rerun(self):
        now, req, target = self._escaped()
        key = self.mgr.get(req.id).subject["verify"]["fallback"]
        detached_action.begin(self.ws, key, now + 2)  # dispatched, then the card update was lost
        run = Run()
        RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=True, now=now + RA.VERIFY_S + 1,
                       target=target, runner=run)
        self.assertEqual(run.calls, [])
        self.assertEqual(detached_action.status(self.ws, key, now + RA.VERIFY_S + 1).state, detached_action.RUNNING)
        self.assertEqual(self.mgr.get(req.id).subject["verify"]["detached"], key)

    def test_one_fallback_only_a_worker_still_blocked_after_its_restart_gets_a_new_card(self):
        now, req, target = self._escaped()
        run = Run()
        RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=True, now=now + RA.VERIFY_S + 1,
                       target=target, runner=run)
        detached_action.report(self.ws, run.calls[0][1]["env"][detached_action.KEY_ENV], detached_action.DONE)
        for later in (RA.VERIFY_S + 5, 2 * RA.VERIFY_S + 2, 3 * RA.VERIFY_S):
            RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=True, now=now + later,
                           target=target, runner=run)
        self.assertEqual(len(run.calls), 1, "a second fallback restart ran")
        card = self.mgr.get(req.id)
        self.assertIn(RA.STILL_BLOCKED, card.message)
        self.assertTrue(card.terminal, "closed for a fresh card")

    def test_another_sessions_target_never_restarts_this_worker(self):
        now, req, target = self._escaped()
        other = RA.Target(self.ws, SOCK, "sutando-worker-other", seat="worker Other", retry_cmd=self.RETRY)
        run = Run()
        RA.verify_step(self.mgr, self.ws, SEAT_SESSION, blocked=True, now=now + RA.VERIFY_S + 1,
                       target=other, runner=run)
        self.assertEqual(run.calls, [])
        self.assertIn(RA.STILL_BLOCKED, self.mgr.get(req.id).message)

    def test_the_monitor_loop_hands_its_retry_cmd_to_verify(self):
        out = self.ws / "state" / f"core-supervisor.{SEAT_SESSION}.json"
        argv = ["core-input-watch.py", "--socket", SOCK, "--session", SEAT_SESSION, "--out", str(out),
                "--once", "--stable", "1", "--seat", SEAT, "--retry-cmd=" + " ".join(self.RETRY)]

        class RH:
            TMUX_SOCKET = SESSION = None

            def derive(self):
                return {"health": "idle", "signals": {"process": True}}

        with mock.patch.object(M, "capture_styled", lambda s, sess: FABLE_REFUSED), \
                mock.patch.object(M, "_load_runtime_health", lambda: RH()), \
                mock.patch.object(M, "gateway_alive", lambda *a: True), \
                mock.patch.object(M, "_ensure_tmux_on_path", lambda: None), \
                mock.patch.object(M, "session_runtime", lambda *a: "claude"), \
                mock.patch.object(M, "send_keys", lambda *a: False), \
                mock.patch.object(M, "verify_step", return_value=[]) as v, \
                mock.patch.object(sys, "argv", argv):
            M.main()
        self.assertTrue(v.called, "the monitor never ran its verify step")
        kw = v.call_args.kwargs
        self.assertEqual((kw["retry_cmd"], kw["socket"], kw["seat"]), (self.RETRY, SOCK, SEAT))

    def test_the_monitor_passes_the_seats_restart_to_verify(self):
        now, req, target = self._tap()
        RA.run_pending(self.mgr, target, runner=Run(), now=now, follow_ups=[])
        with mock.patch.object(RA, "verify_step", return_value=[]) as v:
            M.verify_step(self.mgr, self.ws, SEAT_SESSION, True, "blocked-human", socket=SOCK, seat=SEAT,
                          retry_cmd=self.RETRY)
        self.assertEqual(v.call_args.kwargs["target"].retry_cmd, self.RETRY)


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
        self.click(req, sw(self.ws, "haiku"))
        with mock.patch.object(M, "send_keys", lambda *a: True), \
                mock.patch.object(RA.subprocess, "run", Run()):
            self.assertEqual(M.recovery_step(self.mgr, self.ws, SOCK, CORE, "claude", None), RA.CONTINUE)


class OneSurfaceAtTheClose(Base):
    """The card's own last edit says how the incident ended; no separate "Cleared" message repeats it."""

    def projection(self, req):
        return {"event_id": "$card", "revision": req.revision, "notified_at": time.time() - 60}

    def test_a_card_that_said_it_is_working_again_owes_no_cleared_message(self):
        from hitl import renotify
        req = self.card()
        self.click(req, sw(self.ws, "haiku"))
        self.run_pending(Run())
        RA.verify_step(self.mgr, self.ws, CORE, blocked=False)
        M.resolve_escalations(self.mgr, CORE, "❯ \n", "claude", workspace=self.ws)
        card = self.mgr.get(req.id)
        self.assertEqual(card.status, STATUS_RESOLVED)
        self.assertIsNone(renotify.due(card, self.projection(card), time.time()))
        self.assertEqual(renotify.plan([(card, self.projection(card))], time.time()), [])

    def test_a_block_that_cleared_with_no_action_still_gets_its_one_notice(self):
        from hitl import renotify
        req = self.card()
        M.resolve_escalations(self.mgr, CORE, "❯ \n", "claude", workspace=self.ws)
        card = self.mgr.get(req.id)
        self.assertEqual(renotify.due(card, self.projection(card), time.time()), renotify.RECOVERY)


class RoomBinding(Base):
    def test_a_tap_from_another_room_runs_nothing(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.assertEqual(self.click(req, "restart_core", room="!elsewhere:s.space"), "rejected")
        run = Run()
        self.assertEqual(self.run_pending(run), [])
        self.assertEqual(run.calls, [], "the same card and id from another room executed")
        self.assertEqual(self.click(req, "restart_core"), "applied", "from the room it was posted in it runs")

    def test_a_card_never_posted_takes_no_tap(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.handler.offer({"type": "message.created", "event_id": "$c", "actor_id": OWNER, "room_id": DM,
                            "content": {REPLY_FIELD: self.payload(req, "restart_core")}})
        self.assertEqual(self.handler.last_outcome, "rejected")


class RuntimeAllowlist(Base):
    """The executor's runtime check is the last word, and a card never offers what it refuses."""

    def test_a_codex_card_offers_no_retry_and_a_forged_codex_retry_changes_nothing(self):
        now = time.time()
        core_limit_hold._write_hold(self.ws, {"status": "held", "kind": "usage-limit", "until": now + 3600,
                                              "started_at": now - 60, "set_at": now - 60})
        req = self.card(runtime="codex")
        self.assertNotIn("retry_now", [a.id for a in req.actions], "Codex's host side has no retry")
        with self.mgr.store.locked():
            cur = self.mgr.get(req.id)
            cur.actions = [tui_gate.recovery_action("retry_now")] + cur.actions
            self.mgr.store.save(cur)
        before = core_limit_hold.read_hold(self.ws)
        self.click(self.mgr.get(req.id), "retry_now")
        run = Run()
        [receipt] = self.run_pending(run, now=now)
        self.assertEqual((receipt["outcome"], run.calls, self.spawned.calls), ("refused", [], []))
        self.assertIn("runtime carries out", receipt["reason"])
        self.assertEqual(core_limit_hold.read_hold(self.ws), before, "the hold is untouched")

    def test_a_codex_paused_card_offers_no_retry_or_model_switch(self):
        now = time.time()
        core_limit_hold._write_hold(self.ws, {"status": "held", "kind": "usage-limit", "until": now + 3600,
                                              "started_at": now - 60, "set_at": now - 60})
        ids = [a.id for a in RA.hold_card_step(self.mgr, self.ws, CORE, "codex").actions]
        self.assertNotIn("retry_now", ids)
        self.assertFalse([i for i in ids if i.startswith("switch_model:")])


class NothingPrintedLeaks(Base):
    """No subprocess output reaches the card, the receipt or the audit, on any path that runs one."""

    AUDIT_KEYS = ["action_id", "at", "hitl_id", "outcome", "seat", "sender", "session"]

    def stored(self, req, receipt):
        return [json.dumps(receipt), self.mgr.get(req.id).message, json.dumps(self.mgr.get(req.id).subject),
                (recovery.recovery_dir(self.ws) / RA.AUDIT_FILE).read_text()]

    def test_restart_runtime_switch_and_a_seats_retry_copy_nothing_out(self):
        cases = (("restart_core", None), ("switch_runtime:codex", None), ("retry_now", SEAT))
        for aid, seat in cases:
            with self.subTest(aid=aid):
                self.tearDown()
                self.setUp()
                RA.shutil.which.return_value = "/usr/bin/codex"
                now = time.time()
                if seat:
                    core_limit_hold._write_hold(self.ws, {"status": "held", "until": now + 600, "started_at": 1,
                                                          "source": core_limit_hold.PANE_SOURCE}, SEAT_SESSION)
                    req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED, seat=SEAT)
                else:
                    req = self.card(kind="unknown", prompt=UNKNOWN) if aid == "restart_core" else self.card()
                self.assertIn(aid, [a.id for a in req.actions])
                self.click(req, aid)
                run = Run(rc=1, out=f"token={SECRET} /Users/someone/project")
                target = RA.Target(self.ws, SOCK, SEAT_SESSION if seat else CORE, seat=seat,
                                   retry_cmd=["python3", "pool_manage.py", "retry", "w"])
                [receipt] = RA.run_pending(self.mgr, target, runner=run, now=now)
                if not seat:  # the launcher's own output went to its log; its exit code is all that is kept
                    self.answer(detached_action.FAILED, rc=1, reason="exit")
                    RA.verify_step(self.mgr, self.ws, CORE, blocked=True, now=now + 1)
                for text in self.stored(req, receipt):
                    self.assertNotIn(SECRET, text)
                    self.assertNotIn("/Users/someone", text)
                [line] = self.audit()
                keys = sorted(self.AUDIT_KEYS + ["path"]) if seat else self.AUDIT_KEYS
                if aid == "restart_core":
                    keys = sorted(keys + ["via"])
                self.assertEqual(sorted(line), keys,
                                 "the tap audit holds ids, outcomes, the retry's path and the restart's command only")


class HandOffRechecks(Base):
    """A hand-off file is re-checked against the card: its id, guard and chosen action must match."""

    def test_a_rewritten_action_guard_or_unoffered_id_is_stale_or_refused_and_runs_nothing(self):
        for field, value, outcome in (("guard", "limit:someone-else", "stale"),
                                      ("action_id", "refresh_options", "stale"),
                                      ("action_id", "switch_runtime:codex", "refused")):
            with self.subTest(field=field, value=value):
                self.tearDown()
                self.setUp()
                req = self.card(kind="unknown", prompt=UNKNOWN)
                self.click(req, "restart_core")
                [path] = list(recovery.actions_dir(self.ws).glob("*.json"))
                path.write_text(json.dumps({**json.loads(path.read_text()), field: value}))
                run = Run()
                [receipt] = self.run_pending(run)
                self.assertEqual((receipt["outcome"], run.calls, self.spawned.calls), (outcome, [], []))

    def test_a_forged_hand_off_for_a_pending_card_runs_nothing(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        recovery.write_action(self.ws, ActionReply(hitl_id=req.id, expected_revision=req.revision,
                                                   action_id="restart_core", guard=req.guard), OWNER, DM)
        run = Run()
        [receipt] = self.run_pending(run)
        self.assertEqual((receipt["outcome"], run.calls, self.spawned.calls), ("stale", [], []))


class StaleTapAnswered(Base):
    """A stale tap is answered on its card (receipt `stale`, a fresh revision); nothing runs."""

    def project(self, sender):
        from hitl.projector import project_pass
        project_pass(self.mgr, sender, DM)

    def sends(self, req, sender):
        from hitl.projector import WIRE_FIELD
        return [x for x in sender if x["extra_content"][WIRE_FIELD]["id"] == req.id]

    def test_an_expired_tap_edits_its_card_once_with_a_stale_receipt_and_runs_nothing(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        sent = []
        sender = lambda payload: sent.append(payload) or {"ok": True, "event_id": "$e"}  # noqa: E731
        self.project(sender)
        sent.clear()
        [handoff] = list(recovery.actions_dir(self.ws).glob("*.json"))
        run = Run()
        [receipt] = self.run_pending(run, now=json.loads(handoff.read_text())["at"] + 601)
        self.assertEqual((receipt["outcome"], run.calls, self.spawned.calls), ("stale", [], []))
        self.project(sender)
        from hitl.projector import WIRE_FIELD
        [edit] = self.sends(req, sent)
        self.assertEqual(edit["op"], "edit")
        wire = edit["extra_content"][WIRE_FIELD]
        self.assertEqual(wire["subject"]["receipt"]["outcome"], "stale")
        self.assertGreater(wire["revision"], req.revision + 1, "a fresh revision past the tap's")

    def test_a_replayed_hand_off_is_refused_by_the_gate(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        [path] = list(recovery.actions_dir(self.ws).glob("*.json"))
        body = path.read_text()
        self.run_pending(Run())
        recovery.receipts_dir(self.ws).joinpath(f"{req.id}.json").unlink()  # even without its receipt
        path.write_text(body)
        before = self.mgr.get(req.id)
        [receipt] = self.run_pending(Run())
        self.assertEqual((receipt["outcome"], len(self.spawned.calls)), ("stale", 1), "run once, never twice")
        self.assertIn("already acted on", receipt["reason"])
        after = self.mgr.get(req.id)
        self.assertEqual((after.revision, after.status, after.message), (before.revision, before.status, before.message),
                         "the card stays the running tap's: a replay never answers over it")

    def test_a_tap_on_an_old_revision_gets_a_stale_receipt_on_the_open_card(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        old = self.payload(req, "restart_core")
        with self.mgr.store.locked():
            cur = self.mgr.get(req.id)
            cur.refresh_guard(cur.guard)  # the screen moved on: a new revision of the same card
            self.mgr.store.save(cur)
        self.assertEqual(self.click(req, "restart_core", payload=old), "rejected")
        card = self.mgr.get(req.id)
        self.assertEqual((card.status, card.subject["receipt"]["outcome"]), ("pending", "stale"))
        self.assertEqual(card.to_wire()["subject"]["receipt"]["outcome"], "stale")
        self.assertFalse(list(recovery.actions_dir(self.ws).glob("*.json")) if recovery.actions_dir(self.ws).is_dir()
                         else [], "nothing was handed to the executor")

    def test_a_second_tap_while_the_first_runs_leaves_the_first_ones_outcome(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        payload = self.payload(req, "restart_core")
        self.click(req, "restart_core", payload=payload)
        self.assertEqual(self.click(req, "restart_core", payload=payload), "rejected")
        self.assertNotIn("receipt", self.mgr.get(req.id).subject, "the running tap's answer is not overwritten")

    def test_a_forged_stale_hand_off_leaves_an_open_card_that_later_says_only_cleared(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        recovery.write_action(self.ws, ActionReply(hitl_id=req.id, expected_revision=req.revision,
                                                   action_id="restart_core", guard=req.guard), OWNER, DM)
        [receipt] = self.run_pending(Run())
        card = self.mgr.get(req.id)
        self.assertEqual((receipt["outcome"], card.status, card.subject["receipt"]["outcome"]),
                         ("stale", "pending", "stale"), "answered on the card, which stays usable")
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=False), [req.id])
        msg = self.mgr.get(req.id).message
        self.assertIn(RA.CLEARED, msg)
        self.assertNotIn(RA.WORKING_AGAIN, msg, "nothing ran, so nothing is claimed for it")

    def test_a_stale_stamp_that_cannot_be_saved_never_breaks_the_rejection(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        with mock.patch.object(self.mgr.store, "save", side_effect=OSError("disk")):
            self.handler._stamp_stale(req.id)  # logged, never raised

    def test_a_tap_held_in_a_backlog_counts_from_when_it_was_made(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.post(req)
        made = time.time() - 700
        self.handler.offer({"type": "message.created", "event_id": "$c", "actor_id": OWNER, "room_id": DM,
                            "origin_server_ts": int(made * 1000),
                            "content": {REPLY_FIELD: self.payload(req, "restart_core")}})
        [handoff] = list(recovery.actions_dir(self.ws).glob("*.json"))
        self.assertEqual(json.loads(handoff.read_text())["tapped_at"], int(made))
        [receipt] = self.run_pending(Run())
        self.assertEqual((receipt["outcome"], self.spawned.calls), ("stale", []))

    def test_the_event_time_is_read_only_from_a_sane_stamp(self):
        for ev in ({}, {"origin_server_ts": "1"}, {"origin_server_ts": True}, {"origin_server_ts": -5},
                   {"origin_server_ts": float("inf")}, None):
            self.assertIsNone(recovery.event_time(ev))
        self.assertEqual(RA.issued_at({"at": 10, "tapped_at": 4}), 4)
        self.assertEqual(RA.issued_at({"at": 10}), 10)
        self.assertIsNone(RA.issued_at({"at": True}))


class ResumeReported(Base):
    """The card's last word says whether the work that was waiting resumed, not only that the core is up."""

    def waiting(self, name="task-1"):
        (self.ws / "tasks" / f"{name}.txt").write_text("id: x\nsource: ag2space\ntask: hi\n")

    def done_switch(self):
        req = self.card()
        self.click(req, sw(self.ws, "haiku"))
        [receipt] = self.run_pending(Run())
        self.assertEqual(receipt["outcome"], "done")
        return req

    def test_up_with_the_waiting_task_still_queued_stays_checking_then_says_it_did_not_start(self):
        self.waiting()
        req = self.done_switch()
        self.assertEqual(self.mgr.get(req.id).subject["verify"]["waiting"], ["task-1"])
        now = time.time()
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=False, now=now + 5, state="idle"), [])
        M.resolve_escalations(self.mgr, CORE, "❯ \n", "claude", workspace=self.ws)
        self.assertFalse(self.mgr.get(req.id).terminal, "kept open while the resume is still being checked")
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=False, now=now + RA.RESUME_S + 1,
                                        state="idle"), [req.id])
        card = self.mgr.get(req.id)
        self.assertIn(RA.NOT_RESUMED, card.message)
        self.assertNotIn(RA.WORKING_AGAIN, card.message)

    def test_the_waiting_task_leaving_the_queue_is_the_resume(self):
        self.waiting()
        req = self.done_switch()
        (self.ws / "tasks" / "task-1.txt").unlink()
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=False, state="idle"), [req.id])
        self.assertIn(RA.RESUMED, self.mgr.get(req.id).message)

    def test_a_refused_turn_resumes_when_the_core_is_seen_running(self):
        req = self.card(kind="turn-rejected", prompt=FABLE_REFUSED)
        self.click(req, sw(self.ws, "haiku"))
        self.run_pending(Run())
        self.assertTrue(self.mgr.get(req.id).subject["verify"]["turn"])
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=False, state="idle"), [])
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=False, state="running"), [req.id])
        self.assertIn(RA.RESUMED, self.mgr.get(req.id).message)

    def test_with_nothing_waiting_it_says_working_again(self):
        req = self.done_switch()
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=False, state="idle"), [req.id])
        self.assertIn(RA.WORKING_AGAIN, self.mgr.get(req.id).message)

    def test_a_detached_restart_stamps_done_when_it_answers_then_reports_the_resume(self):
        self.waiting()
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        self.run_pending(Run())
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=False), [], "the restart has not answered")
        self.answer(detached_action.DONE)
        RA.verify_step(self.mgr, self.ws, CORE, blocked=False, state="idle")
        card = self.mgr.get(req.id)
        self.assertEqual(card.subject["receipt"]["outcome"], "done")
        self.assertNotIn("seen", card.subject["verify"], "still checking that the waiting work starts")
        RA.verify_step(self.mgr, self.ws, CORE, blocked=False, state="running")
        self.assertIn(RA.RESUMED, self.mgr.get(req.id).message)

    def test_an_unreadable_queue_is_no_evidence(self):
        with mock.patch("task_queue.pending_files", side_effect=OSError("no")):
            self.assertEqual(RA._resume_check(self.ws, self.card()), {"waiting": []})
            self.assertFalse(RA._resumed(self.ws, {"waiting": ["t"]}, "idle"))
        self.assertFalse(RA.verify_pending(mock.Mock(subject={"verify": {"outcome": "done", "at": "x"}}), 1.0))


class StillBlockedThenRecovered(Base):
    """A card that said "still blocked" is closed for a fresh one; when the block later lifts, the
    fresh card carries the incident's one closing notice, so nothing is suppressed."""

    def test_timeout_then_later_recovery_still_gets_its_notice(self):
        from hitl import renotify
        req = self.card()
        self.click(req, sw(self.ws, "haiku"))
        self.run_pending(Run())
        RA.verify_step(self.mgr, self.ws, CORE, blocked=True, now=time.time() + RA.VERIFY_S + 1)
        old = self.mgr.get(req.id)
        self.assertTrue(old.terminal)
        proj = {"event_id": "$card", "revision": old.revision, "notified_at": time.time() - 60}
        self.assertIsNone(renotify.due(old, proj, time.time()), "the closed card owes nothing")
        fresh = self.card(fresh=False)
        self.assertNotEqual(fresh.id, req.id)
        M.resolve_escalations(self.mgr, CORE, "❯ \n", "claude", workspace=self.ws)
        closed = self.mgr.get(fresh.id)
        self.assertEqual(closed.status, STATUS_RESOLVED)
        proj = {"event_id": "$card2", "revision": closed.revision, "notified_at": time.time() - 60}
        self.assertEqual(renotify.due(closed, proj, time.time()), renotify.RECOVERY,
                         "the recovery after a still-blocked card is told once")


class PausedCardWaitsForItsVerdict(Base):
    def test_the_paused_card_stays_open_until_it_says_how_the_retry_ended(self):
        now = time.time()
        core_limit_hold._write_hold(self.ws, {"status": "held", "kind": "usage-limit", "model": "opus",
                                              "until": now + 3600, "started_at": now - 60, "set_at": now - 60})
        req = RA.hold_card_step(self.mgr, self.ws, CORE, "claude")
        self.click(req, "retry_now")
        [receipt] = self.run_pending(Run(), now=now)
        self.assertEqual(receipt["outcome"], "done")
        self.assertIsNone(RA.hold_card_step(self.mgr, self.ws, CORE, "claude"), "the hold was lifted")
        self.assertFalse(self.mgr.get(req.id).terminal, "closed before the core was seen working")
        RA.verify_step(self.mgr, self.ws, CORE, blocked=False, state="idle")
        self.assertIn(RA.WORKING_AGAIN, self.mgr.get(req.id).message)
        RA.hold_card_step(self.mgr, self.ws, CORE, "claude")
        self.assertTrue(self.mgr.get(req.id).terminal)


#: What a ctl that takes the app's launch lock lists for `capabilities`.
LOCKING_CTL = "status\nrestart-core\nlaunch-lock\n"


class DesktopRestart(Base):
    """On a desktop install restart_core goes through the app's sutando-ctl.sh restart-core, which
    re-runs the app's launcher and keeps the gateway's session; elsewhere it stays start-cli."""

    def _desktop(self, ctl=True):
        app = self.ws / "app"
        repo = app / "engine" / "sutando"
        repo.mkdir(parents=True)
        if ctl:
            (app / "engine" / "sutando-ctl.sh").write_text("#!/bin/bash\n")
        self.desktop.stop()
        self.desktop = mock.patch.object(RA.sutando_config, "desktop_app_support", return_value=app)
        self.desktop.start()
        repo_patch = mock.patch.object(RA, "REPO", repo)
        repo_patch.start()
        self.addCleanup(repo_patch.stop)
        return app, repo

    def _restart(self, run=None):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        [receipt] = self.run_pending(run or Run(out=LOCKING_CTL))
        return receipt

    def test_a_desktop_install_restarts_the_core_through_the_apps_ctl(self):
        app, repo = self._desktop()
        run = Run(out=LOCKING_CTL)
        with mock.patch.dict(os.environ, {"SUTANDO_ENGINE_LAUNCH_LOCK_HELD": "/elsewhere/engine-launch.lock"}):
            receipt = self._restart(run)
        [(probe, probe_kw)] = run.calls
        self.assertEqual(probe, ["bash", str(app / "engine" / "sutando-ctl.sh"), "capabilities"])
        self.assertEqual(probe_kw["timeout"], RA.CTL_CAPABILITIES_TIMEOUT_S)
        self.assertEqual(self.spawned.command(), ["bash", str(app / "engine" / "sutando-ctl.sh"), "restart-core"])
        _, kw = self.spawned.calls[-1]
        self.assertEqual(kw["env"]["SUTANDO_APP_SUPPORT"], str(app))
        self.assertNotIn("SUTANDO_ENGINE_LAUNCH_LOCK_HELD", kw["env"], "a lock marker is never inherited")
        self.assertEqual((kw["env"]["SUTANDO_TMUX_SOCKET"], kw["env"]["SUTANDO_TMUX_SESSION"]), (SOCK, CORE))
        self.assertTrue(kw["start_new_session"], "still detached from the monitor")
        self.assertTrue(self.spawned.gate(), "still gated on the tap's claim")
        self.assertEqual(receipt["outcome"], "done")
        self.assertNotIn("via", receipt.get("recovery") or {}, "the receipt is unchanged")
        self.assertEqual(self.audit()[-1]["via"], RA.VIA_CTL)

    def test_elsewhere_the_core_restarts_through_start_cli(self):
        self._restart()
        self.assertEqual(self.spawned.command(), ["bash", str(SRC / "agent" / "start-cli.sh"), "--restart"])
        self.assertEqual(self.audit()[-1]["via"], RA.VIA_START_CLI)

    def test_the_apps_bundle_without_its_ctl_is_refused_and_launches_nothing(self):
        # start-cli --restart would kill the gateway's session, which it does not recreate.
        self._desktop(ctl=False)
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        run = Run()
        [receipt] = self.run_pending(run)
        self.assertEqual((self.spawned.calls, run.calls), ([], []), "nothing is launched")
        self.assertEqual(receipt["outcome"], "refused")
        self.assertIn("Restart the core from the desktop app", self.mgr.get(req.id).message)
        self.assertEqual(self.audit()[-1]["via"], RA.VIA_NO_CTL)

    def test_a_held_retry_on_the_bundle_without_its_ctl_launches_nothing_and_stays_paused(self):
        self._desktop(ctl=False)
        now = time.time()
        core_limit_hold._write_hold(self.ws, {"status": "held", "kind": "usage-limit", "model": "opus",
                                              "until": now + 3600, "started_at": now - 60, "set_at": now - 60,
                                              "source": core_limit_hold.PANE_SOURCE})
        req = self.card()
        self.click(req, "retry_now")
        [receipt] = self.run_pending(Run(), now=now)
        self.assertEqual(self.spawned.calls, [])
        self.assertEqual(receipt["outcome"], "refused")
        self.assertIn("Restart the core from the desktop app", self.mgr.get(req.id).message)
        self.assertIsNotNone(core_limit_hold.active_hold(self.ws, now + 1), "the hold stays")

    def test_a_standalone_clone_with_the_apps_dir_exported_falls_back_to_start_cli(self):
        app, repo = self._desktop()
        clone = self.ws / "clone" / "sutando"
        clone.mkdir(parents=True)
        with mock.patch.object(RA, "REPO", clone):
            self._restart()
        self.assertEqual(self.spawned.command(), ["bash", str(clone / "src" / "agent" / "start-cli.sh"), "--restart"])
        self.assertEqual(self.audit()[-1]["via"], RA.VIA_FALLBACK)

    def test_a_standalone_restart_under_a_configured_app_dir_says_so_on_the_card_and_audit(self):
        # A dev override (an exported app dir, or a checkout outside the bundle) is never silent.
        self._desktop()
        clone = self.ws / "clone" / "sutando"
        clone.mkdir(parents=True)
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        with mock.patch.object(RA, "REPO", clone):
            [receipt] = self.run_pending(Run())
        self.assertEqual(receipt["outcome"], "done")
        self.assertIn("not the desktop app's own copy, so the standalone restart was used",
                      self.mgr.get(req.id).message)
        line = self.audit()[-1]
        self.assertEqual((line["via"], line.get("via_reason")), (RA.VIA_FALLBACK, "not-bundle"))

    def test_without_an_app_dir_the_card_carries_no_standalone_note(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        self.run_pending(Run())
        self.assertNotIn("standalone", self.mgr.get(req.id).message)
        self.assertNotIn("via_reason", self.audit()[-1])

    def test_a_ctl_without_the_launch_lock_is_refused_and_launches_nothing(self):
        # An engine newer than its app: the ctl predates the lock (no `capabilities` verb: usage, exit 2),
        # lists no launch-lock, or does not answer in time. Never start-cli on the bundle either.
        for name, run in (("old ctl", Run(rc=2)), ("no launch-lock", Run(out="status\nrestart-core\n")),
                          ("hangs", Hang())):
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                self._desktop()
                req = self.card(kind="unknown", prompt=UNKNOWN)
                self.click(req, "restart_core")
                [receipt] = self.run_pending(run)
                self.assertEqual(self.spawned.calls, [], "nothing is launched")
                self.assertEqual([c[0][-1] for c in run.calls], ["capabilities"], "only the read-only probe ran")
                self.assertEqual(receipt["outcome"], "refused")
                self.assertIn("Restart the core from the desktop app", self.mgr.get(req.id).message)
                self.assertEqual(self.audit()[-1]["via"], RA.VIA_NO_LOCK)

    def test_a_held_retry_on_a_ctl_without_the_lock_keeps_its_hold(self):
        self._desktop()
        now = time.time()
        core_limit_hold._write_hold(self.ws, {"status": "held", "kind": "usage-limit", "model": "opus",
                                              "until": now + 3600, "started_at": now - 60, "set_at": now - 60,
                                              "source": core_limit_hold.PANE_SOURCE})
        req = self.card()
        self.click(req, "retry_now")
        [receipt] = self.run_pending(Run(rc=2), now=now)
        self.assertEqual((self.spawned.calls, receipt["outcome"]), ([], "refused"))
        self.assertIsNotNone(core_limit_hold.active_hold(self.ws, now + 1), "the hold stays")

    def test_a_busy_launch_lock_says_a_restart_is_already_running_keeps_the_hold_and_retries_nothing(self):
        # sutando-ctl.sh restart-core exits 12 when another launcher run holds the launch lock.
        self._desktop()
        now = time.time()
        core_limit_hold._write_hold(self.ws, {"status": "held", "kind": "usage-limit", "model": "opus",
                                              "until": now + 3600, "started_at": now - 60, "set_at": now - 60,
                                              "source": core_limit_hold.PANE_SOURCE})
        req = self.card()
        self.click(req, "retry_now")
        [receipt] = self.run_pending(Run(out=LOCKING_CTL), now=now)
        self.assertEqual((receipt["outcome"], self.spawned.command()[-1]), ("done", "restart-core"))
        self.answer(detached_action.FAILED, rc=RA.CTL_LAUNCH_LOCK_BUSY, reason="exit")
        self.assertEqual(RA.verify_step(self.mgr, self.ws, CORE, blocked=True, now=now + 5), [req.id])
        card = self.mgr.get(req.id)
        self.assertIn("already being restarted", card.message)
        self.assertIn("Try again", card.message)
        self.assertNotIn("did not complete", card.message)
        self.assertEqual((card.subject["receipt"]["outcome"], card.status), ("failed", STATUS_RESOLVED))
        self.assertEqual(recovery.read_receipt(self.ws, req.id)["outcome"], "failed", "the receipt on disk too")
        self.assertIsNotNone(core_limit_hold.active_hold(self.ws, now + 6), "the hold stays")
        self.assertEqual(len(self.spawned.calls), 1, "no automatic retry")
        RA.verify_step(self.mgr, self.ws, CORE, blocked=True, now=now + 10)
        self.assertEqual(len(self.spawned.calls), 1, "no automatic retry on a later tick either")

    def test_exit_12_from_start_cli_is_an_ordinary_failure(self):
        req = self.card(kind="unknown", prompt=UNKNOWN)
        self.click(req, "restart_core")
        self.run_pending(Run())
        self.answer(detached_action.FAILED, rc=RA.CTL_LAUNCH_LOCK_BUSY, reason="exit")
        RA.verify_step(self.mgr, self.ws, CORE, blocked=True)
        self.assertIn("did not complete (exit 12)", self.mgr.get(req.id).message)

    def test_a_runtime_switch_on_a_desktop_install_stays_start_cli(self):
        _, repo = self._desktop()
        req = self.card(runtime="codex")
        self.click(req, "switch_runtime:claude")
        self.run_pending(Run())
        self.assertEqual(self.spawned.command(),
                         ["bash", str(repo / "src" / "agent" / "start-cli.sh"), "--runtime", "claude", "--restart"])
        self.assertNotIn("via", self.audit()[-1])


class WireReceipt(unittest.TestCase):
    def test_only_a_known_outcome_and_its_time_leave_the_host(self):
        from hitl.schema import HumanRequirement
        base = dict(kind="core-blocked", runtime="claude", message="m", guard="g")
        req = HumanRequirement(**base, subject={"receipt": {"outcome": "stale", "at": 5, "reason": "x"},
                                                "verify": {"waiting": ["t"]}})
        self.assertEqual(req.to_wire()["subject"], {"receipt": {"outcome": "stale", "at": 5}})
        # A card closed for a fresh one says so, so a client never reads its done receipt as success.
        closed = HumanRequirement(**base, subject={"receipt": {"outcome": "done", "at": 5}, "superseded": True})
        self.assertEqual(closed.to_wire()["subject"], {"receipt": {"outcome": "done", "at": 5}, "superseded": True})
        for odd in ("yes", 1, None):
            self.assertNotIn("superseded", HumanRequirement(**base, subject={"superseded": odd}).to_wire().get("subject", {}))
        for receipt in ({"outcome": "maybe"}, "done", {"outcome": "done", "at": True}):
            wire = HumanRequirement(**base, subject={"receipt": receipt}).to_wire()
            self.assertEqual(wire.get("subject", {}).get("receipt", {}).get("at"), None)
            if not (isinstance(receipt, dict) and receipt.get("outcome") == "done"):
                self.assertNotIn("subject", wire)


class Contract(Base):
    def test_action_fixture_is_a_recovery_click_and_its_hand_off_keeps_every_field(self):
        wire = json.loads((CONTRACTS / "action.json").read_text())[REPLY_FIELD]
        reply = ActionReply.from_wire(wire)
        self.assertEqual(recovery.parse(reply.action_id), ("switch_model", "o4b876be2aaa6"))
        body = json.loads(recovery.write_action(self.ws, reply, OWNER, DM, now=1).read_text())
        self.assertEqual({k: body[k] for k in wire}, wire)

    def test_receipt_writer_produces_the_fixture(self):
        fixture = json.loads((CONTRACTS / "receipt.json").read_text())
        req = self.card()
        self.click(req, fixture["action_id"])
        [handoff] = list(recovery.actions_dir(self.ws).glob("*.json"))
        handoff.write_text(json.dumps({**json.loads(handoff.read_text()), "at": fixture["at"] - 5}))  # tapped then
        [receipt] = self.run_pending(Run(), now=fixture["at"])
        self.assertEqual(receipt, {**fixture, "hitl_id": req.id, "incident": RA.incident_for(req)})
        self.assertEqual(recovery.read_receipt(self.ws, req.id), receipt)
        self.assertEqual(set(recovery.OUTCOMES), {"done", "refused", "stale", "failed"})


if __name__ == "__main__":
    unittest.main()
