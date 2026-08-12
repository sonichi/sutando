#!/usr/bin/env python3
"""Regression guard: morning-briefing's get_pending_questions() honors **Status:**.

Pending questions are kept above a `# Resolved` divider even after they're
answered — the convention is to mark them resolved via a `**Status:** resolved`
body field rather than deleting or retitling them (some keep the original
`[OPEN ...]` title for audit continuity). Before the fix, get_pending_questions()
only skipped title-level RESOLVED, so a section whose body said
`**Status:** resolved` was still counted + spoken as pending.

After the fix, a section is excluded when its body carries a `**Status:**` field
whose value is anything other than `unanswered`/`waiting`. This mirrors
check-pending-questions.py so the briefing and the checker agree on the count.
"""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
MB_PATH = REPO / "src" / "morning-briefing.py"


def _load_mb():
    spec = importlib.util.spec_from_file_location("morning_briefing", MB_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBriefingPendingStatus(unittest.TestCase):
    def setUp(self):
        if not MB_PATH.exists():
            self.skipTest("morning-briefing.py not found")
        self.mb = _load_mb()

    def _run(self, content: str, tmp_path: Path) -> list[str]:
        pq = tmp_path / "pending-questions.md"
        pq.write_text(content)
        # get_pending_questions resolves its file via personal_path(...); point
        # that at our fixture so the test is hermetic (no real workspace read).
        with patch.object(self.mb, "personal_path", return_value=pq):
            return self.mb.get_pending_questions()

    def test_status_resolved_body_excluded_even_when_title_open(self):
        """A section titled [OPEN ...] but body **Status:** resolved is NOT pending."""
        import tempfile

        content = (
            "# Pending questions\n\n"
            "## [OPEN] Genuinely waiting on you\n"
            "Body with no status field — this one counts.\n\n"
            "## [OPEN] Already answered but title not updated\n"
            "**Status:** resolved — you picked option B last week.\n\n"
            "## [OPEN] Marked waiting still counts\n"
            "**Status:** waiting on upstream review.\n\n"
            "# Resolved\n"
            "## [OPEN] Below the divider, never counts\n"
            "Body.\n"
        )
        with tempfile.TemporaryDirectory() as d:
            got = self._run(content, Path(d))

        # The resolved-in-body section is dropped; the genuinely-open one and the
        # explicitly-"waiting" one remain; the below-divider one is cut.
        joined = " | ".join(got)
        self.assertIn("Genuinely waiting on you", joined)
        self.assertIn("Marked waiting still counts", joined)
        self.assertNotIn("Already answered", joined)
        self.assertNotIn("Below the divider", joined)
        self.assertEqual(len(got), 2, f"expected 2 pending, got {got}")

    def test_status_open_counts_as_pending(self):
        """`**Status:** open` is the natural word writers reach for; it must count
        as pending (not fall through to the resolved-skip). Mirrors
        check-pending-questions.py so the briefing and the checker agree."""
        import tempfile

        content = (
            "# Pending questions\n\n"
            "## [OPEN] Plain open counts\n"
            "**Status:** open\n\n"
            "## [OPEN] Open with case + trailing prose counts\n"
            "**Status:** Open — waiting on the owner's call.\n\n"
            "## [OPEN] Resolved control still excluded\n"
            "**Status:** resolved — done.\n"
        )
        with tempfile.TemporaryDirectory() as d:
            got = self._run(content, Path(d))

        joined = " | ".join(got)
        self.assertIn("Plain open counts", joined)
        self.assertIn("Open with case + trailing prose counts", joined)
        self.assertNotIn("Resolved control", joined)
        self.assertEqual(len(got), 2, f"expected 2 pending (both open), got {got}")


if __name__ == "__main__":
    unittest.main()
