#!/usr/bin/env python3
"""Verify a delegated role-status judgment against the task file it was made from.

    skills/role-status/scripts/verify.py <task-file> <judgment.json> [--out <verified.json>]
                                         [--min-coverage 0.5]

Two checks, in order:

  provenance  every row's actor_id must occur verbatim in the task file and every
              cited event id must be OWNED by that actor (an id in the file that
              belongs to another actor is stripped like a fabricated one, one
              stderr line each); a row left with nothing to cite is dropped, a
              second row for an actor already seen is dropped as a duplicate,
              blocked_items defaults to [].
  coverage    the file's actors that own at least one event set the floor:
              distinct verified actors < ceil(min_coverage * actors_with_events)
              is refused.

Ids are whole tokens (`ag2space:@<name>:ag2.space`, `ag2space-message:$<id>`):
an id counts only when no character of the id alphabet touches either end, so a
truncated or extended id never matches and a cited id must equal a whole token.
Ownership comes from the object that follows the one line starting with
`EVIDENCE_JSON` (`events[].actor_id` owns `events[].id`; an actor mentioned
inside another actor's event text owns nothing). A marker whose object is
malformed, or a second marker line, is "cannot answer" -- never the fallback and
never a zero floor. Only evidence with no marker at all uses the block fallback:
an event id is owned when its blank-line-delimited block names exactly ONE
actor. On either path an id claimed by more than one actor is ambiguous -- owned
by nobody (a citation is stripped as `ambiguous ownership`) while every claimant
still counts toward the coverage floor.

stdout carries
`rows=<n> distinct_actors=<d> actors=<m> actors_with_events=<k> min_rows=<r>`
whenever the evidence was readable (rows as submitted, distinct actors among the
verified rows); with no --out the verified JSON array follows it.

Exit 0: verified JSON written.  1: refused on coverage, nothing written.
2: cannot answer (unreadable input, structured evidence unreadable/ambiguous,
judgment is not a JSON array, --out unwritable).
This script never writes into results/ and knows nothing about result markers.
"""
import argparse
import json
import math
import os
import re
import sys
import tempfile
from typing import Dict, List, NamedTuple, Optional, Set

# Union of both id alphabets: a token touching one of these on either side is a
# different (longer) token, never this id.
ID_CHARS = r"A-Za-z0-9._:@$+/=-"
ACTOR_RE = re.compile(r"(?<![%s])ag2space:@[A-Za-z0-9._-]+:ag2\.space(?![%s])" % (ID_CHARS, ID_CHARS))
EVENT_RE = re.compile(r"(?<![%s])ag2space-message:\$[A-Za-z0-9_+/=-]+(?![%s])" % (ID_CHARS, ID_CHARS))
MARKER = "EVIDENCE_JSON"

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


class EvidenceError(ValueError):
    """The marked structured evidence cannot be read: malformed object or two markers."""


def structured_events(evidence: str) -> Optional[List[dict]]:
    """The `events` list of the object following the `EVIDENCE_JSON` marker line.

    None when no line starts with the marker; EvidenceError when the marker
    occurs twice or its object is not a JSON object carrying an `events` list."""
    lines = evidence.splitlines(keepends=True)
    marks = [i for i, line in enumerate(lines) if line.startswith(MARKER)]
    if not marks:
        return None
    if len(marks) > 1:
        raise EvidenceError("%d %s marker lines" % (len(marks), MARKER))
    rest = lines[marks[0]][len(MARKER):]
    tail = "".join(lines[marks[0] + 1:])
    # The object opens on the marker line itself or is the next non-blank text.
    text = rest[rest.index("{"):] + tail if "{" in rest else tail.lstrip()
    try:
        obj, _ = json.JSONDecoder().raw_decode(text)
    except ValueError as e:
        raise EvidenceError("object after %s does not parse: %s" % (MARKER, e))
    if not isinstance(obj, dict) or not isinstance(obj.get("events"), list):
        raise EvidenceError("object after %s carries no `events` list" % MARKER)
    return [e for e in obj["events"] if isinstance(e, dict)]


