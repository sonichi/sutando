#!/usr/bin/env python3
"""Publish a verified role-status judgment as the task's result.

    skills/role-status/scripts/publish.py <task-file> <judgment.json>
                                          [--workspace DIR] [--min-coverage 0.5]

Reads the task file and the judgment (a leading `[no-send]` line the model may
have added is dropped), verifies the judgment in-process through `verify.py`,
and on success writes `[no-send]\\n<verified JSON array>\\n` (UTF-8) to
`<workspace>/results/<task-file-stem>.txt` -- create-if-absent: a temp file in
results/ is hard-linked to the result name, which fails when the name exists,
so a drain never sees a half-written result. The consumer moves the result into
results/archive/, so the result name alone cannot bar a second publisher: a
durable claim, `<workspace>/state/role-status/claims/<task-id>`, is the record.
Two phases under flock(2): the claim is created EMPTY (in progress) and holds
the lock until the result links, then its body becomes the result path
(committed, permanent). A failure before commit removes the claim, so the task
is retryable; an empty claim found unlocked is abandoned (a crash) and is
recovered: committed against a result found live or archived, else reused.
The stats line goes to stdout; every strip/refusal line goes to stderr, as
verify.py prints them.

Exit 0: published.  1: refused on coverage, nothing written -- re-judge with an
explicit coverage instruction.  2: cannot answer (unreadable task file or
judgment, unreadable/ambiguous structured evidence, no event ownership
resolvable, result not serializable, a result already published -- committed
claim or result name present, another publisher holding the claim, results/ or
the claim dir unwritable), nothing written.
This script emits the `[no-send]` first line but never parses result markers.
"""
import argparse
import fcntl
import glob
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(1, str(Path(__file__).resolve().parents[3] / "src"))
import verify  # noqa: E402
from workspace_default import resolve_workspace  # noqa: E402

EXIT_OK, EXIT_REFUSED, EXIT_CANNOT = verify.EXIT_OK, verify.EXIT_REFUSED, verify.EXIT_CANNOT


def result_path(workspace: Path, task_file: str) -> Path:
    return workspace / "results" / (Path(task_file).stem + ".txt")


def claim_path(workspace: Path, task_file: str) -> Path:
    """The one durable record that <task-id> was published; results/ is not, since
    the consumer moves the result into results/archive/."""
    return workspace / "state" / "role-status" / "claims" / Path(task_file).stem


def claim_open(path: str) -> Optional[int]:
    """Create-if-absent (never truncate) and lock the claim; the lock outlives nothing
    but this process, so an unlocked empty claim is a crash. None = held by another."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def claim_body(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    return os.read(fd, 4096).decode("utf-8", "replace").strip()


def claim_commit(fd: int, note: str) -> Optional[str]:
    """One write under the lock turns the empty claim into a committed one.
    Returns the error text when the write failed (the caller then drops the claim)."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, (note + "\n").encode("utf-8"))
        os.fsync(fd)
    except OSError as e:
        return str(e)
    return None


def find_result(workspace: Path, task_file: str) -> Optional[Path]:
    """The live result, else the consumer's archive copy (results/archive/<ym>/<id>*.txt)."""
    live = result_path(workspace, task_file)
    if live.is_file():
        return live
    stem = Path(task_file).stem
    hits = sorted(glob.glob(str(workspace / "results" / "archive" / "*" / (glob.escape(stem) + "*.txt"))))
    return Path(hits[0]) if hits else None


def link_exclusive(path: str, data: bytes) -> bool:
    """Stage data beside path, then link(2) it in -- link refuses an existing target,
    never replaces. False = the target already existed."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".role-status-publish.", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("task_file")
    ap.add_argument("judgment")
    ap.add_argument("--workspace", default=None, help="override the resolved workspace")
    ap.add_argument("--min-coverage", type=float, default=0.5,
                    help="floor as a fraction of actors_with_events (default 0.5)")
    args = ap.parse_args(argv)

    try:
        with open(args.task_file, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        sys.stderr.write("cannot answer: task file unreadable: %s\n" % e)
        return EXIT_CANNOT
    try:
        judgment = verify.read_judgment(args.judgment)
    except (OSError, ValueError) as e:
        sys.stderr.write("cannot answer: judgment unreadable: %s\n" % e)
        return EXIT_CANNOT

    outcome = verify.verify(text, judgment, args.min_coverage)
    if outcome.stats is not None:
        print(outcome.stats)
    if outcome.rc != EXIT_OK:
        sys.stderr.write(outcome.reason + "\n")
        return outcome.rc

    try:
        data = ("[no-send]\n" + json.dumps(outcome.verified, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as e:
        sys.stderr.write("cannot answer: result not serializable: %s\n" % e)
        return EXIT_CANNOT

    workspace = Path(args.workspace) if args.workspace else resolve_workspace()
    out = result_path(workspace, args.task_file)
    claim = claim_path(workspace, args.task_file)
    try:
        os.makedirs(str(out.parent), exist_ok=True)
        os.makedirs(str(claim.parent), exist_ok=True)
        fd = claim_open(str(claim))
    except OSError as e:
        sys.stderr.write("cannot answer: results/ unwritable: %s\n" % e)
        return EXIT_CANNOT
    if fd is None:
        sys.stderr.write("cannot answer: publication in progress (claim %s)\n" % claim)
        return EXIT_CANNOT
    committed = bool(claim_body(fd))
    try:
        if committed:
            sys.stderr.write("cannot answer: result already published (claim %s)\n" % claim)
            return EXIT_CANNOT
        found = find_result(workspace, args.task_file)
        if found is not None:
            committed = claim_commit(fd, str(found)) is None
            sys.stderr.write("cannot answer: result already published (claim %s) -- recovered from %s\n"
                             % (claim, found))
            return EXIT_CANNOT
        try:
            linked = link_exclusive(str(out), data)
        except OSError as e:
            sys.stderr.write("cannot answer: results/ unwritable: %s\n" % e)
            return EXIT_CANNOT
        if not linked:
            sys.stderr.write("cannot answer: result already present at %s\n" % out)
            return EXIT_CANNOT
        err = claim_commit(fd, str(out))
        committed = err is None
        print("published %s" % out)
        if err is not None:
            sys.stderr.write("warning: claim not committed (%s); a retry recovers it from the result\n" % err)
        return EXIT_OK
    finally:
        # Under the lock still: an uncommitted claim is ours alone to drop.
        if not committed:
            try:
                os.unlink(str(claim))
            except OSError:
                pass
        os.close(fd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
