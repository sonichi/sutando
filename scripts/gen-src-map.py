#!/usr/bin/env python3
"""Generate docs/src-map.md — a one-line-per-module index of src/.

WHY THIS EXISTS
    An agent working on this repo (Claude Code or Codex, per `core.runtime`)
    has CLAUDE.md / AGENTS.md for the operating protocol, but nothing that maps
    the code. src/ is ~160 files; the only orientation is CLAUDE.md's four-entry
    "Workspace layout" and README's user-facing capability table. Finding where
    something lives means grepping, and grepping a 38k-line tree burns the
    context budget CLAUDE.md is otherwise careful to protect.

    The raw material already exists: agent-facing source modules carry a
    substantive header comment. This script indexes what is already written
    rather than inventing new prose, so the map cannot drift from the code's own
    description of itself — and a stale entry is a signal the header is stale.

DESIGN
    - PURPOSE LINE ONLY, no symbol dumps. `grep` and LSP already answer "where is
      symbol X". Neither answers "what is this file for", which is the actual gap.
      Keeping it to one line also keeps the artifact small enough to read.
    - Generated + `--check`ed in CI, mirroring scripts/agents-md-sync.sh. A map
      nobody regenerates is worse than no map, because it is confidently wrong.
    - Deterministic output (sorted, no timestamps) so --check never false-fires.

USAGE
    python3 scripts/gen-src-map.py            # regenerate docs/src-map.md
    python3 scripts/gen-src-map.py --check    # exit 1 if stale
"""

from __future__ import annotations

import re
import sys
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
OUT = REPO / "docs" / "src-map.md"

SUFFIXES = (".ts", ".py", ".sh", ".mjs", ".swift")

# Directories under src/ that are not agent-facing source.
SKIP_DIRS = {"node_modules", "__pycache__", ".git", "dist", "build"}

# Comment/docstring markers to strip from a captured header block.
_LEAD = re.compile(r"^\s*(?:/\*\*?|\*|//+|#+|\"\"\"|''')\s?")
_TRAIL = re.compile(r"\s*(?:\*/|\"\"\"|''')\s*$")
_SENTENCE = re.compile(r"^(.+?[.!?](?:[`*_)]*))(?=\s|$)")


def _clean_comment_line(raw: str) -> str:
    return _TRAIL.sub("", _LEAD.sub("", raw.strip())).strip()


def _comment_block(lines: list[str], start: int) -> tuple[list[str], int] | None:
    """Return the leading comment/docstring block and the next line index."""
    first = lines[start].lstrip()

    if first.startswith("/*"):
        block = []
        i = start
        while i < len(lines):
            block.append(_clean_comment_line(lines[i]))
            if "*/" in lines[i]:
                return block, i + 1
            i += 1
        return block, i

    delimiter = next((d for d in ('"""', "'''") if first.startswith(d)), None)
    if delimiter:
        block = []
        i = start
        while i < len(lines):
            block.append(_clean_comment_line(lines[i]))
            if i > start and delimiter in lines[i]:
                return block, i + 1
            if i == start and lines[i].count(delimiter) > 1:
                return block, i + 1
            i += 1
        return block, i

    prefix = "//" if first.startswith("//") else "#" if first.startswith("#") else None
    if prefix:
        block = []
        i = start
        while i < len(lines) and lines[i].lstrip().startswith(prefix):
            block.append(_clean_comment_line(lines[i]))
            i += 1
        return block, i

    return None


def _purpose_from_block(lines: list[str]) -> str:
    """Extract one complete purpose sentence/phrase from a comment block."""
    paragraph = []
    for line in lines:
        if not line:
            if paragraph:
                break
            continue
        paragraph.append(line)

    if not paragraph:
        return ""

    # Swift MARK headers and a following Usage block are already concise labels.
    if paragraph[0].startswith("MARK:"):
        return re.sub(r"^MARK:\s*-\s*", "", paragraph[0]).rstrip(".")
    if len(paragraph) > 1 and paragraph[1].startswith("Usage:"):
        return paragraph[0]

    # Join physical wrapping before selecting the first sentence. Returning the
    # first physical line produced fragments whenever a header wrapped naturally.
    text = " ".join(paragraph)
    sentence = _SENTENCE.match(text)
    return sentence.group(1) if sentence else text


