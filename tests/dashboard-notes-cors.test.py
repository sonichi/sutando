#!/usr/bin/env python3
"""Security regression guard for the dashboard notes CORS exception."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = (REPO / "src" / "dashboard.py").read_text()


def test_notes_cors_is_narrowly_allowlisted():
    assert '"http://localhost:8080"' in SRC
    assert '"http://127.0.0.1:8080"' in SRC
    start = SRC.index("NOTES_CORS_ORIGINS")
    end = SRC.index("def _resolve_note_path")
    assert '"*"' not in SRC[start:end]


def test_notes_routes_use_the_cors_helper():
    start = SRC.index('elif urlparse(self.path).path == "/notes":')
    notes_block = SRC[start:]
    assert notes_block.count("self._send_local_ui_cors()") == 2


def test_identity_route_uses_the_cors_helper():
    start = SRC.index('elif urlparse(self.path).path == "/stand-identity":')
    end = SRC.index('elif urlparse(self.path).path == "/json":')
    assert "self._send_local_ui_cors()" in SRC[start:end]


def main():
    tests = (
        test_notes_cors_is_narrowly_allowlisted,
        test_notes_routes_use_the_cors_helper,
        test_identity_route_uses_the_cors_helper,
    )
    for test in tests:
        test()
        print(f"  OK {test.__name__}")
    print("All dashboard notes CORS tests passed.")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)