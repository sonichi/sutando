#!/usr/bin/env python3
"""Publish a verified role-status judgment as the task's result.

    skills/role-status/scripts/publish.py <task-file> <judgment.json>
                                          [--workspace DIR] [--min-coverage 0.5]

Reads the task file and the judgment (a leading `[no-send]` line the model may
have added is dropped), verifies the judgment in-process through `verify.py`,
and on success writes `[no-send]\\n<verified JSON array>\\n` to
`<workspace>/results/<task-file-stem>.txt` -- create-if-absent: a temp file in
results/ is hard-linked to the result name, which fails when the name exists,
so a drain never sees a half-written result and two publishers never both win.
The stats line goes to stdout; every strip/refusal line goes to stderr, as
verify.py prints them.

Exit 0: published.  1: refused on coverage, nothing written -- re-judge with an
explicit coverage instruction.  2: cannot answer (unreadable task file or
judgment, unreadable/ambiguous structured evidence, no event ownership
resolvable, a result already published, results/ unwritable), nothing written.
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


def create_exclusive(path: str, payload: str) -> bool:
    """Publish payload at path only if nothing is there: link(2) refuses an existing
    target, so racing publishers get exactly one winner. False = target already existed."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".role-status-publish.", dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
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

    workspace = Path(args.workspace) if args.workspace else resolve_workspace()
    out = result_path(workspace, args.task_file)
    payload = "[no-send]\n" + json.dumps(outcome.verified, indent=2, ensure_ascii=False) + "\n"
    try:
        os.makedirs(str(out.parent), exist_ok=True)
        published = create_exclusive(str(out), payload)
    except OSError as e:
        sys.stderr.write("cannot answer: results/ unwritable: %s\n" % e)
        return EXIT_CANNOT
    if not published:
        sys.stderr.write("cannot answer: result already published: %s\n" % out)
        return EXIT_CANNOT
    print("published %s" % out)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
