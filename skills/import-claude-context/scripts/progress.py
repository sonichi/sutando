#!/usr/bin/env python3
"""Recount the import's progress from disk and rewrite status.json — no model.

The import coordinator (SKILL.md steps 5–7) runs this after every batch of
session summaries, after the roll-ups and after the entities pass, so the
desktop's progress reads what is on disk rather than whatever the coordinator
last remembered to write: on the live VM run of 2026-09-10 status.json still
said `summarized 0` (updated 14:40:50) while 38 of 43 summaries existed.

Everything comes from readdir + the JSON side files; no summary, dump or
transcript is opened. Counts:
  sessions     sessions listed in index.json (every project, non-sidechain)
               minus the ones extract.py marked `skipped_empty` in state.json
               (no dump, nothing to summarise)
  skipped_empty  that subtrahend
  summarized   merged summaries/<slug>/<uuid>.json present — partials
               `<uuid>.<n>.json` are not counted
  projects     projects with at least one session in index.json
  rolled_up    projects/<slug>.json present
  entities     entities.json present
  staged       staged/manifest.json present (a review set is pending)
Phase: `staged` while a review set is pending; `rolling-up` once every
session is summarised (roll-ups, entities and staging still to come); else
`summarizing`.

Flags: --data-dir (alias --out-dir), --workspace, --json. Output is counts
only — never a title, a slug or a path.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402
from _common import ENTITIES_FILE, INDEX_FILE, PROJECTS_DIR, SUMMARIES_DIR, write_status  # noqa: E402

STAGED_MANIFEST = Path("staged") / "manifest.json"


def count_summaries(data_dir: Path) -> int:
    base = data_dir / SUMMARIES_DIR
    if not base.is_dir():
        return 0
    return sum(1 for slug_dir in base.iterdir() if slug_dir.is_dir()
               for f in slug_dir.glob("*.json") if f.name.count(".") == 1)


def count_rollups(data_dir: Path) -> int:
    base = data_dir / PROJECTS_DIR
    return sum(1 for _ in base.glob("*.json")) if base.is_dir() else 0


def progress(data_dir) -> dict:
    data_dir = Path(data_dir)
    index_doc = _common.load_json(data_dir / INDEX_FILE, None)
    if not isinstance(index_doc, dict):
        raise SystemExit("import-claude-context: no index.json under the data dir yet — run index.py first")
    state = _common.load_state(data_dir)
    skipped = sum(1 for r in state["sessions"].values() if isinstance(r, dict) and r.get("skipped_empty"))
    projects = index_doc.get("projects") or {}
    indexed = sum(len(p.get("sessions") or []) for p in projects.values() if isinstance(p, dict))
    counts = {
        "sessions": max(0, indexed - skipped),
        "skipped_empty": skipped,
        "summarized": count_summaries(data_dir),
        "projects": sum(1 for p in projects.values() if isinstance(p, dict) and p.get("sessions")),
        "rolled_up": count_rollups(data_dir),
        "entities": (data_dir / ENTITIES_FILE).is_file(),
        "staged": (data_dir / STAGED_MANIFEST).is_file(),
    }
    if counts["staged"]:
        phase = "staged"
    elif counts["sessions"] and counts["summarized"] >= counts["sessions"]:
        phase = "rolling-up"
    else:
        phase = "summarizing"
    write_status(data_dir, phase, **counts)
    return {"phase": phase, **counts}


def progress_line(r: dict) -> str:
    return (f"{r['phase']}: {r['summarized']}/{r['sessions']} sessions summarised "
            f"({r['skipped_empty']} empty skipped), {r['rolled_up']}/{r['projects']} projects rolled up, "
            f"entities {'yes' if r['entities'] else 'no'}, staged {'yes' if r['staged'] else 'no'}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", "--out-dir", default=None, help="default <workspace>/data/claude-import")
    ap.add_argument("--workspace", default=None, help="workspace root (default: sutando-config.sh workspace)")
    ap.add_argument("--json", action="store_true", help="print the counts as JSON (counts only)")
    a = ap.parse_args(argv)
    r = progress(_common.data_dir(a.workspace, a.data_dir))
    print(json.dumps(r, sort_keys=True) if a.json else progress_line(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
