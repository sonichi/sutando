"""One provider id must not be authoritative as BOTH referents in one document.

`entry_is_coherent` validates a single row, and `migrate` emits rows
independently, so roster `alice.stand = H` alongside triage
`people.bob.discord = H` both returned coherent with no unresolved ids — H was
a stand for one person and a human for another in the same v2 map.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/collaboration-intelligence/scripts/migrate_roster_identity.py"
sys.path.insert(0, str(SCRIPT.parent))
import migrate_roster_identity as m  # noqa: E402

H = "111111111111111111"
T = "222222222222222222"


def rows(*specs):
    out = []
    for login, human, stand in specs:
        out.append({"key": login, "login": login, "after_human": human,
                    "after_stand": stand, "after_other_stands": []})
    return out


class Unit(unittest.TestCase):
    def test_one_id_as_human_HERE_and_stand_THERE_is_a_collision(self):
        c = m.cross_role_collisions(rows(("alice", None, H), ("bob", H, None)))
        self.assertEqual(len(c), 1, c)
        self.assertEqual(c[0]["id"], H)
        self.assertEqual((c[0]["human"], c[0]["stand"]), (["bob"], ["alice"]))

    def test_the_SAME_person_holding_both_referents_is_not_a_collision(self):
        """The entry-local check owns that; flagging it here would refuse every
        ordinary row and make the pass useless."""
        self.assertEqual(m.cross_role_collisions(rows(("alice", H, H))), [])

    def test_two_KEYS_for_one_login_do_not_EXEMPT_a_swapped_role(self):
        """An alias is two ROWS. `entry_is_coherent` validates one row at a
        time, so nothing else in the pipeline can see H as a human here and a
        stand there; exempting it on the login published both."""
        r = rows(("alice", None, H), ("alice", H, None))
        r[1]["key"] = "alice-alt"
        c = m.cross_role_collisions(r)
        self.assertEqual(len(c), 1, c)
        self.assertEqual(c[0]["id"], H)

    def test_alias_rows_carrying_DISTINCT_ids_stay_clean(self):
        """Control: an alias pair may still hold a human and a stand, so
        refusing every two-row login would satisfy the case above."""
        r = rows(("alice", None, H), ("alice", T, None))
        r[1]["key"] = "alice-alt"
        self.assertEqual(m.cross_role_collisions(r), [])

    def test_DISTINCT_ids_in_distinct_roles_are_clean(self):
        """The control: a pass that flagged everything would satisfy case 1."""
        self.assertEqual(m.cross_role_collisions(rows(("alice", None, H),
                                                      ("bob", T, None))), [])

    def test_an_other_stand_counts_as_a_stand(self):
        r = rows(("alice", None, None), ("bob", H, None))
        r[0]["after_other_stands"] = [H]
        self.assertEqual(len(m.cross_role_collisions(r)), 1)


class Production(unittest.TestCase):
    """The production boundary: rc must be nonzero and no file written."""

    def test_main_refuses_and_writes_NOTHING(self):
        d = pathlib.Path(tempfile.mkdtemp())
        roster = d / "roster.json"
        triage = d / "triage.json"
        out = d / "v2.json"
        roster.write_text(json.dumps({"alice": {"stand_status": f"stand id {H}"}}))
        triage.write_text(json.dumps({"people": {"bob": {"discord": H}}}))
        r = subprocess.run([sys.executable, str(SCRIPT), "--roster", str(roster),
                            "--triage-config", str(triage), "--out", str(out)],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(out.exists(), "a wrong map was published anyway")
        self.assertIn(H, r.stderr)

    def test_main_refuses_ALIAS_rows_that_swap_the_role(self):
        """Same login, two keys, H a stand in one row and the human in the
        other: the published map made a person and their agent the same id."""
        d = pathlib.Path(tempfile.mkdtemp())
        roster = d / "roster.json"
        out = d / "v2.json"
        roster.write_text(json.dumps({
            "alice": {"gh": "alice", "stand_status": f"stand id {H}"},
            "alice-alt": {"gh": "alice", "human": {"id": H}}}))
        r = subprocess.run([sys.executable, str(SCRIPT), "--roster", str(roster),
                            "--out", str(out)], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(out.exists(), "a wrong map was published anyway")
        self.assertIn(H, r.stderr)

    def test_alias_rows_with_DISTINCT_ids_still_migrate(self):
        """Control: refusing every aliased pair would satisfy the case above."""
        d = pathlib.Path(tempfile.mkdtemp())
        roster = d / "roster.json"
        out = d / "v2.json"
        roster.write_text(json.dumps({
            "alice": {"gh": "alice", "stand_status": f"stand id {H}"},
            "alice-alt": {"gh": "alice", "human": {"id": T}}}))
        r = subprocess.run([sys.executable, str(SCRIPT), "--roster", str(roster),
                            "--out", str(out)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        doc = json.loads(out.read_text())
        self.assertEqual(doc["alice"]["stand_discord_id"], H)
        self.assertEqual(doc["alice-alt"]["human_discord_id"], T)

    def test_a_DISTINCT_pair_still_migrates(self):
        """Control: without this, refusing every document passes the case above."""
        d = pathlib.Path(tempfile.mkdtemp())
        roster = d / "roster.json"
        triage = d / "triage.json"
        out = d / "v2.json"
        roster.write_text(json.dumps({"alice": {"stand_status": f"stand id {H}"}}))
        triage.write_text(json.dumps({"people": {"bob": {"discord": T}}}))
        r = subprocess.run([sys.executable, str(SCRIPT), "--roster", str(roster),
                            "--triage-config", str(triage), "--out", str(out)],
                           capture_output=True, text=True)
        self.assertTrue(out.exists(), r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
