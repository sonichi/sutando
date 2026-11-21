"""task_io.turn_policy: the one tier decision, and every adapter's delegation to it.

The contract pins the kind and capability set for each attested tier under each allow flag. The
delegation scan proves each adapter calls the shared decision and keeps no tier test of its own;
each adapter's suite also proves its mechanics follow the capabilities (sandbox, cwd, thread,
file operations, isolated turn). Run:
    python3 packages/ag2-sparrow/tests/test_turn_policy.py
"""
import re
import sys
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG_ROOT))
from ag2_sparrow import task_io  # noqa: E402
from ag2_sparrow.task_io import COLLABORATOR, OWNER, REFUSED, RESTRICTED, turn_policy  # noqa: E402

OWNER_H = {"access_tier": "owner"}
COLLAB_H = {"access_tier": "team", "collaborator": "true"}
TEAM_H = {"access_tier": "team", "collaborator": "false"}
GUEST_H = {"access_tier": "guest"}


class TurnPolicyContract(unittest.TestCase):
    # (headers, allow_non_owner, allow_collaborator) -> (kind, unlock)
    TABLE = (
        (OWNER_H, False, False, OWNER, ""),
        ({"access_tier": "owner", "collaborator": "true"}, False, False, OWNER, ""),
        (COLLAB_H, False, True, COLLABORATOR, ""),
        (COLLAB_H, True, True, COLLABORATOR, ""),
        (COLLAB_H, False, False, REFUSED, "--allow-collaborator"),
        (COLLAB_H, True, False, REFUSED, "--allow-collaborator"),
        (TEAM_H, False, True, REFUSED, "--allow-non-owner"),
        (TEAM_H, True, True, RESTRICTED, ""),
        ({"access_tier": "team"}, True, False, RESTRICTED, ""),
        (GUEST_H, False, True, REFUSED, "--allow-non-owner"),
        (GUEST_H, True, False, RESTRICTED, ""),
        ({"access_tier": "guest", "collaborator": "true"}, False, True, REFUSED, "--allow-non-owner"),
        ({"access_tier": "guest", "collaborator": "true"}, True, True, RESTRICTED, ""),
        ({}, False, True, REFUSED, "--allow-non-owner"),
        ({}, True, True, RESTRICTED, ""),
    )

    def test_the_kind_and_unlock_for_every_tier_and_flag(self):
        for headers, non_owner, collab, kind, unlock in self.TABLE:
            p = turn_policy(headers, non_owner, collab)
            self.assertEqual((p.kind, p.unlock, p.served), (kind, unlock, kind != REFUSED), (headers, non_owner, collab))
            self.assertIs(p.capabilities, task_io.TURN_CAPABILITIES[kind])

    def test_defaults_serve_the_owner_and_an_attested_collaborator_only(self):
        self.assertEqual([turn_policy(h).kind for h in (OWNER_H, COLLAB_H, TEAM_H, GUEST_H)],
                         [OWNER, COLLABORATOR, REFUSED, REFUSED])

    def test_the_capability_table(self):
        caps = {k: (v._asdict() if v else None) for k, v in task_io.TURN_CAPABILITIES.items()}
        self.assertEqual(caps, {
            OWNER: dict(workspace="owner", writes=True, integrations=True, session="durable", framing="owner",
                        delivery_guard=False, owner_dm=True),
            COLLABORATOR: dict(workspace="owner", writes=True, integrations=True, session="durable",
                               framing="non_owner", delivery_guard=True, owner_dm=False),
            RESTRICTED: dict(workspace="isolated", writes=False, integrations=False, session="ephemeral",
                             framing="non_owner", delivery_guard=True, owner_dm=False),
            REFUSED: None,
        })

    def test_a_collaborator_gets_the_owners_capabilities_bounded_by_prompt_and_guard_only(self):
        owner, collab = task_io.OWNER_CAPABILITIES, task_io.COLLABORATOR_CAPABILITIES
        differ = {f for f in owner._fields if getattr(owner, f) != getattr(collab, f)}
        self.assertEqual(differ, {"framing", "delivery_guard", "owner_dm"})

    def test_restricted_is_read_only_and_isolated(self):
        r = task_io.RESTRICTED_CAPABILITIES
        self.assertEqual((r.workspace, r.writes, r.integrations, r.session), ("isolated", False, False, "ephemeral"))

    def test_the_older_admission_words_are_the_policy_renamed(self):
        for headers, non_owner, _collab, kind, _ in self.TABLE:
            if _collab:
                self.assertEqual(task_io.admission(headers, non_owner),
                                 {OWNER: "owner", COLLABORATOR: "collaborator", RESTRICTED: "non_owner"}.get(kind))


ADAPTERS = PKG_ROOT / "ag2_sparrow" / "adapters"
# Each enforcing module and the call that proves it takes the shared decision.
DELEGATES = {
    "codex/codex_connector.py": "turn_policy(",
    "claude_code/inbox.py": "task_io.turn_policy(",
    "openclaw/openclaw_connector.py": "turn_policy(",
    "claude_cloud_session/session_adapter.py": "self.policy(",
    "claude_routine/routine_adapter.py": "self.policy(",
}
PRIVATE_TIER = re.compile(r"""access_tier["']\)\s*(==|!=)\s*["'](owner|team|guest)|collaborator["']\)\s*==\s*["']true|"""
                          r"""\badmission\(|\bis_collaborator\(|\bis_owner\(""")
# Moves to turn_policy once its open changes land; until then it reads the older admission() words.
PENDING = {"antigravity"}


class AdapterDelegation(unittest.TestCase):
    def test_every_enforcing_adapter_calls_the_shared_decision_and_keeps_no_tier_test(self):
        for rel, call in DELEGATES.items():
            text = (ADAPTERS / rel).read_text(encoding="utf-8")
            with self.subTest(module=rel):
                self.assertIn(call, text)
                self.assertEqual([m.group(0) for m in PRIVATE_TIER.finditer(text)], [])

    def test_the_relay_loop_decides_through_turn_policy(self):
        text = (PKG_ROOT / "ag2_sparrow" / "relay_loop.py").read_text(encoding="utf-8")
        body = text[text.index("    def policy(self, headers):"):text.index("    def pending_attestation")]
        self.assertIn("turn_policy(headers, self.args.allow_non_owner, self.args.allow_collaborator)", body)
        self.assertIsNone(PRIVATE_TIER.search(body))

    def test_every_adapter_is_covered(self):
        dirs = {p.name for p in ADAPTERS.iterdir() if p.is_dir() and not p.name.startswith("_")}
        covered = {rel.split("/")[0] for rel in DELEGATES} | PENDING | {"hatch"}
        self.assertEqual(dirs - covered, set(), "a new adapter must take the shared decision")

    def test_hatch_enforces_nothing_itself(self):
        for path in (ADAPTERS / "hatch").glob("*.py"):
            with self.subTest(module=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertEqual([m.group(0) for m in PRIVATE_TIER.finditer(text) if m.group(0) != "is_collaborator("], [])

    def test_the_scan_catches_a_copy(self):
        for copy in ('if headers.get("access_tier") == "owner":', "kind = admission(headers, x)",
                     'headers.get("collaborator") == "true"', "if is_collaborator(headers):"):
            self.assertIsNotNone(PRIVATE_TIER.search(copy), copy)


if __name__ == "__main__":
    unittest.main()