def purpose(path: Path) -> str:
    """First complete purpose sentence/phrase from the file's header comment.

    Reads only the top of the file — the header is by definition at the top, and
    scanning whole files would make this slow for no benefit.
    """
    try:
        head = path.read_text(encoding="utf-8", errors="replace").split("\n")[:40]
    except OSError:
        return ""

    i = 0
    while i < len(head):
        line = head[i].strip()
        if not line:
            i += 1
            continue

        # Boilerplate may legally precede the module header.
        if line.startswith(("#!", "# -*-", "from __future__ import")):
            i += 1
            continue
        if path.suffix == ".swift" and line.startswith("import "):
            i += 1
            continue

        captured = _comment_block(head, i)
        if captured is None:
            return ""
        block, next_i = captured

        # A TypeScript reference directive and its explanatory comment block are
        # compiler setup, not the module purpose. Continue to the real docblock.
        if line.startswith("/// <reference"):
            i = next_i
            continue

        return _purpose_from_block(block)

    return ""


def collect() -> list[tuple[str, str]]:
    rows = []
    for p in sorted(SRC.rglob("*")):
        if not p.is_file() or p.suffix not in SUFFIXES:
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(REPO).parts):
            continue
        # Test files describe a test, not a module of the system.
        if ".test." in p.name:
            continue
        # POSIX separators, not the platform's: the artifact is diffed by CI on
        # Linux, so a Windows run must not emit backslashes it can never match.
        rows.append((p.relative_to(REPO).as_posix(), purpose(p)))
    return rows


def render(rows: list[tuple[str, str]]) -> str:
    undocumented = [r for r, d in rows if not d]
    out = [
        "<!-- GENERATED by scripts/gen-src-map.py — do not edit by hand. -->",
        "<!-- Edit the module's own header comment, then re-run the script.  -->",
        "",
        "# src/ module map",
        "",
        "One line per agent-facing source module, taken from that file's own header",
        "comment. This is a lookup, not required reading — it is deliberately NOT",
        "loaded into every session (see CLAUDE.md's note on context budget).",
        "",
        "If an entry reads wrong, the file's header comment is wrong: fix the header",
        "and re-run `python3 scripts/gen-src-map.py`.",
        "",
        # The module total rewrote one line on every module change, so two PRs
        # adding different numbers of modules conflicted on it. It is now stdout-only.
        "One entry per agent-facing module."
        + (f" {len(undocumented)} without a usable header comment." if undocumented else ""),
        "",
    ]

    # Group by directory so related modules read together.
    groups: dict[str, list[tuple[str, str]]] = {}
    for rel, desc in rows:
        groups.setdefault(PurePosixPath(rel).parent.as_posix(), []).append((rel, desc))

    for group in sorted(groups):
        out.append(f"## `{group}/`")
        out.append("")
        for rel, desc in groups[group]:
            name = PurePosixPath(rel).name
            out.append(f"- **`{name}`** — {desc}" if desc else f"- **`{name}`** — _(no header comment)_")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    # Keep the collected rows: the write path needs the count for its summary
    # line, and re-calling collect() would re-walk src/ and re-read every
    # header a second time for a number we already have.
    rows = collect()
    rendered = render(rows)
    check = "--check" in sys.argv[1:]

    if check:
        if not OUT.exists():
            print(f"gen-src-map: {OUT.relative_to(REPO)} is missing — run 'python3 scripts/gen-src-map.py'", file=sys.stderr)
            return 1
        if OUT.read_text(encoding="utf-8") == rendered:
            print("gen-src-map: docs/src-map.md is up to date")
            return 0
        print(
            "gen-src-map: docs/src-map.md is stale — run 'python3 scripts/gen-src-map.py' and commit the result",
            file=sys.stderr,
        )
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(rendered, encoding="utf-8")
    undocumented = sum(1 for _, d in rows if not d)
    tally = f"{len(rows)} modules"
    if undocumented:
        tally += f", {undocumented} without a usable header comment"
    print(f"gen-src-map: wrote {OUT.relative_to(REPO)} ({tally})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