Ownership = Dict[str, Set[str]]


class Evidence(NamedTuple):
    """actors: every id named in the evidence; ownership: actor -> ids it unambiguously
    owns; ambiguous_ids: ids nobody may cite; ambiguous_actors: set the floor, own nothing."""
    actors: Set[str]
    ownership: Ownership
    ambiguous_ids: Set[str]
    ambiguous_actors: Set[str]

    def with_events(self) -> Set[str]:
        return {a for a, ids in self.ownership.items() if ids} | self.ambiguous_actors


def resolve_claims(evidence: str, claims: Dict[str, Set[str]],
                   ambiguous_ids: Set[str], ambiguous_actors: Set[str]) -> Evidence:
    """An id with exactly one claimant is owned; with several it is ambiguous and
    every claimant still sets the floor. One rule for both evidence shapes."""
    owned: Ownership = {}
    for eid, claimants in claims.items():
        if eid in ambiguous_ids or len(claimants) > 1:
            ambiguous_ids.add(eid)
            ambiguous_actors.update(claimants)
        else:
            owned.setdefault(next(iter(claimants)), set()).add(eid)
    return Evidence(set(ACTOR_RE.findall(evidence)), owned, ambiguous_ids, ambiguous_actors)


def owned_events_by_block(evidence: str) -> Evidence:
    """Each blank-line-delimited block claims its event ids for the one actor it
    names; a block naming several actors makes its ids ambiguous outright."""
    claims: Dict[str, Set[str]] = {}
    ambiguous_ids: Set[str] = set()
    ambiguous_actors: Set[str] = set()
    for block in re.split(r"\n\s*\n", evidence):
        events = set(EVENT_RE.findall(block))
        actors = set(ACTOR_RE.findall(block))
        if not events or not actors:
            continue
        if len(actors) > 1:
            ambiguous_ids.update(events)
            ambiguous_actors.update(actors)
            continue
        for eid in events:
            claims.setdefault(eid, set()).add(next(iter(actors)))
    return resolve_claims(evidence, claims, ambiguous_ids, ambiguous_actors)


def count_actors(evidence: str) -> Evidence:
    """Ownership from the marked object when the evidence carries one, else by block."""
    events = structured_events(evidence)
    if events is None:
        return owned_events_by_block(evidence)
    claims: Dict[str, Set[str]] = {}
    for e in events:
        actor, eid = e.get("actor_id"), e.get("id")
        if isinstance(actor, str) and isinstance(eid, str) \
                and ACTOR_RE.fullmatch(actor) and EVENT_RE.fullmatch(eid):
            claims.setdefault(eid, set()).add(actor)
    return resolve_claims(evidence, claims, set(), set())


def _owned_ids(values, known: Set[str], owned: Set[str], ambiguous: Set[str],
               row_label: str, field: str) -> List[str]:
    kept: List[str] = []
    if not isinstance(values, list):
        values = []
    for v in values:
        if not isinstance(v, str) or v not in known:
            sys.stderr.write("strip %s %s: %r not in task file\n" % (row_label, field, v))
        elif v in ambiguous:
            sys.stderr.write("strip %s %s: %r ambiguous ownership\n" % (row_label, field, v))
        elif v not in owned:
            sys.stderr.write("strip %s %s: %r not owned by actor\n" % (row_label, field, v))
        elif v not in kept:
            kept.append(v)
    return kept


