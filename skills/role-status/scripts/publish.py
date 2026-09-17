#!/usr/bin/env python3
"""Publish a verified role-status judgment as the task's result.

    skills/role-status/scripts/publish.py <task-file> <judgment.json>
                                          [--workspace DIR] [--min-coverage 0.5]

Reads the task file and the judgment (a leading `[no-send]` line the model may
have added is dropped), verifies the judgment in-process through `verify.py`,
and on success writes `[no-send]\\n<verified JSON array>\\n` (UTF-8) to
`<workspace>/results/<task-file-stem>.txt` -- create-if-absent: a temp file in
results/ is hard-linked to the result name, which fails when the name exists,
so a drain never sees a half-written result. Before that link the publisher
takes a durable per-task claim, `<workspace>/state/role-status/claims/<task-id>`,
created O_EXCL and never removed: the consumer moves the result into
results/archive/, so the result name alone cannot bar a second publisher.
The stats line goes to stdout; every strip/refusal line goes to stderr, as
verify.py prints them.

Exit 0: published.  1: refused on coverage, nothing written -- re-judge with an
explicit coverage instruction.  2: cannot answer (unreadable task file or
judgment, unreadable/ambiguous structured evidence, no event ownership
resolvable, result not serializable, a result already published -- claim or
result name present, results/ or the claim dir unwritable), nothing written.
This script emits the `[no-send]` first line but never parses result markers.
"""
import argparse
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


def claim_exclusive(path: str, note: str) -> bool:
    """O_EXCL create: racing publishers get exactly one claim. False = already claimed."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as fh:
        fh.write(note.encode("utf-8"))
    return True


def create_exclusive(path: str, data: bytes, claim: str) -> str:
    """Stage data beside path, take the claim, then link(2) it in -- link refuses an
    existing target. Returns "published", "claimed" or "exists"; never replaces."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".role-status-publish.", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if not claim_exclusive(claim, path + "\n"):
            return "claimed"
        try:
            os.link(tmp, path)
        except FileExistsError:
            return "exists"
        return "published"
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
        state = create_exclusive(str(out), data, str(claim))
    except OSError as e:
        sys.stderr.write("cannot answer: results/ unwritable: %s\n" % e)
        return EXIT_CANNOT
    if state == "claimed":
        sys.stderr.write("cannot answer: result already published (claim %s)\n" % claim)
        return EXIT_CANNOT
    if state == "exists":
        sys.stderr.write("cannot answer: result already published: %s\n" % out)
        return EXIT_CANNOT
    print("published %s" % out)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
