#!/usr/bin/env python3
"""
Regression guard: skills/quota-tracker/scripts/read-quota.py must catch only
ImportError when importing workspace_default, not bare Exception.

Background (sonichi/sutando#1062): the original `except Exception:` swallowed
any exception from `status_read_path`, including genuine runtime errors in
workspace_default (e.g. a broken refactor). The symptom is a misleading
"No quota-state.json found" message regardless of why the import failed.
Narrowing to `except ImportError:` lets real bugs propagate to cron logs.

Guards:
  1. Source contains `except ImportError:` (not `except Exception:`)
  2. Source does NOT contain bare `except Exception:` in the import block
  3. The `_canonical = None` fallback is still present (safety net preserved)
  4. The try block still imports `workspace_default` / `status_read_path`

Run: python3 skills/quota-tracker/tests/quota-tracker-bare-except.test.py
Exit: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent
SRC = REPO / "skills" / "quota-tracker" / "scripts" / "read-quota.py"
SOURCE = SRC.read_text(encoding="utf-8")


class TestBareExceptNarrowed(unittest.TestCase):
    def test_uses_import_error_not_bare_exception(self):
        """The except clause must be `except ImportError:` (not `except Exception:`)."""
        self.assertIn("except ImportError:", SOURCE, (
            "read-quota.py must use `except ImportError:` when importing workspace_default. "
            "See sonichi/sutando#1062."
        ))

    def test_no_bare_except_exception_in_import_block(self):
        """There must be no `except Exception:` in the workspace_default import block."""
        # Extract the try/except block around the workspace_default import
        block_match = re.search(
            r"try:\s*\n\s+from workspace_default.*?except\s+(\w+(?:\.\w+)?):",
            SOURCE,
            re.DOTALL,
        )
        self.assertIsNotNone(block_match, "Could not locate try/except block for workspace_default import")
        caught = block_match.group(1)
        self.assertEqual(caught, "ImportError", (
            f"Expected `except ImportError:` but found `except {caught}:`. "
            "Bare `except Exception:` masks real bugs — narrowing required."
        ))

    def test_canonical_none_fallback_preserved(self):
        """The `_canonical = None` fallback must still exist after the except."""
        self.assertIn("_canonical = None", SOURCE, (
            "_canonical = None fallback must be preserved in the except block."
        ))

    def test_workspace_default_import_still_present(self):
        """The `from workspace_default import status_read_path` line must still be present."""
        self.assertIn("from workspace_default import status_read_path", SOURCE, (
            "workspace_default import must remain in the try block."
        ))


def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestBareExceptNarrowed)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
