#!/usr/bin/env python3
"""Durable Work Model contract: the vocabulary in local_task_protocol must
cover what live producers actually stamp, and the named lifecycle must match
the directory layout the helpers implement.

The failure this exists to make self-detecting: a producer ships a new tier /
priority (the 2026-08 case: `ambient`, stamped by the events consumer) and the
model's vocabulary silently lags — validators written against the constants
would then misclassify real traffic. Sibling in spirit to
ci-covers-every-python-test.test.py: contract drift should fail a test, not
wait to be noticed.
"""
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import local_task_protocol as ltp  # noqa: E402


def _grep_stamped_values(key: str, roots=None) -> set:
    """Every literal value producers stamp for `key:` across live writer
    code, Python AND TypeScript (the TS bridges are live stampers too).

    Write signatures only — quoted-key assignment ("access_tier":
    "ambient") and header-line writes inside string literals ("access_tier:
    ambient\\n" or backtick-terminated `access_tier: owner`). Each carries a
    syntactic marker that a task RECORD is being written. Prose in comments,
    `key: Type` annotations, and `.key === 'x'` comparisons match none of
    these, which is what keeps this a producer census rather than a word
    search.

    A bare object-literal key (`priority: 'high'`) is deliberately NOT a
    signature: it matches any option bag in any subsystem, so it cannot tell
    a task stamp from an unrelated argument that reuses the word."""
    pat = re.compile(
        rf"""["']{key}["']\s*[:=]\s*f?["']([a-z_]+)["']"""
        rf"""|{key}:\s*([a-z_]+)\\n"""
        rf"""|{key}:\s*([a-z_]+)`""")
    found = set()
    roots = ([REPO / "src", REPO / "skills", REPO / "packages"]
             if roots is None else [Path(r) for r in roots])
    for root in roots:
        for suffix in ("*.py", "*.ts", "*.tsx"):
            for f in root.rglob(suffix):
                if "node_modules" in str(f) or "/tests/" in str(f):
                    continue
                try:
                    text = f.read_text(errors="ignore")
                except OSError:
                    continue
                for m in pat.finditer(text):
                    found.add(next(g for g in m.groups() if g))
    return found - {None}


class TestCensusSignatures(unittest.TestCase):
    """What counts as a producer stamp, driven by synthetic files."""

    def _scan(self, name, text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        (Path(d) / name).write_text(text)
        return _grep_stamped_values("priority", roots=[d])

    def test_record_signatures_are_counted(self):
        # Positive control: a narrowed pattern that matched nothing would let
        # every census below pass by being empty rather than by being clean.
        self.assertEqual(self._scan("w.py", '"priority": "urgent"'), {"urgent"})
        self.assertEqual(self._scan("w.py", 'f"priority: normal\\n"'), {"normal"})
        self.assertEqual(self._scan("w.ts", '`priority: low`'), {"low"})

    def test_option_bag_key_is_not_a_task_stamp(self):
        # notifyBackground(msg, { priority: 'high' }) is a voice-session
        # option; counting it made the model answer for a plane it does not own.
        self.assertEqual(
            self._scan("v.ts",
                       "session.notifyBackground(msg, { priority: 'high' });"),
            set())


class TestVocabularyCoversLiveProducers(unittest.TestCase):
    def test_every_stamped_access_tier_is_in_the_model(self):
        stamped = _grep_stamped_values("access_tier")
        unknown = stamped - set(ltp.ACCESS_TIERS)
        self.assertEqual(
            unknown, set(),
            f"producers stamp access_tier values the model does not name: {unknown} "
            "— add them to ACCESS_TIERS (and update CLAUDE.md) or fix the producer")

    def test_ambient_is_a_named_tier(self):
        # The 2026-08 drift this suite was born from: the events consumer
        # stamps ambient; the model must know it.
        self.assertIn("ambient", ltp.ACCESS_TIERS)

    def test_every_stamped_priority_is_in_the_model(self):
        stamped = _grep_stamped_values("priority")
        # priority: also matches unrelated uses; restrict to the known plane
        unknown = (stamped & {"urgent", "normal", "low", "high", "critical"}) - set(ltp.PRIORITIES)
        self.assertEqual(unknown, set(),
                         f"producers stamp priorities the model does not name: {unknown}")


class TestLifecycleMatchesLayout(unittest.TestCase):
    def test_states_are_named(self):
        self.assertEqual(ltp.LIFECYCLE_STATES, ("pending", "result_written", "archived"))

    def test_archive_layout_helpers_agree_with_the_archived_state(self):
        # The archived state's month-partitioned materialization is what
        # archive_month_dir/find_archived_task implement; a rename there must
        # show up here.
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            d = ltp.archive_month_dir(base, "2026-08-07T03:00:00Z")
            self.assertEqual(d, base / "archive" / "2026-08")

    def test_result_identity_is_task_id_keyed(self):
        # A result answers exactly one task id: the id grammar must be shared.
        self.assertTrue(ltp.valid_task_id("task-chat-1786000000"))
        self.assertFalse(ltp.valid_task_id("../escape"))
        self.assertTrue(ltp.valid_archive_lookup_id("task-chat-1786000000"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
