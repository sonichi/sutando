#!/usr/bin/env python3
"""Regenerate MEMORY.md from per-file YAML frontmatter in the memory dir.

Index manifests like MEMORY.md cannot use rsync mtime-wins — one side's
newer-but-shorter listing clobbers the other's longer one (Studio + MBP both
hit this on 2026-04-17: 74 files on disk, only 19 linked). The fix is to
exclude MEMORY.md from rsync and regenerate it locally from each entry's
YAML frontmatter after every sync.

Usage:
    python3 regenerate-memory-index.py [--memory-dir PATH] [--dry-run]

Memory dir resolution order:
    1. --memory-dir CLI flag
    2. $SUTANDO_MEMORY_DIR env var
    3. ~/.claude/projects/<repo-slug>/memory/  (slug = repo-root absolute
       path with '/' → '-', matching Claude Code's convention)

Closes #712.
"""
import argparse
import os
import re
import sys
from pathlib import Path

EXCLUDE_FILES = {"MEMORY.md", "INDEX.md", "self_identity.md"}
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
MAX_LINE = 200  # per CLAUDE.md: "Keep index entries to one line under ~200 chars"


def default_memory_dir() -> Path:
    env = os.environ.get("SUTANDO_MEMORY_DIR")
    if env:
        return Path(env).expanduser()
    # Derive from this script's location: <repo>/skills/cross-node-sync/scripts/<this>
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    slug = str(repo_root.resolve()).replace("/", "-")
    return Path.home() / ".claude" / "projects" / slug / "memory"


def parse_frontmatter(text: str) -> dict:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip()
    return out


def collect_entries(memdir: Path) -> list:
    entries = []
    for p in sorted(memdir.glob("*.md")):
        if p.name in EXCLUDE_FILES:
            continue
        try:
            text = p.read_text()
        except Exception as e:
            print(f"warn: {p.name}: {e}", file=sys.stderr)
            continue
        fm = parse_frontmatter(text)
        name = fm.get("name", p.stem)
        desc = fm.get("description", "")
        entries.append((name, p.name, desc))
    return entries


def render(entries: list) -> str:
    lines = []
    for name, fname, desc in sorted(entries, key=lambda x: x[0].lower()):
        prefix = f"- [{name}]({fname}) — "
        budget = MAX_LINE - len(prefix)
        if budget < 20:
            line = prefix + desc[: max(0, budget)]
        elif len(desc) <= budget:
            line = prefix + desc
        else:
            line = prefix + desc[: budget - 3] + "..."
        lines.append(line)
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--memory-dir", type=Path, default=default_memory_dir())
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    memdir = args.memory_dir
    if not memdir.is_dir():
        # Not an error — first-run / fresh install may not have memory yet.
        print(f"memory dir not found: {memdir} (skipping regen)", file=sys.stderr)
        return 0

    entries = collect_entries(memdir)
    body = render(entries)
    target = memdir / "MEMORY.md"

    if args.dry_run:
        print(f"would write {len(entries)} entries to {target}")
        print(body[:400])
        return 0

    target.write_text(body)
    print(f"wrote {len(entries)} entries to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
