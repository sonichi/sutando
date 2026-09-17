#!/usr/bin/env python3
"""Verify a delegated role-status judgment against the task file it was made from.

    scripts/role-status-verify.py <task-file> <judgment.json> [--out <verified.json>]
                                  [--min-coverage 0.5]

Two checks, in order:

  provenance  every row's actor_id and every cited event id must occur verbatim
              in the task file; unknown ids are stripped (one stderr line each),
              a row left with nothing to cite is dropped, blocked_items defaults
              to [].
  coverage    the file's actors that own at least one event set the floor:
              verified rows < ceil(min_coverage * actors_with_events) is refused.

Ids are whole tokens (`ag2space:@<name>:ag2.space`, `ag2space-message:$<id>`), so a
truncated or extended id never matches. Actors and their events are read from the
`EVIDENCE_JSON` object when the evidence carries one (an actor mentioned inside
another actor's event text is not an actor with events); otherwise an actor counts
as having events when it shares a line or a blank-line-delimited block with an
event id.

stdout always carries `rows=<n> actors=<m> actors_with_events=<k> min_rows=<r>`;
with no --out the verified JSON array follows it.

Exit 0: verified JSON written.  1: refused on coverage, nothing written.
2: cannot answer (unreadable input, judgment is not a JSON array).
This script never writes into results/ and knows nothing about result markers.
"""
import argparse
import json
import math
import os
import re
import sys
import tempfile
from typing import Dict, List, Optional, Set, Tuple

ACTOR_RE = re.compile(r"ag2space:@[A-Za-z0-9._-]+:ag2\.space")
EVENT_RE = re.compile(r"ag2space-message:\$[A-Za-z0-9_+/=-]+")

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_CANNOT = 2


def evidence_part(text: str) -> str:
    """Everything after the first line that starts with `task:` (headers excluded)."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("task:"):
            return "".join(lines[i + 1:])
    return ""


def structured_events(evidence: str) -> Optional[List[dict]]:
    """The `events` list of the first JSON-object line in the evidence, if any."""
    for line in evidence.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            obj = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("events"), list):
            return [e for e in obj["events"] if isinstance(e, dict)]
    return None


def actors_with_events_by_block(evidence: str) -> Set[str]:
    found: Set[str] = set()
    for block in re.split(r"\n\s*\n", evidence):
        if EVENT_RE.search(block):
            found.update(ACTOR_RE.findall(block))
    return found


def count_actors(evidence: str) -> Tuple[Set[str], Set[str]]:
    """(distinct actors named in the evidence, those owning at least one event id)."""
    actors = set(ACTOR_RE.findall(evidence))
    events = structured_events(evidence)
    if events is None:
        return actors, actors_with_events_by_block(evidence)
    with_events: Set[str] = set()
    for e in events:
        actor, eid = e.get("actor_id"), e.get("id")
        if isinstance(actor, str) and isinstance(eid, str) \
                and ACTOR_RE.fullmatch(actor) and EVENT_RE.fullmatch(eid):
            with_events.add(actor)
    return actors, with_events


def _known_ids(values, known: Set[str], row_label: str, field: str) -> List[str]:
    kept: List[str] = []
    if not isinstance(values, list):
        values = []
    for v in values:
        if isinstance(v, str) and v in known:
            if v not in kept:
                kept.append(v)
        else:
            sys.stderr.write("strip %s %s: %r not in task file\n" % (row_label, field, v))
    return kept


def verify_rows(rows: list, known_actors: Set[str], known_events: Set[str]) -> List[dict]:
    verified: List[dict] = []
    for i, row in enumerate(rows):
        label = "row[%d]" % i
        if not isinstance(row, dict):
            sys.stderr.write("drop %s: not an object\n" % label)
            continue
        actor = row.get("actor_id")
        if not isinstance(actor, str) or actor not in known_actors:
            sys.stderr.write("drop %s: actor_id %r not in task file\n" % (label, actor))
            continue
        label = "row[%d] %s" % (i, actor)
        working = _known_ids(row.get("working_event_ids"), known_events, label, "working_event_ids")
        blocked: List[dict] = []
        raw_blocked = row.get("blocked_items")
        for j, item in enumerate(raw_blocked if isinstance(raw_blocked, list) else []):
            if not isinstance(item, dict):
                sys.stderr.write("strip %s blocked_items[%d]: not an object\n" % (label, j))
                continue
            ev = _known_ids(item.get("evidence_event_ids"), known_events,
                            label, "blocked_items[%d].evidence_event_ids" % j)
            if not ev:
                sys.stderr.write("strip %s blocked_items[%d]: no evidence left\n" % (label, j))
                continue
            kept = dict(item)
            kept["evidence_event_ids"] = ev
            blocked.append(kept)
        if not working and not blocked:
            sys.stderr.write("drop %s: no verifiable event ids left\n" % label)
            continue
        out = dict(row)
        out["actor_id"] = actor
        out["working_event_ids"] = working
        out["blocked_items"] = blocked
        verified.append(out)
    return verified


def write_atomic(path: str, payload: str) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".role-status-verify.", dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("task_file")
    ap.add_argument("judgment")
    ap.add_argument("--out", default=None, help="write the verified array here (atomic)")
    ap.add_argument("--min-coverage", type=float, default=0.5,
                    help="floor as a fraction of actors_with_events (default 0.5)")
    args = ap.parse_args(argv)

    if not 0.0 <= args.min_coverage <= 1.0:
        sys.stderr.write("cannot answer: --min-coverage must be in [0, 1]\n")
        return EXIT_CANNOT
    try:
        with open(args.task_file, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        sys.stderr.write("cannot answer: task file unreadable: %s\n" % e)
        return EXIT_CANNOT

    evidence = evidence_part(text)
    actors, with_events = count_actors(evidence)
    min_rows = int(math.ceil(args.min_coverage * len(with_events)))
    known_actors = set(ACTOR_RE.findall(text))
    known_events = set(EVENT_RE.findall(text))

    def stats(rows: int) -> None:
        print("rows=%d actors=%d actors_with_events=%d min_rows=%d"
              % (rows, len(actors), len(with_events), min_rows))

    try:
        with open(args.judgment, encoding="utf-8") as fh:
            judgment = json.load(fh)
    except (OSError, ValueError) as e:
        stats(0)
        sys.stderr.write("cannot answer: judgment unreadable: %s\n" % e)
        return EXIT_CANNOT
    if not isinstance(judgment, list):
        stats(0)
        sys.stderr.write("cannot answer: judgment is not a JSON array\n")
        return EXIT_CANNOT

    verified = verify_rows(judgment, known_actors, known_events)
    stats(len(verified))
    if len(verified) < min_rows:
        sys.stderr.write("refused: %d verified row(s) < min_rows=%d (coverage %.2f of %d "
                         "actors with events)\n"
                         % (len(verified), min_rows, args.min_coverage, len(with_events)))
        return EXIT_REFUSED

    payload = json.dumps(verified, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        write_atomic(args.out, payload)
    else:
        sys.stdout.write(payload)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
