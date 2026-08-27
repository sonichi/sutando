#!/usr/bin/env python3
"""notify_reviewers refuses everything rule 9 forbids, and plans correctly.

Run: python3 tests/sci-notify-reviewers.test.py   (stdlib only)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import pathlib
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = (REPO / "skills" / "collaboration-intelligence" / "scripts"
          / "notify_reviewers.py")


# One managed root for every fixture; NamedTemporaryFile(delete=False) leaked
# one roster JSON per call, six per run, for the lifetime of the machine.
_TMP = tempfile.TemporaryDirectory()


def run(roster: "dict | None", *args):
    env = {**os.environ}
    path = pathlib.Path(tempfile.mkdtemp(dir=_TMP.name)) / "roster.json"
    if roster is not None:
        path.write_text(json.dumps(roster))
        env["SUTANDO_SCI_ROSTER"] = str(path)
    else:
        env["SUTANDO_SCI_ROSTER"] = str(path) + ".missing"
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=30, env=env)


GOOD = {"rui": {"human": "@rui:x", "stand": "@sutando-rui:x",
                "room": "!triage:x", "allowlisted": True}}


class NotifyReviewers(unittest.TestCase):
    def test_plan_mode_builds_a_stand_mention(self):
        p = run(GOOD, "--reviewers", "rui", "--message", "re-review #1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("mention @sutando-rui:x", p.stdout)
        self.assertIn("!triage:x", p.stdout)
        self.assertIn("cc @rui:x", p.stdout)

    def test_unknown_reviewer_refused_exit_2(self):
        p = run(GOOD, "--reviewers", "ghost", "--message", "m")
        self.assertEqual(p.returncode, 2)
        self.assertIn("do not guess", p.stderr)

    def test_human_only_entry_refused_exit_3(self):
        p = run({"kim": {"human": "@kim:x", "room": "!r:x"}},
                "--reviewers", "kim", "--message", "m")
        self.assertEqual(p.returncode, 3)
        self.assertIn("not Stand addressing", p.stderr)

    def test_known_off_allowlist_refused_exit_4(self):
        p = run({"mini": {"stand": "@mini:x", "room": "!r:x",
                          "allowlisted": False}},
                "--reviewers", "mini", "--message", "m")
        self.assertEqual(p.returncode, 4)
        self.assertIn("route through the owner", p.stderr)

    def test_missing_roster_names_the_path_and_refuses(self):
        p = run(None, "--reviewers", "rui", "--message", "m")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("never guess", p.stderr)

    def test_one_bad_entry_never_starves_the_batch(self):
        roster = dict(GOOD)
        roster["mini"] = {"stand": "@mini:x", "room": "!r:x",
                          "allowlisted": False}
        p = run(roster, "--reviewers", "rui,mini", "--message", "m")
        self.assertEqual(p.returncode, 4)          # refusal still visible
        self.assertIn("mention @sutando-rui:x", p.stdout)  # rui still planned
        self.assertIn("OFF-ALLOWLIST 'mini'", p.stderr)

def run_send(stub_payload, roster=None, stub_stderr=""):
    """Drive --send against a STUB room_ops. The script resolves room_ops as
    parents[3] of its own path, so the copy must sit in a matching tree."""
    root = pathlib.Path(tempfile.mkdtemp(dir=_TMP.name))
    (root / "skills" / "collaboration-intelligence" / "scripts").mkdir(parents=True)
    (root / "skills" / "agent-room-ops").mkdir(parents=True)
    copy = root / "skills/collaboration-intelligence/scripts/notify_reviewers.py"
    copy.write_text(SCRIPT.read_text())
    (root / "skills/agent-room-ops/room_ops.py").write_text(
        "import sys\n"
        f"sys.stdout.write({stub_payload!r})\n"
        f"sys.stderr.write({stub_stderr!r})\n"
        "sys.exit(0)\n")          # rc 0 + empty stderr: the real refusal shape
    rp = root / "roster.json"
    rp.write_text(json.dumps(roster or GOOD))
    env = {**os.environ, "SUTANDO_SCI_ROSTER": str(rp)}
    return subprocess.run([sys.executable, str(copy), "--send",
                           "--reviewers", "rui", "--message", "m"],
                          capture_output=True, text=True, timeout=30, env=env)


class SilentRefusal(unittest.TestCase):
    """room_ops reports refusals IN-BAND: rc 0, empty stderr, ok:false+reason.
    Printing stderr alone renders every such refusal as a blank line."""

    def test_in_band_reason_is_surfaced_not_swallowed(self):
        p = run_send('{"ok": false, "members": [], "reason": "no gateway configured"}')
        self.assertEqual(p.returncode, 1)
        self.assertIn("no gateway configured", p.stderr)
        self.assertNotIn("STDERR=\n", p.stderr)

    def test_the_gateway_reason_is_surfaced_verbatim(self):
        # Surfaced, not interpreted: the producer emits this whenever the base
        # URL is empty and says nothing about why, so we must not name a cause.
        p = run_send('{"ok": false, "reason": "no gateway configured"}')
        self.assertIn("reason=no gateway configured", p.stderr)
        self.assertNotIn("env is not loaded", p.stderr)

    def test_a_reasonless_failure_still_says_something(self):
        p = run_send('{"ok": false}')
        self.assertEqual(p.returncode, 1)
        self.assertIn("no reason reported", p.stderr)

    def test_a_real_stderr_survives_an_unusable_payload(self):
        # The placeholder must not occupy `reason` — a caller debugging a crash
        # needs the traceback, not our generic word for "did not parse".
        p = run_send('boom', stub_stderr="ConnectionRefusedError: [Errno 61]")
        self.assertEqual(p.returncode, 1)
        self.assertIn("ConnectionRefusedError", p.stderr)

    def test_unparseable_output_is_not_reported_as_success(self):
        p = run_send('not json at all')
        self.assertEqual(p.returncode, 1)
        self.assertIn("unparseable", p.stderr)

    def test_non_object_payloads_do_not_crash_the_notifier(self):
        # room_ops should never emit these; a notifier that dies on one reports
        # nothing at all, which is the failure this PR exists to remove.
        for payload in ('[]', '"hello"', 'null', '{"reason": 1}'):
            with self.subTest(payload=payload):
                p = run_send(payload)
                self.assertEqual(p.returncode, 1, p.stderr)
                self.assertIn("ok=False", p.stderr)
                self.assertNotIn("Traceback", p.stderr)

    def test_a_non_string_event_id_does_not_crash_the_success_path(self):
        # `event[:24]` slices, so a non-string id raises here and nowhere else —
        # this is the coercion that is load-bearing now the hint is gone.
        p = run_send('{"ok": true, "event_id": 12345}')
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("12345", p.stdout)
        self.assertNotIn("Traceback", p.stderr)

    def test_a_non_string_reason_is_still_reported(self):
        p = run_send('{"ok": false, "reason": 1}')
        self.assertIn("reason=1", p.stderr)
        self.assertEqual(p.returncode, 1, p.stderr)

    def test_success_still_reports_the_event_id(self):
        p = run_send('{"ok": true, "event_id": "$abc123"}')
        self.assertEqual(p.returncode, 0)
        self.assertIn("ok=True", p.stdout)
        self.assertIn("$abc123", p.stdout)



def run_room(members_payload, *extra, roster=None):
    """Drive the room-scoped path against a stub room_ops that answers `members`
    and `mention` DIFFERENTLY — the single-payload stub above cannot express a
    roster read and a send in one run."""
    root = pathlib.Path(tempfile.mkdtemp(dir=_TMP.name))
    (root / "skills" / "collaboration-intelligence" / "scripts").mkdir(parents=True)
    (root / "skills" / "agent-room-ops").mkdir(parents=True)
    copy = root / "skills/collaboration-intelligence/scripts/notify_reviewers.py"
    copy.write_text(SCRIPT.read_text())
    (root / "skills/agent-room-ops/room_ops.py").write_text(
        "import sys\n"
        "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        f"sys.stdout.write({members_payload!r} if cmd == 'members' "
        "else '{\"ok\": true, \"event_id\": \"$e\"}')\n"
        "sys.exit(0)\n")
    rp = root / "roster.json"
    rp.write_text(json.dumps(roster or GOOD))
    env = {**os.environ, "SUTANDO_SCI_ROSTER": str(rp)}
    return subprocess.run([sys.executable, str(copy), "--send",
                           "--reviewers", "rui", "--message", "m", *extra],
                          capture_output=True, text=True, timeout=30, env=env)


_PRESENT = '{"ok": true, "members": [{"user_id": "@sutando-rui:x"}, {"user_id": "@other:x"}]}'
_ABSENT  = '{"ok": true, "members": [{"user_id": "@other:x"}, {"user_id": "@third:x"}]}'
_EMPTY_OK = '{"ok": true, "members": []}'
_DEGRADED = '{"ok": false, "reason": "no gateway configured"}'


def run_room_per_room(per_room: dict, default, *extra, roster=None):
    """Like run_room, but the stub answers `members <room>` DIFFERENTLY per room.
    The single-payload stub cannot express "absent here, present there", so a test
    written on it passes whether or not the recorded room was ever queried."""
    root = pathlib.Path(tempfile.mkdtemp(dir=_TMP.name))
    (root / "skills" / "collaboration-intelligence" / "scripts").mkdir(parents=True)
    (root / "skills" / "agent-room-ops").mkdir(parents=True)
    copy = root / "skills/collaboration-intelligence/scripts/notify_reviewers.py"
    copy.write_text(SCRIPT.read_text())
    seen = root / "queried.txt"
    (root / "skills/agent-room-ops/room_ops.py").write_text(
        "import sys\n"
        f"PER = {per_room!r}\n"
        f"DEFAULT = {default!r}\n"
        "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "if cmd == 'members':\n"
        "    room = sys.argv[2] if len(sys.argv) > 2 else ''\n"
        f"    open({str(seen)!r}, 'a').write(room + chr(10))\n"
        "    sys.stdout.write(PER.get(room, DEFAULT))\n"
        "else:\n"
        "    sys.stdout.write('{\"ok\": true, \"event_id\": \"$e\"}')\n"
        "sys.exit(0)\n")
    rp = root / "roster.json"
    rp.write_text(json.dumps(roster or GOOD))
    env = {**os.environ, "SUTANDO_SCI_ROSTER": str(rp)}
    proc = subprocess.run([sys.executable, str(copy), "--send",
                           "--reviewers", "rui", "--message", "m", *extra],
                          capture_output=True, text=True, timeout=30, env=env)
    proc.queried = seen.read_text().split() if seen.exists() else []
    return proc


class RoomScopedPresence(unittest.TestCase):
    """A Stand mxid is scoped to a ROOM. room_ops has no unknown-handle branch,
    so mentioning an absent mxid resolves to nothing and still reports ok."""

    def test_absent_from_its_own_recorded_room_refuses_instead_of_sending(self):
        p = run_room(_ABSENT)
        self.assertIn("ABSENT from", p.stderr)
        self.assertNotEqual(p.returncode, 0)

    def test_present_in_its_own_room_still_sends(self):
        # Control: the guard must not refuse the ordinary case.
        p = run_room(_PRESENT)
        self.assertNotIn("ABSENT from", p.stderr)
        self.assertIn("ok=True", p.stdout)

    def test_room_arg_naming_a_room_the_stand_is_absent_from_refuses_and_says_where(self):
        # Absent in the requested room, PRESENT in the recorded one — and the
        # recorded room must actually have been queried before it is recommended.
        p = run_room_per_room({"!elsewhere:x": _ABSENT, "!triage:x": _PRESENT}, _ABSENT,
                              "--room", "!elsewhere:x")
        self.assertIn("NOT REACHABLE in !elsewhere:x", p.stderr)
        self.assertIn("IS a member of !triage:x", p.stderr)
        self.assertIn("!triage:x", p.queried)         # verified, not asserted
        self.assertEqual(p.returncode, 5)             # distinct from other refusals

    def test_room_arg_where_the_stand_is_present_addresses_them_there(self):
        p = run_room(_PRESENT, "--room", "!elsewhere:x")
        self.assertNotIn("NOT REACHABLE", p.stderr)
        self.assertIn("ok=True", p.stdout)

    def test_unusable_members_payload_is_UNVERIFIED_not_an_absence(self):
        # Fail-open by design: a broken/absent gateway must not convert
        # "this mention reaches nobody" into "nothing reaches anybody".
        for payload in ("[]", '"hello"', "null", "not json"):
            with self.subTest(payload=payload):
                p = run_room(payload)
                self.assertNotIn("ABSENT from", p.stderr)
                self.assertIn("ok=True", p.stdout)
                # ...and it must NOT read as a checked send.
                self.assertIn("UNVERIFIED", p.stderr)

    def test_room_arg_on_an_unreadable_roster_relocates_but_says_UNVERIFIED(self):
        # The fifth outcome: --room is passed to GET a presence guarantee, so an
        # unread roster must not print the verified-send line.
        for payload in ("[]", "null", "not json"):
            with self.subTest(payload=payload):
                p = run_room(payload, "--room", "!elsewhere:x")
                self.assertIn("UNVERIFIED for !elsewhere:x", p.stderr)
                self.assertNotIn("NOT REACHABLE", p.stderr)
                self.assertIn("ok=True", p.stdout)

    def test_a_verified_present_send_carries_NO_unverified_label(self):
        # Control for the two above: the label must not be unconditional.
        p = run_room(_PRESENT, "--room", "!elsewhere:x")
        self.assertNotIn("UNVERIFIED", p.stderr)
        self.assertIn("ok=True", p.stdout)

    def test_absent_from_BOTH_rooms_does_not_recommend_the_recorded_room(self):
        # The blind spot: with the Stand absent everywhere, naming the recorded
        # room sends the operator to a second room that also reaches nobody.
        p = run_room_per_room({"!elsewhere:x": _ABSENT, "!triage:x": _ABSENT}, _ABSENT,
                              "--room", "!elsewhere:x")
        self.assertIn("NOT REACHABLE in !elsewhere:x", p.stderr)
        self.assertIn("absent from its recorded room !triage:x too", p.stderr)
        self.assertNotIn("post there deliberately", p.stderr)
        self.assertEqual(p.returncode, 5)

    def test_unreadable_recorded_room_is_not_recommended_either(self):
        p = run_room_per_room({"!elsewhere:x": _ABSENT, "!triage:x": "not json"}, _ABSENT,
                              "--room", "!elsewhere:x")
        self.assertIn("could not be checked", p.stderr)
        self.assertNotIn("post there deliberately", p.stderr)
        self.assertEqual(p.returncode, 5)

    def test_successful_EMPTY_membership_is_an_absence_not_unverified(self):
        # ok:true with members:[] is a positive absence. Treating it as an
        # unreadable instrument sends a mention that resolves to nobody.
        p = run_room(_EMPTY_OK)
        self.assertIn("ABSENT from", p.stderr)
        self.assertNotIn("UNVERIFIED", p.stderr)
        self.assertNotEqual(p.returncode, 0)

    def test_degraded_ok_false_is_unverified_not_an_absence(self):
        # The other half: ok:false is the instrument failing, so fail OPEN.
        p = run_room(_DEGRADED)
        self.assertNotIn("ABSENT from", p.stderr)
        self.assertIn("UNVERIFIED", p.stderr)
        self.assertIn("ok=True", p.stdout)

    def test_a_members_list_without_ok_is_unverified(self):
        # A list alone is not the contract; `ok` is what licenses reading it.
        p = run_room('{"members": [{"user_id": "@other:x"}]}')
        self.assertNotIn("ABSENT from", p.stderr)
        self.assertIn("UNVERIFIED", p.stderr)

if __name__ == "__main__":
    unittest.main(verbosity=2)
