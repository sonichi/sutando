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

# workspace_default lives in <repo>/src/. The repo tree is mirrored verbatim
# into the app bundle, so following the skills/ symlink here is harmless — it
# locates a valid copy of the helper either way. resolve_workspace() itself
# resolves the workspace from $SUTANDO_WORKSPACE / ~/.sutando/workspace and
# never from __file__, so the path it returns is correct regardless.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent / "src"))
from workspace_default import resolve_workspace  # noqa: E402

EXCLUDE_FILES = {"MEMORY.md", "INDEX.md", "self_identity.md"}
FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
MAX_LINE = 200  # per CLAUDE.md: "Keep index entries to one line under ~200 chars"


def _slugify_path(p: Path) -> str:
    """Claude Code project-slug convention: every non-alphanumeric char in the
    absolute path becomes '-'.

    e.g. /Users/x/.sutando/workspace -> -Users-x--sutando-workspace
    (note the '--' where '/.' was). The old code only replaced '/', which
    left the dot in '.sutando' intact and produced a slug that did not match
    Claude Code's actual project directory.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(p))


def default_memory_dir() -> Path:
    """Resolve the memory dir.

    Order:
      1. $SUTANDO_MEMORY_DIR env var.
      2. ~/.claude/projects/<workspace-slug>/memory — slug derived from the
         WORKSPACE dir (the Claude Code session cwd), not this script's path.

    Historic bug: the fallback derived the slug from
    ``Path(__file__).resolve().parent`` walked up four levels. When ``skills/``
    is an app-bundle symlink (the normal install layout), ``.resolve()``
    follows it into ``/Applications/Sutando.app/Contents/Resources/repo`` — so
    the slug pointed at an empty memory dir, ``collect_entries`` found 0
    frontmatter files, and the clobber guard made the regen a silent no-op on
    every cross-node-sync cron pass (every 7 min). Deriving from
    ``resolve_workspace()`` — the canonical workspace contract — fixes this
    regardless of where the script file physically lives.
    """
    env = os.environ.get("SUTANDO_MEMORY_DIR")
    if env:
        return Path(env).expanduser()
    workspace = resolve_workspace(migrate=False)
    return Path.home() / ".claude" / "projects" / _slugify_path(workspace) / "memory"


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

    # Guard: refuse to clobber an existing non-trivial MEMORY.md when the
    # frontmatter scan finds 0 entries. This happens when the sync-repo has
    # not been rsynced yet (SUTANDO_SYNC_PEER unset) or when all memory
    # entries are still inline (no per-file frontmatter). Without this guard
    # the cron (every 7 min) silently overwrites accumulated inline entries
    # with a bare header. See issue #787.
    #
    # In --dry-run mode the guard emits the warning but continues so the
    # operator can still see what would be written (dry-run is read-only
    # so there is no risk of clobbering). Per qingyun-wu's review on #806.
    clobber_guard = False
    if len(entries) == 0 and target.exists():
        existing = target.read_text().strip()
        # "non-trivial" = more than just a heading or whitespace
        # (list items, paragraphs, fenced code, etc. all count)
        content_lines = [l for l in existing.splitlines() if l.strip() and not l.strip().startswith("#")]
        if content_lines:
            print(f"warning: 0 frontmatter entries but MEMORY.md has {len(content_lines)} content lines — refusing to clobber. "
                  f"Add per-file frontmatter to memory entries, or set SUTANDO_SYNC_PEER to fetch the synced memory dir.",
                  file=sys.stderr)
            clobber_guard = True

    if args.dry_run:
        print(f"would write {len(entries)} entries to {target}")
        print(body[:400])
        return 1 if clobber_guard else 0

    if clobber_guard:
        return 1

    target.write_text(body)
    print(f"wrote {len(entries)} entries to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
