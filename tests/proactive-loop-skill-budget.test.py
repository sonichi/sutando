#!/usr/bin/env python3
"""The proactive-loop skill loads on every pass: a byte cap, no date stamps, and no command
named by the rationale doc may go missing from it."""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SKILL = REPO / "skills" / "proactive-loop" / "SKILL.md"
RATIONALE = REPO / "docs" / "proactive-loop-rationale.md"
CAP = 12 * 1024
CMD = re.compile(r"(skills/[a-z-]+/scripts/[A-Za-z0-9_.-]+|scripts/[A-Za-z0-9_.-]+\.(?:py|sh)"
                 r"|src/[A-Za-z0-9_.-]+\.(?:py|sh|ts)|\$CLAUDE_CONFIG_DIR/skills/[A-Za-z0-9_./-]+)")


class SkillBudget(unittest.TestCase):
    def test_under_the_byte_cap(self):
        size = SKILL.stat().st_size
        self.assertLessEqual(size, CAP, f"SKILL.md is {size} B; the cap is {CAP} B — move prose to the rationale doc")

    def test_no_date_stamps_in_the_skill(self):
        stamps = re.findall(r"20\d\d-\d\d-\d\d", SKILL.read_text())
        self.assertEqual(stamps, [], f"incident dates belong in docs/proactive-loop-rationale.md: {stamps[:5]}")

    def test_rationale_doc_exists_and_is_linked(self):
        self.assertTrue(RATIONALE.exists())
        self.assertIn("docs/proactive-loop-rationale.md", SKILL.read_text())

    def test_every_command_path_in_the_rationale_is_still_in_the_skill(self):
        # The extraction may drop lessons, never commands.
        skill = SKILL.read_text()
        missing = sorted({m.group(0) for m in CMD.finditer(RATIONALE.read_text())} - {m.group(0) for m in CMD.finditer(skill)})
        self.assertEqual(missing, [], f"commands named in the rationale but absent from the skill: {missing}")

    def test_the_cap_can_fail(self):
        # Control: the assertion is live, not vacuous.
        self.assertGreater(SKILL.stat().st_size, 1024)
        self.assertLess(CAP, 70_000)

    def test_every_gate_is_chained_to_its_consumer(self):
        """A gate bound by prose is a gate you can run one action late.

        Per BULLET, not per line: the script and its exit codes sit on separate
        lines, so a per-line filter matches neither and passes vacuously.
        """
        text = SKILL.read_text()
        NOT_GATING = {
            "tool-suites-check.py": "reports suite health; withholds no action",
            "codex-quota-gate.py": "Codex-only tier read, informational",
        }
        bullets = re.split(r"\n(?=\d+(?:\.\d+)?\. )", text)
        checked = []
        for b in bullets:
            if "cannot answer" not in b:
                continue
            for script in set(re.findall(r"scripts/([a-z-]+\.py)", b)):
                if script in NOT_GATING:
                    continue
                checked.append(script)
                with self.subTest(script=script):
                    self.assertIn("&&", b,
                                  f"{script} declares a refusal but is not bound to its consumer")
        # Non-vacuity: a loop that examined nothing passes silently, which is the
        # failure this arm exists to prevent, turned on the arm itself.
        self.assertGreaterEqual(len(checked), 4,
                                f"only examined {checked} — the enumerator matched too little")

    def test_the_dedup_gate_is_staged_under_its_final_name(self):
        """`check-dedup-targets` reads the source task id from the BASENAME.

        Staging under a generic temp name leaves it no sender/room metadata, so
        it skips both comparisons and returns clean — a gate that publishes what
        the unstaged invocation refused.
        """
        text = SKILL.read_text()
        bullet = next(b for b in re.split(r"\n(?=\d+(?:\.\d+)?\. )", text)
                      if "check-dedup-targets.py" in b)
        self.assertNotRegex(bullet, r'check-dedup-targets\.py "\$tmp"',
                            "staged under a generic temp name: the checker loses the task identity")
        self.assertIn("dedup-staging/<file>", bullet,
                      "the staged path must carry the FINAL basename")


if __name__ == "__main__":
    unittest.main(verbosity=1)
