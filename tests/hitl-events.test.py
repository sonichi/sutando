"""Ingest contract for driver-dropped RuntimeEvents: create once, idempotent
re-read, tombstone -> resolved (if a human acted) or expired (if not), jump
action always present, malformed files never crash the pass."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hitl.events import (  # noqa: E402
    JUMP_ACTION_ID,
    SCHEMA,
    events_dir,
    ingest,
)
from hitl.manager import HitlManager, HitlStore  # noqa: E402
from hitl.schema import ActionReply  # noqa: E402


def event(session="core-2", guard="g1", kind="permission", cleared=False, **extra):
    d = {"schema": SCHEMA, "session": session, "socket": "/tmp/s.sock", "runtime": "claude",
         "kind": kind, "prompt": "Do you want to proceed?", "guard": guard, "observed_ms": 1,
         "options": [{"id": "1", "label": "Yes"}, {"id": "4", "label": "No"}]}
    if cleared:
        d = {"schema": SCHEMA, "session": session, "guard": guard, "cleared": True}
    d.update(extra)
    return d


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.mgr = HitlManager(HitlStore(self.ws / "state" / "hitl" / "store"))
        events_dir(self.ws).mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def drop(self, name, ev):
        (events_dir(self.ws) / f"{name}.json").write_text(json.dumps(ev))

    def test_creates_requirement_with_tui_options_and_jump(self):
        self.drop("core-2-g1", event())
        c = ingest(self.mgr, self.ws)
        self.assertEqual(c["created"], 1)
        [req] = self.mgr.active()
        self.assertEqual((req.kind, req.runtime, req.guard), ("permission", "claude", "g1"))
        self.assertIn("core-2", req.message)
        kinds = [(a.id, a.kind) for a in req.actions]
        self.assertIn(("1", "tui_select"), kinds)
        self.assertEqual(req.actions[-1].id, JUMP_ACTION_ID)

    def test_reingest_is_idempotent(self):
        self.drop("core-2-g1", event())
        ingest(self.mgr, self.ws)
        c = ingest(self.mgr, self.ws)
        self.assertEqual((c["created"], c["skipped"]), (0, 1))
        self.assertEqual(len(self.mgr.active()), 1)

    def test_tombstone_expires_untouched_and_resolves_acted_on(self):
        self.drop("core-2-g1", event(session="core-2", guard="g1"))
        self.drop("core-3-g2", event(session="core-3", guard="g2"))
        ingest(self.mgr, self.ws)
        acted = next(r for r in self.mgr.active() if r.guard == "g2")
        self.mgr.apply_action(ActionReply(hitl_id=acted.id, expected_revision=acted.revision, action_id="1", guard="g2"))
        self.drop("core-2-g1", event(guard="g1", cleared=True))
        self.drop("core-3-g2", event(session="core-3", guard="g2", cleared=True))
        c = ingest(self.mgr, self.ws)
        self.assertEqual((c["expired"], c["resolved"]), (1, 1))
        self.assertEqual(self.mgr.active(), [])
        self.assertEqual(list(events_dir(self.ws).glob("*.json")), [])  # tombstones consumed

    def test_acp_style_event_keeps_option_kinds_and_subject(self):
        # An ACP driver names its option kinds and what is being asked; the
        # Manager's policy reads the subject, the driver reads chosen_action.
        self.drop("worker-7-tc1", event(session="worker-7", guard="toolCall:1", runtime="acp",
                                         subject={"tool": "Read", "input": "/x"},
                                         options=[{"id": "allow", "label": "Allow", "kind": "allow_once"},
                                                  {"id": "deny", "label": "Deny", "kind": "reject_once"}]))
        ingest(self.mgr, self.ws)
        [req] = self.mgr.active()
        self.assertEqual(req.subject, {"tool": "Read", "input": "/x"})
        self.assertEqual([(a.id, a.kind) for a in req.actions[:2]], [("allow", "allow_once"), ("deny", "reject_once")])
        self.assertEqual(req.actions[-1].kind, "open_terminal")  # the jump floor stays

    def test_option_without_kind_is_still_a_tui_keystroke(self):
        self.drop("core-2-g1", event())
        ingest(self.mgr, self.ws)
        [req] = self.mgr.active()
        self.assertEqual({a.kind for a in req.actions[:-1]}, {"tui_select"})
        self.assertEqual(req.subject, {})

    def test_repaint_expires_the_old_dialog_and_mints_a_new_card_with_new_options(self):
        # TustinOC's second repro: the guard moved to screen BBB; the old card's
        # options must never be typed into the new screen.
        self.drop("core-2-AAA", event(session="core-2", guard="tui:AAA",
                                       options=[{"id": "1", "label": "Keep the file"}, {"id": "2", "label": "Delete the file"}]))
        ingest(self.mgr, self.ws)
        [old] = self.mgr.active()
        self.drop("core-2-BBB", event(session="core-2", guard="tui:BBB",
                                       options=[{"id": "1", "label": "Deploy to production"}, {"id": "2", "label": "Cancel"}]))
        c = ingest(self.mgr, self.ws)
        self.assertEqual((c["created"], c.get("superseded")), (1, 1))
        self.assertEqual(self.mgr.get(old.id).status, "expired")
        [new] = self.mgr.active()
        self.assertNotEqual(new.id, old.id)
        self.assertEqual((new.guard, new.revision), ("tui:BBB", 1))
        self.assertEqual([a.label for a in new.actions[:2]], ["Deploy to production", "Cancel"])
        # A click carrying the old card's revision+guard is rejected by the gate.
        from hitl.schema import StaleRequirementError
        with self.assertRaises(StaleRequirementError):
            self.mgr.apply_action(ActionReply(hitl_id=old.id, expected_revision=old.revision, action_id="1", guard="tui:AAA"))

    def test_unknown_kind_still_gets_a_jump_only_card(self):
        self.drop("x", event(kind="unknown", prompt=None, options=[]))
        ingest(self.mgr, self.ws)
        [req] = self.mgr.active()
        self.assertEqual([a.kind for a in req.actions], ["open_terminal"])

    def test_bad_files_are_counted_not_fatal(self):
        (events_dir(self.ws) / "junk.json").write_text("{not json")
        (events_dir(self.ws) / "other.json").write_text(json.dumps({"schema": "something.else"}))
        c = ingest(self.mgr, self.ws)
        self.assertEqual(c["bad"], 2)

    def test_missing_dir_is_a_noop(self):
        self.assertEqual(ingest(self.mgr, self.ws / "nope")["created"], 0)


if __name__ == "__main__":
    unittest.main()
