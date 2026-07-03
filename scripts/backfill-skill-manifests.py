#!/usr/bin/env python3
"""backfill-skill-manifests.py — give every skill a v1 package manifest.

Phase 1 (#1902) migrated the 4 manifest-loaded skills. The other ~44 are
slash-command / SKILL.md skills with no manifest.json. This adds a MINIMAL v1
manifest to each (identity only — name/version/owner/license/stability +
description), so every skill is registrable and lint-covered.

Minimal by design: these skills aren't manifest-loaded, so no enabled/tools/
access_tier — just package identity. Stability defaults to `stable` (matching the
4 already-migrated, in-production skills); downgrade specific ones to
`experimental` in a follow-up if their contract isn't settled.

Idempotent: skips any skill that already has a manifest.json. Only touches dirs
that look like skills (have a SKILL.md). Run from the repo root:

  python3 scripts/backfill-skill-manifests.py            # write manifests
  python3 scripts/backfill-skill-manifests.py --dry-run  # list what would change
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _repo_root() -> Path:
    try:
        top = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path(__file__).resolve().parent, text=True,
            stderr=subprocess.DEVNULL).strip()
        if top:
            return Path(top)
    except Exception:  # noqa: BLE001
        pass
    return Path(__file__).resolve().parents[1]


REPO = _repo_root()
OWNER = "sonichi/sutando"


def _description(skill_dir: Path, name: str) -> str:
    """Best available summary for SKILL.md, in order: the YAML frontmatter
    `description:` field (skills author this deliberately), else the first
    body paragraph, else the H1, else a generic."""
    md = skill_dir / "SKILL.md"
    if not md.exists():
        return f"The {name} skill."
    lines = md.read_text(errors="ignore").splitlines()

    # 1. frontmatter description
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            s = lines[i].strip()
            if s == "---":
                body_start = i + 1
                break
            if s.startswith("description:"):
                v = s.split(":", 1)[1].strip().strip('"').strip("'")
                if v:
                    return v[:300]

    # 2. first body paragraph, else 3. H1
    h1 = ""
    for raw in lines[body_start:]:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            if not h1:
                h1 = line.lstrip("#").strip()
            continue
        return line[:300]
    return (h1 or f"The {name} skill")[:300]


def _manifest(name: str, desc: str) -> dict:
    return {
        "name": name,
        "version": "1.0.0",
        "owner": OWNER,
        "license": "MIT",
        "stability": "stable",
        "description": desc,
    }


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    skills_root = REPO / "skills"
    written, skipped, no_skill_md = [], [], []
    for d in sorted(skills_root.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        if (d / "manifest.json").exists():
            skipped.append(name)
            continue
        if not (d / "SKILL.md").exists():
            no_skill_md.append(name)
            continue
        manifest = _manifest(name, _description(d, name))
        if dry:
            written.append(name)
            continue
        (d / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        written.append(name)

    verb = "would write" if dry else "wrote"
    print(f"{verb} {len(written)} manifest(s); skipped {len(skipped)} "
          f"(already had one); {len(no_skill_md)} dir(s) had no SKILL.md.")
    if written:
        print("  + " + ", ".join(written))
    if no_skill_md:
        print("  (no SKILL.md, left alone): " + ", ".join(no_skill_md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
