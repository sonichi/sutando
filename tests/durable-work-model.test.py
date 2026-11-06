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
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

import local_task_protocol as ltp  # noqa: E402


def _stamp_pattern(key: str) -> "re.Pattern":
    """The write signatures a producer actually emits. Split out so the shapes
    can be tested against fixtures instead of only against the live tree."""
    return re.compile(
        rf"""["']{key}["']\s*[:=]\s*f?["']([a-z_]+)["']"""
        rf"""|{key}:\s*([a-z_]+)\\n"""
        rf"""|{key}:\s*([a-z_]+)`""")


def _grep_stamped_values(key: str) -> set:
    """Every literal value producers stamp for `key:` across live writer
    code, Python AND TypeScript (the TS bridges are live stampers too).

    Write signatures only — quoted-key assignment ("access_tier":
    "ambient") and header-line writes inside string literals ("access_tier:
    ambient\\n" or backtick-terminated `access_tier: owner`). Prose in
    comments, `key: Type` annotations, `.key === 'x'` comparisons, a
    third-party call's options object ({ priority: 'high' }) and a struct
    that merely carries the field (observability's Actor descriptor) match
    none of these, which is what keeps this a producer census rather than a
    word search."""
    pat = _stamp_pattern(key)
    found = set()
    roots = [REPO / "src", REPO / "skills", REPO / "packages"]
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


class TestStampPatternMatchesProducersOnly(unittest.TestCase):
    """The census must count task-header writes and nothing that merely looks
    like one. A third-party call's options object is the shape that slipped."""

    def _found(self, text, key="priority"):
        return {next(g for g in m.groups() if g)
                for m in _stamp_pattern(key).finditer(text)}

    def test_a_third_party_options_object_is_not_a_stamp(self):
        # bodhi's notifyBackground takes priority?: 'normal' | 'high'. Counting
        # it made the model look wrong when the dependency was simply obeyed.
        text = ("session.notifyBackground(\n"
                "  `A delegated task just finished. ...`,\n"
                "  { priority: 'high' });\n")
        self.assertEqual(self._found(text), set())

    def test_a_struct_that_carries_the_field_is_not_a_stamp(self):
        # src/observability/** builds Actor descriptors with access_tier, under
        # its own AccessTier vocabulary ('owner'|'team'|'public'|'unknown').
        text = ("const ACTOR = { user_id: 'core', channel: 'claude-code',\n"
                "                access_tier: 'owner' as AccessTier, tenant_id: null };\n")
        self.assertEqual(self._found(text, key="access_tier"), set())

    def test_a_header_line_write_IS_a_stamp(self):
        self.assertEqual(self._found('f"priority: urgent\\n"'), {"urgent"})

    def test_a_backtick_header_write_IS_a_stamp(self):
        self.assertEqual(self._found("`priority: low`"), {"low"})

    def test_a_quoted_key_assignment_IS_a_stamp(self):
        self.assertEqual(self._found("""{"priority": "normal"}"""), {"normal"})

    def test_the_same_holds_for_access_tier(self):
        self.assertEqual(self._found("route(msg, { access_tier: 'owner' })",
                                     key="access_tier"), set())
        self.assertEqual(self._found('f"access_tier: team\\n"',
                                     key="access_tier"), {"team"})


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
