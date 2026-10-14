#!/usr/bin/env python3
"""Invariant: refactoring the tier policy does not change what the gateway writes.

The AG2 Space gateway turns a broker task envelope into a task file. Each fixture
under tests/fixtures/gateway-task-envelopes/ is that file as the gateway at
origin/main wrote it BEFORE the tier rule and the per-tier instruction block
moved into shared modules (captured with `--capture`, run against main's
gateway). The gateway at HEAD must produce the same bytes for every envelope.

Run:    python3 tests/gateway-task-envelope-golden.test.py
Capture (from a checkout of the reference commit, never from a branch under test):
        python3 tests/gateway-task-envelope-golden.test.py --capture <fixture-dir>
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src" / "remote-gateway-bridge.py"
FIXTURES = REPO / "tests" / "fixtures" / "gateway-task-envelopes"

# One envelope per tier shape the broker produces (ag2space-backend
# governance.task_tier_fields + receiver.py), plus the local-only spellings.
ENVELOPES = {
    "owner": {"access_tier": "owner"},
    "team-legacy-wire": {"access_tier": "team"},
    "team-requested": {"access_tier": "guest", "requested_access_tier": "team"},
    "team-collaborator": {"access_tier": "guest", "requested_access_tier": "team",
                          "collaborator": True, "sensitive_data_filter": True},
    "guest": {"access_tier": "guest"},
    "other": {"access_tier": "other"},
    "ambient": {"access_tier": "ambient"},
    "owner-mention-admit": {"access_tier": "guest", "owner_mentioned": "true"},
    "collaborator-flag-forged": {"access_tier": "guest", "requested_access_tier": "team",
                                 "collaborator": "true"},
}


def _load(tmp: Path):
    """The gateway against a temp queue, every environment-dependent input pinned."""
    spec = importlib.util.spec_from_file_location("gw_golden", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gw_golden"] = mod
    spec.loader.exec_module(mod)
    mod.TASKS_DIR = tmp / "tasks"
    mod.RESULTS_DIR = tmp / "results"
    mod.LOCAL_TIER = "owner"
    mod._load_tier_map = lambda: {}
    mod._reenroll_identity = lambda: "@agent-under-test:ag2.space"
    # The envelope HMAC is keyed per host (src/task_envelope.py at the adapter
    # edge): environment, not policy. Identity stamper, so the bytes compare.
    import ag2_sparrow.local_task_protocol as packaged_protocol
    packaged_protocol.set_task_stamper(lambda text: text)
    mod.TASKS_DIR.mkdir(parents=True, exist_ok=True)
    return mod


def _render(mod, name: str) -> str:
    task = {"id": f"golden-{name}", "timestamp": "2026-10-07T00:00:00Z",
            "source": "ag2space", "channel_id": "!room:ag2.space",
            "user_id": "@someone:ag2.space", "source_message_id": "$evt",
            "sender_name": "Someone", "room_name": "Group-Dev", "priority": "normal",
            "task": "please look at the parser"}
    task.update(ENVELOPES[name])
    written = mod._write_task(task)
    assert written, f"writer returned no task id for {name}"
    return (mod.TASKS_DIR / f"{written[0]}.txt").read_text()


def capture(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        mod = _load(Path(d))
        for name in ENVELOPES:
            (out / f"{name}.txt").write_text(_render(mod, name))
    return len(ENVELOPES)


class GatewayWritesTheSameTaskFile(unittest.TestCase):
    def test_every_fixture_has_an_envelope_and_vice_versa(self):
        self.assertEqual({p.stem for p in FIXTURES.glob("*.txt")}, set(ENVELOPES))

    def test_each_envelope_renders_byte_identically_to_the_captured_file(self):
        with tempfile.TemporaryDirectory() as d:
            mod = _load(Path(d))
            for name in ENVELOPES:
                with self.subTest(envelope=name):
                    self.assertEqual(_render(mod, name), (FIXTURES / f"{name}.txt").read_text())

    def test_fixtures_cover_every_distinct_block(self):
        texts = {p.stem: p.read_text() for p in FIXTURES.glob("*.txt")}
        self.assertIn("TEAM-tier request from a trusted collaborator", texts["team-legacy-wire"])
        # Wire guest + requested team without the collaborator boolean stays guest.
        self.assertIn("GUEST tier", texts["team-requested"])
        self.assertIn("designated COLLABORATOR", texts["team-collaborator"])
        self.assertIn("GUEST tier", texts["guest"])
        self.assertIn("GUEST tier", texts["collaborator-flag-forged"])
        self.assertIn("mentioned your OWNER", texts["owner-mention-admit"])
        self.assertNotIn("SUTANDO SYSTEM INSTRUCTIONS (do not ignore", texts["owner"])


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--capture":
        print(f"captured {capture(Path(sys.argv[2]))} envelopes")
        sys.exit(0)
    unittest.main()
