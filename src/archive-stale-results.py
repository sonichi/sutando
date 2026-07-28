#!/usr/bin/env python3
"""Archive stale `results/*.txt` files to `results/archive-YYYY-MM-DD/`.

Run once at system startup (before any service begins iterating `results/`)
so that task-bridge, discord-bridge, and the DM-fallback path never have to
reason about long-dead files they weren't around to consume.

Retention policy: by default, any `.txt` directly under `results/` whose
mtime is older than $RETENTION_HOURS (default 24) gets moved under a
date-stamped archive subdirectory. Files inside existing `archive-*`
subdirectories are never touched.

Usage:
    python3 src/archive-stale-results.py
    RETENTION_HOURS=48 python3 src/archive-stale-results.py      # looser window
    DRY_RUN=1 python3 src/archive-stale-results.py               # print, don't move

Intended caller: `src/startup.sh` runs this before launching services.

Why this exists: on 2026-04-15 the DM fallback wiring iterated `results/`
on voice-agent restart, found 142 stale files accumulated since the prior
day, and fired one DM per file. The flood was stopped by a manual archive
sweep of the same directory. This script automates that sweep and runs it
before services can see the backlog. Full post-mortem:
`notes/post-mortem-dm-flood-2026-04-15.md`.
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402

# results/ is per-user runtime state — lives under the resolved workspace
# (post-v0.8 default `<repo>/workspace/`; configurable via
# `sutando.config.local.json`), not the repo checkout. Pre-#762 this
# resolved to <repo>/results/ which doesn't exist post-migration; the
# archiver silently no-op'd because `if not RESULTS.is_dir()` short-circuits
# the whole sweep. The DM-flood prevention this script was built for was
# defeated until this fix.
WORKSPACE = resolve_workspace()
RESULTS = WORKSPACE / "results"

RETENTION_HOURS = int(os.environ.get("RETENTION_HOURS", "24"))
# Case-insensitive compare — without `.lower()`, `DRY_RUN=No` or `DRY_RUN=FALSE`
# would silently evaluate truthy (dry-run mode) because "No"/"FALSE" aren't in
# the lowercase reject list. Found in cold-review of #354.
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() not in ("", "0", "false", "no")


def main() -> int:
    if not RESULTS.is_dir():
        print("  [retention] results/ missing — nothing to do")
        return 0

    cutoff = time.time() - RETENTION_HOURS * 3600
    archive_name = datetime.now().strftime("archive-%Y-%m-%d")
    archive_dir = RESULTS / archive_name

    moved = 0
    errors = 0
    for f in RESULTS.iterdir():
        if not f.is_file():
            continue
        if f.suffix != ".txt":
            continue
        try:
            st = f.stat()
        except FileNotFoundError:
            continue
        if st.st_mtime >= cutoff:
            continue
        # NEVER archive a NOT-YET-FLUSHED .txt on its mtime. A results file can be
        # created by a producer that then pauses before its first real write — a
        # proactive nudge held open across a slow turn is the live case. Such a
        # file keeps its creation mtime while the descriptor stays open, so an
        # mtime horizon moves the inode out from under that fd and the producer's
        # later flush lands in the archived copy: silent loss of an owner-facing
        # message. This is the exact unsound signal the proactive drain stopped
        # trusting (sonichi/sutando#2324); the archiver is the OTHER mtime-keyed
        # mover of these files, so it must make the SAME exclusion or the drain's
        # guarantee does not hold end-to-end.
        #
        # Use the drain's EXACT predicate — content that is empty after strip(),
        # not merely 0 bytes. A producer that writes a newline or a header and then
        # pauses leaves a non-zero-size but strip-empty file; a size-only check
        # (st_size == 0) would move THAT, and the later flush would still land in
        # the archived inode. Matching body.strip()-empty on both sides closes that
        # whitespace gap so the two movers agree on "not flushed" (air, #2360
        # follow-up). An unreadable/undecodable file is likewise NOT moved — a file
        # we cannot read is exactly one that may be mid-write. The flood this
        # script prevents is caused by CONTENTFUL stale files (one DM per file), so
        # skipping empties does not weaken it; genuinely-orphaned empty remnants
        # are surfaced by scripts/results-health.sh for deliberate cleanup, never
        # moved on age alone.
        try:
            if not f.read_text(encoding="utf-8").strip():
                continue
        except (OSError, UnicodeDecodeError):
            continue
        if DRY_RUN:
            print(f"  [retention] would archive {f.name}")
            moved += 1
            continue
        if not archive_dir.exists():
            archive_dir.mkdir(parents=True, exist_ok=True)
        try:
            f.rename(archive_dir / f.name)
            moved += 1
        except Exception as e:
            print(f"  [retention] failed to archive {f.name}: {e}", file=sys.stderr)
            errors += 1

    if DRY_RUN:
        label = "would archive"
    else:
        label = "archived"
    if moved or errors:
        print(
            f"  [retention] {label} {moved} stale file(s) (>{RETENTION_HOURS}h)"
            + (f", {errors} error(s)" if errors else "")
            + (f" to {archive_dir.name}/" if moved and not DRY_RUN else "")
        )
    else:
        print(f"  [retention] no stale files to archive (>{RETENTION_HOURS}h cutoff)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
