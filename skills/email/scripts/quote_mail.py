#!/usr/bin/env python3
"""Neutralize result markers in an email note: stdin is the note, stdout the text to publish.

Anything quoted from a mail is untrusted, and a `[file: ...]`, `[channel: ...]` or `[dm-only]`
in a subject would otherwise be acted on by the delivering bridge. The grammar's own inverse,
`result_markers.neutralize_markers`, does the quoting.

Usage: python3 skills/email/scripts/quote_mail.py < note.txt
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
from result_markers import neutralize_markers  # noqa: E402


def quote(text: str) -> str:
    return neutralize_markers(text)


def main() -> int:
    sys.stdout.write(quote(sys.stdin.read()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