def verify_rows(rows: list, known_actors: Set[str], known_events: Set[str],
                ownership: Ownership, ambiguous: Optional[Set[str]] = None) -> List[dict]:
    ambiguous = ambiguous or set()
    verified: List[dict] = []
    seen: Set[str] = set()
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
        if actor in seen:
            sys.stderr.write("drop %s: duplicate actor row\n" % label)
            continue
        seen.add(actor)
        owned = ownership.get(actor, set())
        working = _owned_ids(row.get("working_event_ids"), known_events, owned, ambiguous,
                             label, "working_event_ids")
        blocked: List[dict] = []
        raw_blocked = row.get("blocked_items")
        for j, item in enumerate(raw_blocked if isinstance(raw_blocked, list) else []):
            if not isinstance(item, dict):
                sys.stderr.write("strip %s blocked_items[%d]: not an object\n" % (label, j))
                continue
            ev = _owned_ids(item.get("evidence_event_ids"), known_events, owned, ambiguous,
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


class Outcome(NamedTuple):
    """verified: the array to publish, only on rc 0; stats: the stats line, None when
    the evidence was unreadable; reason: the stderr line, "" on rc 0."""
    rc: int
    verified: Optional[List[dict]]
    stats: Optional[str]
    reason: str


def verify(task_text: str, judgment: object, min_coverage: float = 0.5) -> Outcome:
    """Provenance-strip then coverage-check one parsed judgment against one task text."""
    if not 0.0 <= min_coverage <= 1.0:
        return Outcome(EXIT_CANNOT, None, None, "cannot answer: --min-coverage must be in [0, 1]")
    try:
        ev = count_actors(evidence_part(task_text))
    except EvidenceError as e:
        return Outcome(EXIT_CANNOT, None, None,
                       "cannot answer: structured evidence unreadable/ambiguous: %s" % e)
    with_events = ev.with_events()
    min_rows = int(math.ceil(min_coverage * len(with_events)))

    def stats(rows: int, distinct: int) -> str:
        return ("rows=%d distinct_actors=%d actors=%d actors_with_events=%d min_rows=%d"
                % (rows, distinct, len(ev.actors), len(with_events), min_rows))

    if not isinstance(judgment, list):
        return Outcome(EXIT_CANNOT, None, stats(0, 0), "cannot answer: judgment is not a JSON array")
    verified = verify_rows(judgment, set(ACTOR_RE.findall(task_text)), set(EVENT_RE.findall(task_text)),
                           ev.ownership, ev.ambiguous_ids)
    distinct = len({r["actor_id"] for r in verified})
    if distinct < min_rows:
        return Outcome(EXIT_REFUSED, None, stats(len(judgment), distinct),
                       "refused: %d distinct verified actor(s) < min_rows=%d (coverage %.2f "
                       "of %d actors with events)" % (distinct, min_rows, min_coverage, len(with_events)))
    return Outcome(EXIT_OK, verified, stats(len(judgment), distinct), "")


def read_judgment(path: str) -> object:
    """The parsed judgment file; a leading `[no-send]` line the model added is dropped."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    first, _, rest = text.lstrip().partition("\n")
    return json.loads(rest if first.strip() == "[no-send]" else text)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("task_file")
    ap.add_argument("judgment")
    ap.add_argument("--out", default=None, help="write the verified array here (atomic)")
    ap.add_argument("--min-coverage", type=float, default=0.5,
                    help="floor as a fraction of actors_with_events (default 0.5)")
    args = ap.parse_args(argv)

    try:
        with open(args.task_file, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        sys.stderr.write("cannot answer: task file unreadable: %s\n" % e)
        return EXIT_CANNOT
    judgment_error = None
    try:
        judgment = read_judgment(args.judgment)
    except (OSError, ValueError) as e:
        judgment, judgment_error = None, "cannot answer: judgment unreadable: %s" % e

    outcome = verify(text, judgment, args.min_coverage)
    if outcome.stats is not None:
        print(outcome.stats)
    if outcome.rc != EXIT_OK:
        sys.stderr.write((judgment_error if judgment_error and outcome.stats else outcome.reason) + "\n")
        return outcome.rc

    payload = json.dumps(outcome.verified, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        try:
            write_atomic(args.out, payload)
        except OSError as e:
            sys.stderr.write("cannot answer: --out unwritable: %s\n" % e)
            return EXIT_CANNOT
    else:
        sys.stdout.write(payload)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
