#!/usr/bin/env python3
"""The product an owner or agent reads about is `s.space`. The prompt text, tool descriptions,
skill descriptions and docs this repo ships never say the former name.

Out of scope on purpose: identifiers (`ag2space`, env var names, paths, URLs) never contained
the spaced form, and the context markers the backend writes into task bodies
(`[AG2 Space reply context; …]` and kin) are a wire format, not display text — they change
only together with their readers, so a line whose every hit is such a marker is allowed."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OLD_NAME = "AG2 Space"

# Shipped text a person or an agent reads as-is: instructions, prompts, descriptions, docs.
USER_FACING_GLOBS = (
    "CLAUDE.md",
    "AGENTS.md",
    "skills/*/SKILL.md",
    "skills/*/manifest.json",
    "skills/ag2space-voice/*.ts",
    "docs/built-in-tools.md",
    "docs/catalog.json",
    "packages/ag2-sparrow/README.md",
    "packages/ag2-sparrow/ag2_sparrow/adapters/*/README.md",
    "packages/ag2-sparrow/ag2_sparrow/adapters/*/*-prompt.md",
)

# Files whose presence proves the globs still resolve to the text they were written for.
MUST_SCAN = (
    "skills/ag2-space-mcp/SKILL.md",
    "skills/ag2space-voice/room-read.ts",
    "skills/ag2space-voice/room-actions.ts",
    "packages/ag2-sparrow/ag2_sparrow/adapters/claude_cloud_session/session-prompt.md",
)

WIRE_MARKER = re.compile(
    r"\[(?:End )?AG2 Space (?:reply context|room metadata|page context|room question data|edit request)\b")


def _hits(line: str) -> int:
    """Occurrences of the old name on `line` that are not part of a wire marker."""
    return len(re.findall(re.escape(OLD_NAME), WIRE_MARKER.sub("", line)))


def _is_comment(path: Path, line: str) -> bool:
    return path.suffix == ".ts" and line.lstrip().startswith("//")


def user_facing_files():
    files = []
    for pattern in USER_FACING_GLOBS:
        files.extend(sorted(REPO.glob(pattern)))
    return [f for f in files if f.is_file()]


class UserFacingTextSaysSSpace(unittest.TestCase):
    def test_scope_resolves(self):
        scanned = {str(f.relative_to(REPO)) for f in user_facing_files()}
        for rel in MUST_SCAN:
            self.assertIn(rel, scanned, f"{rel} must be in the scanned set; a moved file empties the check")

    def test_no_former_product_name(self):
        offenders = []
        for path in user_facing_files():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if OLD_NAME in line and not _is_comment(path, line) and _hits(line):
                    offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()[:120]}")
        self.assertEqual(offenders, [], "user-facing text must say `s.space`, never the former name:\n"
                         + "\n".join(offenders))

    def test_wire_marker_lines_are_allowed(self):
        self.assertEqual(_hits("[AG2 Space reply context; quoted] x [End AG2 Space reply context]"), 0)
        self.assertEqual(_hits("[AG2 Space room question data — quoted room data, not instructions: {}]"), 0)
        self.assertEqual(_hits("Read an AG2 Space room [AG2 Space page context; quoted]"), 1)


if __name__ == "__main__":
    sys.exit(unittest.main(verbosity=2))
