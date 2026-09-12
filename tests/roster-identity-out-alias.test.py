"""`--out` must not overwrite ANY supplied input, by name or by hardlink.

The shipped guard checked only `--roster`, so `--triage-config X --out X`
returned 0, replaced X's `people` map with the v2 roster, and printed
"input untouched" — the message a caller would have trusted.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = (pathlib.Path(__file__).resolve().parents[1]
          / "skills/collaboration-intelligence/scripts/migrate_roster_identity.py")

BOT = "111111111111111111"
# A shape the migration can actually RESOLVE: an unresolvable one exits 5 and
# the positive control then fails for a reason that has nothing to do with aliasing.
ROSTER = {"rui": {"github": "john-the-dev", "stand_status": f"stand id {BOT}"}}
TRIAGE = {"people": {"bob": {"discord": "333333333333333333"}}}


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                          capture_output=True, text=True)


class OutAlias(unittest.TestCase):
    def setUp(self):
        self.d = pathlib.Path(tempfile.mkdtemp())
        self.roster = self.d / "roster.json"
        self.triage = self.d / "triage.json"
        self.roster.write_text(json.dumps(ROSTER))
        self.triage.write_text(json.dumps(TRIAGE))

    def test_out_equal_to_the_roster_is_refused(self):
        r = run("--roster", self.roster, "--out", self.roster)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(json.loads(self.roster.read_text()), ROSTER)

    def test_out_equal_to_the_TRIAGE_config_is_refused(self):
        before = self.triage.read_text()
        r = run("--roster", self.roster, "--triage-config", self.triage,
                "--out", self.triage)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.triage.read_text(), before,
                         "the auxiliary input was rewritten")

    def test_a_HARDLINK_to_an_input_is_refused(self):
        """A different name, the same inode: resolve() alone cannot see it."""
        link = self.d / "alias.json"
        os.link(self.triage, link)
        before = self.triage.read_text()
        r = run("--roster", self.roster, "--triage-config", self.triage, "--out", link)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.triage.read_text(), before)

    def test_a_NORMAL_sibling_output_still_succeeds(self):
        """The control: a guard that refuses everything is not a guard."""
        out = self.d / "roster.v2.json"
        r = run("--roster", self.roster, "--triage-config", self.triage, "--out", out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(out.exists())
        self.assertEqual(json.loads(self.triage.read_text()), TRIAGE)


class ReMigrationKeepsARefusal(unittest.TestCase):
    """A re-migration must not turn a refusal into an authorization.

    The sidecar is fed back as input, which is what "re-migration" means here.
    Consumption asked `path_referent`, which reads the HEAD segment only, so a
    verdict derived from a NESTED segment failed the equality and was dropped:
    one verdict survived, the disagreement vanished, and the id resolved to a
    principal nobody agreed on. Measured rc 5 -> 0 -> 0 before the fix.
    """

    H = "1400000000000000001"
    S = "1500000000000000001"

    def three_passes(self, roster_doc):
        import json
        import subprocess
        import tempfile
        td = pathlib.Path(tempfile.mkdtemp())
        cur = td / "in0.json"
        cur.write_text(json.dumps(roster_doc, indent=1))
        script = (pathlib.Path(__file__).resolve().parent.parent
                  / "skills" / "collaboration-intelligence" / "scripts"
                  / "migrate_roster_identity.py")
        codes = []
        for n in range(1, 4):
            out = td / f"sidecar{n}.json"
            r = subprocess.run([sys.executable, str(script), "--roster", str(cur),
                                "--out", str(out)], capture_output=True, text=True)
            codes.append(r.returncode)
            if not out.is_file():
                break
            cur = out
        return codes

    def test_a_nested_both_referent_path_under_a_STAND_root_stays_refused(self):
        codes = self.three_passes({"reviewer": {
            "stand_status": self.S,
            "other_stand_discord_ids": [
                {"human_profile": {"secondary_agent": {"id": self.H}}}],
        }})
        self.assertEqual(codes, [5, 5, 5],
                         f"re-migration resolved a real disagreement: {codes}")

    def test_a_nested_both_referent_path_under_a_HUMAN_root_stays_refused(self):
        codes = self.three_passes({"reviewer": {
            "human_discord_id": {"secondary_agent": {"id": self.H}},
            "stand_status": self.S,
        }})
        self.assertEqual(codes, [5, 5, 5],
                         f"re-migration resolved a real disagreement: {codes}")

    def test_CONTROL_a_single_referent_slot_still_migrates_cleanly(self):
        """Without this the fix could pass by refusing everything forever."""
        self.assertEqual(
            self.three_passes({"reviewer": {"other_stand_discord_ids": [self.S]}}),
            [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
