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
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import resolve_workspace  # noqa: E402
from sutando_config import config_get  # noqa: E402

# results/ is per-user runtime state — lives under the resolved workspace
# (post-v0.8 default `<repo>/workspace/`; configurable via
# `sutando.config.local.json`), not the repo checkout. Pre-#762 this
# resolved to <repo>/results/ which doesn't exist post-migration; the
# archiver silently no-op'd because `if not RESULTS.is_dir()` short-circuits
# the whole sweep. The DM-flood prevention this script was built for was
# defeated until this fix.
WORKSPACE = resolve_workspace()
RESULTS = WORKSPACE / "results"

RETENTION_HOURS = int(config_get("RETENTION_HOURS", "24"))
# Case-insensitive compare — without `.lower()`, `DRY_RUN=No` or `DRY_RUN=FALSE`
# would silently evaluate truthy (dry-run mode) because "No"/"FALSE" aren't in
# the lowercase reject list. Found in cold-review of #354.
DRY_RUN = (config_get("DRY_RUN", "") or "").strip().lower() not in ("", "0", "false", "no")


class OpenWriterCheckUnavailable(Exception):
    """`lsof` could not be consulted, so no file can be proven closed."""


def paths_held_open(paths: "list[Path]") -> "set[Path]":
    """Return the subset of `paths` that some process currently holds open.

    Raises OpenWriterCheckUnavailable if the question cannot be answered.

    Why this exists: CONTENT IS NOT A COMPLETION SIGNAL. The strip()-empty guard
    below catches a producer that has written nothing, but a producer that wrote
    a header and paused leaves a file that is non-empty, readable, well-formed
    and still mid-write. Archiving it renames the inode out from under the open
    descriptor, so the producer's remaining flush lands in the archived copy and
    the completed owner-facing message is absent from the live queue — silent
    loss (reproduced on 780f7b6: before='header\\n', archived after the flush =
    'header\\nlater body\\n', original_exists=False).

    No amount of looking at bytes distinguishes "complete" from "truncated",
    because the producer's own intent is not in the file. The only signal that
    actually answers "is anyone still writing this?" is the open-descriptor
    table, so ask the kernel instead of guessing from content.

    Batched into ONE lsof call: the flood this script exists to prevent was 142
    files (post-mortem 2026-04-15), and one subprocess per candidate would turn
    a startup-path sweep into 142 spawns.
    """
    if not paths:
        return set()
    lsof = shutil.which("lsof") or "/usr/sbin/lsof"
    try:
        # -F pn = machine-readable field output: `p<pid>` lines then `n<name>`
        # lines. Plain `-t` prints PIDs only, which cannot be mapped back to
        # WHICH file is open — the distinction we need.
        proc = subprocess.run(
            [lsof, "-F", "pn", "--"] + [str(p) for p in paths],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise OpenWriterCheckUnavailable(str(e)) from e
    # lsof exits 1 when simply nothing matched, which is a valid "none open"
    # answer, not an error. Anything else means we did not get a real answer.
    if proc.returncode not in (0, 1):
        raise OpenWriterCheckUnavailable(
            f"lsof exited {proc.returncode}: {proc.stderr.strip()[:200]}"
        )
    # os.path.realpath, not Path.resolve(): lsof reports names we do not control
    # (it can emit decorated or non-path strings), and realpath is total — it
    # normalizes without raising, where Path.resolve() would need a try/except
    # whose failure mode is hard to trigger and therefore hard to prove. Both
    # sides go through the SAME function so the comparison is apples-to-apples;
    # normalizing only one side would silently miss a symlinked results dir.
    wanted = {os.path.realpath(p): p for p in paths}
    open_now = set()
    for line in proc.stdout.splitlines():
        if not line.startswith("n"):
            continue
        match = wanted.get(os.path.realpath(line[1:]))
        if match is not None:
            open_now.add(match)
    return open_now


def main() -> int:
    if not RESULTS.is_dir():
        print("  [retention] results/ missing — nothing to do")
        return 0

    cutoff = time.time() - RETENTION_HOURS * 3600
    archive_name = datetime.now().strftime("archive-%Y-%m-%d")
    archive_dir = RESULTS / archive_name

    moved = 0
    errors = 0
    skipped_open = 0
    candidates: "list[Path]" = []
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
        candidates.append(f)

    # Prove the producer is done before moving anything. FAIL CLOSED: if the
    # open-descriptor table cannot be consulted, nothing is archived this run.
    # The asymmetry is deliberate and is the whole point of the guard —
    #   * not archiving is self-healing and bounded: the file stays in the live
    #     queue and the next startup sweep retries it, so the worst case is one
    #     delayed cycle of flood-prevention;
    #   * archiving a file that is still being written is unbounded and SILENT:
    #     the completed message exists only in an archive nobody reads.
    # A recoverable, visible flood beats an invisible permanent loss of an
    # owner-facing message. The warning below is loud for the same reason —
    # degrading to the old behaviour quietly would reintroduce exactly the
    # silent-drop class this guard removes.
    check_failed = False
    if candidates:
        try:
            held = paths_held_open(candidates)
        except OpenWriterCheckUnavailable as e:
            check_failed = True
            print(
                f"  [retention] cannot verify open writers ({e}) — archiving"
                f" nothing this run; {len(candidates)} candidate(s) stay in the"
                " live queue and are retried next sweep",
                file=sys.stderr,
            )
            held = set(candidates)
    else:
        held = set()

    for f in candidates:
        if f in held:
            # Say which of the two it is. "Still open by a writer" is a positive
            # observation; "could not check" is the absence of one. Reporting the
            # second as the first would be claiming evidence we do not have.
            reason = (
                "open-writer check unavailable"
                if check_failed
                else "still open by a writer"
            )
            print(f"  [retention] skipping {f.name} — {reason}")
            skipped_open += 1
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
    # `skipped_open` is reported even when nothing moved: a run that archived 0
    # because every candidate was still being written is a DIFFERENT outcome
    # from a run that found nothing stale, and collapsing the two would hide the
    # guard doing its job.
    held_note = f", {skipped_open} still-open file(s) left in place" if skipped_open else ""
    if moved or errors:
        print(
            f"  [retention] {label} {moved} stale file(s) (>{RETENTION_HOURS}h)"
            + (f", {errors} error(s)" if errors else "")
            + (f" to {archive_dir.name}/" if moved and not DRY_RUN else "")
            + held_note
        )
    else:
        print(
            f"  [retention] no stale files to archive (>{RETENTION_HOURS}h cutoff)"
            + held_note
        )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
