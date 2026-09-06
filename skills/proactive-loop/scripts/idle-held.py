#!/usr/bin/env python3
"""Emit the idle-surface held-list as a DIFF against the record — never a whole list.

WHY: `idle-surface-hash.py` takes the full set from its caller (`--items` or
stdin). Handed that interface, an agent builds the list from RECALL, and a
recall-built list is a different set wearing the same name — so the hash says
"post", and `--commit` then overwrites the legitimate baseline with it.

Measured three times on this host, by three passes. The third overwrote the
baseline with a 4-item list built from memory while the record held 18 items
under different ids ('3198' vs 'sutando-3198', 'cinny-690' vs 'cinny-700').
Each pass had the warning in the very file it was writing to — because the list
gets BUILT before the file gets READ. That ordering is the defect, and prose
cannot fix it.

So this tool never accepts a list. It reads `held_item_ids` from the state file
and applies explicit add/remove operations:

  idle-held.py --state <ws>/state/idle-streak.json --remove cinny-717 --reason merged
  idle-held.py --state ... --add ds-pr-13:owner
  idle-held.py --state ... --remove X --reason "..." --write | idle-surface-hash.py --state ... --commit

A removal REQUIRES a reason, because a silent shrink is the failure that
corrupted the baseline. Nothing writes `held_item_ids` but this tool — verified
across 10,024 files in both trees: the key appears only in the state file, in
prose records, and in one patch. It was hand-maintained, which is why it drifted.

exit 0 ok · 1 refused (an op the record contradicts) · 2 cannot answer
"""
from __future__ import annotations

import argparse
import json
import time
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import idle_state  # noqa: E402
from idle_state import ABORT, REFUSED, locked_update  # noqa: E402

KEY = "held_item_ids"


def init_empty(state: Path) -> int:
    """The one path that may create the key, and only when it is absent.
    [] asserts nothing held, so this cannot become the invented list."""
    if not state.is_file():
        print(f"CANNOT ANSWER: no state file at {state}", file=sys.stderr)
        return 2
    try:
        json.loads(state.read_text())
    except ValueError as exc:
        print(f"CANNOT ANSWER: state file is not JSON: {exc}", file=sys.stderr)
        return 2

    refusal = []

    def seed(doc):
        # Re-read under the lock: the parse above proves the file is JSON, not
        # that it still lacks the key when the lock is finally held.
        if KEY in doc:
            refusal.append(len(doc[KEY]) if isinstance(doc[KEY], list) else "?")
            return ABORT
        doc[KEY] = []
        doc["held_item_seed"] = {
            "seeded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "by": "idle-held.py --init-empty",
            "note": "empty bootstrap; the record is populated by --add, never by a list",
        }
        return 0

    outcome = locked_update(state, seed, indent=2)
    if outcome is REFUSED:
        return 2
    if outcome is ABORT:
        print(f"REFUSED: {state} already has {KEY} ({refusal[0]} entries) "
              "— --init-empty only bootstraps, it never clears", file=sys.stderr)
        return 2
    print(f"seeded {KEY} = [] in {state}  (provenance in held_item_seed)")
    return 0


def load(state: Path):
    if not state.is_file():
        return None, f"no state file at {state}"
    try:
        d = json.loads(state.read_text())
    except ValueError as exc:
        return None, f"state file is not JSON: {exc}"
    if KEY not in d:
        return None, (f"state file has no {KEY} — refusing to invent one. "
                      "Bootstrap it with --init-empty (seeds [], never a list).")
    items = d[KEY]
    if not isinstance(items, list) or any(
        not (isinstance(p, (list, tuple)) and len(p) == 2) for p in items
    ):
        return None, f"{KEY} is not a list of [id, gate] pairs"
    return d, None


def apply_ops(items, adds, removes):
    """Return (new_items, errors). Order preserved; ids are compared verbatim."""
    cur = [list(p) for p in items]
    have = {p[0] for p in cur}
    errs = []
    for rid in removes:
        if rid not in have:
            near = sorted(x for x in have if rid in x or x in rid)
            errs.append(f"--remove {rid!r}: not in the record"
                        + (f" (did you mean {near}?)" if near else ""))
    for spec in adds:
        aid = spec[0]
        if aid in have:
            errs.append(f"--add {aid!r}: already held — an add cannot restate a member")
    if errs:
        return None, errs
    out = [p for p in cur if p[0] not in set(removes)]
    out.extend([list(a) for a in adds])
    return out, []


BRANCH_SHA = re.compile(r"`?([\w./-]+/[\w./-]+)`?\s*@\s*`?([0-9a-f]{7,40})`?")


def audit_notes(doc, repo) -> int:
    """A sha written into a note is a COPY of a fact git owns, and copies drift.

    Measured 2026-09-01: two notes carried shas that `current-track.md` had already
    corrected — a third record of the same fact, disagreeing with both. ids have a
    guard (this tool); notes had none.
    """
    notes = doc.get("held_item_notes") or {}
    # ABSENT ids and EMPTY ids differ: with no `held_item_ids` key at all,
    # orphanhood is unanswerable, so no note may be called an orphan.
    raw_ids = doc.get("held_item_ids")
    held = ({i for i, _ in raw_ids if isinstance(i, str)}
            if isinstance(raw_ids, list) else None)
    pairs = [(k, m.group(1), m.group(2))
             for k, v in notes.items() for m in BRANCH_SHA.finditer(str(v))]
    print(f"{len(notes)} note(s), {len(pairs)} `<branch> @ <sha>` reference(s) found")

    # An ORPHAN note outlives the id it described. A sha-only audit cannot see one:
    # an orphan carrying no branch@sha reference is invisible to that scan entirely.
    orphans = sorted(k for k in notes if k not in held) if held is not None else []
    if held is None and notes:
        print("  ?      orphan check SKIPPED — no held_item_ids in this doc, so "
              "orphanhood cannot be determined")
    for k in orphans:
        print(f"  ORPHAN {k:<24} note kept for an id the record no longer holds")
    # A held id with NO note is not an error — 9 of 19 were note-less on the live
    # state, so failing here would fire every run and get the check demoted.
    missing = sorted(held - set(notes)) if held is not None else []
    if missing:
        print(f"  {len(missing)} held id(s) with no note (not an error): "
              f"{', '.join(missing[:6])}{' …' if len(missing) > 6 else ''}")

    if not pairs:
        print("no branch@sha references — nothing this check can discriminate")
        return 1 if orphans else 0
    bad = 0
    for key, br, sha in pairs:
        r = subprocess.run(["git", "rev-parse", f"--short={len(sha)}", br],
                           cwd=repo, capture_output=True, text=True)
        real = r.stdout.strip()
        if r.returncode != 0:
            print(f"  ?     {key:<24} {br} -> git cannot resolve the branch")
            continue
        ok = real == sha
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'DRIFT'} {key:<24} note={sha} git={real}")
    print(f"\n{len(pairs) - bad} of {len(pairs)} match git"
          + (f"; {len(orphans)} orphan note(s)" if orphans else ""))
    return 1 if (bad or orphans) else 0


ARCHIVE_KEY = "held_item_notes_archived"


def archive_orphan_notes(doc, when):
    """An orphan note outlives the id it described, and nothing could clear it.

    Archived, never deleted: the note is recorded context, and a permanently-red
    audit stops being consulted long before anyone prunes it by hand.
    """
    raw_ids = doc.get(KEY)
    if raw_ids is None:
        return None, f"no {KEY} in this doc, so orphanhood cannot be determined"
    notes = doc.get("held_item_notes")
    if not isinstance(notes, dict):
        return None, "held_item_notes is absent or not an object — nothing to archive"
    held = {i for i, _ in raw_ids}
    orphans = sorted(k for k in notes if k not in held)
    return {k: notes[k] for k in orphans}, None


PR_REF = re.compile(r"\b([A-Za-z0-9][\w.-]*/[\w.-]+)#(\d+)\b")


def audit_prs(doc) -> int:
    """A PR-backed held item can go stale the moment the PR reaches a terminal state.

    Measured 2026-09-01: `sutando-3487-automerge` sat on the list as "waiting on the
    owner" for 25 minutes after an armed auto-merge landed it. An FYI surface built
    from that list would have reported a blocker that no longer existed.

    MAPPING IS EXPLICIT OR IT DOES NOT HAPPEN. Only an `owner/repo#number` written
    into the item's own note is used. Inferring a PR from the id is how a caller
    ends up acting on an artifact: doing it by hand, the id `stroke-fix-36177568`
    parsed as PRs 36177 AND 568, neither of which exists. An UNMAPPED item is
    reported as unmapped, never guessed at.
    """
    notes = doc.get("held_item_notes") or {}
    raw = doc.get("held_item_ids")
    if not isinstance(raw, list):
        print("CANNOT ANSWER: no held_item_ids list", file=sys.stderr)
        return 2
    ids = [i for i, _ in raw if isinstance(i, str)]
    mapped, unmapped, stale, errors = [], [], [], []
    for hid in ids:
        m = PR_REF.search(str(notes.get(hid, "")))
        if not m:
            unmapped.append(hid)
            continue
        repo, num = m.group(1), m.group(2)
        try:
            r = subprocess.run(["gh", "pr", "view", num, "--repo", repo, "--json",
                                "state,mergeStateStatus"],
                               capture_output=True, text=True, timeout=30)
        except Exception as e:
            errors.append((hid, f"{repo}#{num}", str(e)[:60])); continue
        if r.returncode != 0:
            errors.append((hid, f"{repo}#{num}", r.stderr.strip()[:60])); continue
        d = json.loads(r.stdout)
        mapped.append((hid, f"{repo}#{num}", d["state"], d.get("mergeStateStatus")))
        if d["state"] != "OPEN":
            stale.append((hid, f"{repo}#{num}", d["state"]))
    print(f"{len(ids)} held id(s): {len(mapped)} PR-mapped, {len(unmapped)} unmapped, "
          f"{len(errors)} unresolvable")
    for hid, ref, state, mss in mapped:
        mark = "  <-- TERMINAL: retire it" if state != "OPEN" else ""
        print(f"  {'stale' if state != 'OPEN' else 'ok   '}  {hid:32} {ref:34} {state} {mss}{mark}")
    if unmapped:
        print(f"  unmapped (no `owner/repo#n` in the note, NOT guessed): "
              f"{', '.join(unmapped[:8])}{' …' if len(unmapped) > 8 else ''}")
    for hid, ref, e in errors:
        print(f"  ERROR  {hid} {ref}: {e}")
    if errors:
        # A PR we could not read is not a PR we know is fine.
        print("\ncannot answer for the row(s) above — not a clean bill", file=sys.stderr)
        return 2
    if stale:
        print("\nSTALE held item(s) — the blocker no longer exists:")
        for hid, ref, state in stale:
            print(f"  {hid}  ({ref} is {state})  "
                  f"-> --remove {hid} --reason \"{ref} {state}\"")
        return 1
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True)
    ap.add_argument("--add", action="append", default=[], metavar="ID:GATE")
    ap.add_argument("--remove", action="append", default=[], metavar="ID")
    ap.add_argument("--reason", action="append", default=[],
                    help="why an id is being removed; one per --remove")
    ap.add_argument("--write", action="store_true",
                    help="persist the new held_item_ids (atomic; other keys untouched)")
    ap.add_argument("--audit-prs", action="store_true",
                    help="check every held item whose note carries an explicit `owner/repo#n`; "
                         "exit 1 if any such PR is terminal (the held item is stale)")
    ap.add_argument("--audit-notes", metavar="REPO",
                    help="resolve every `<branch> @ <sha>` in held_item_notes against that git "
                         "repo and report drift; exit 1 if any note disagrees with git")
    ap.add_argument("--init-empty", action="store_true",
                    help="create held_item_ids as [] on a state file that has "
                         "no such key, so --add works; REFUSES if it exists")
    ap.add_argument("--archive-orphan-notes", action="store_true",
                    help="move every held_item_notes entry whose id is no longer held into "
                         "held_item_notes_archived (never deletes); needs --write to persist")
    a = ap.parse_args(argv)

    state = Path(a.state)

    if a.init_empty:
        return init_empty(state)

    doc, err = load(state)
    if err:
        print(f"CANNOT ANSWER: {err}", file=sys.stderr)
        return 2

    if a.archive_orphan_notes:
        moved, err = archive_orphan_notes(doc, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        if err:
            print(f"CANNOT ANSWER: {err}", file=sys.stderr)
            return 2
        if not moved:
            print("no orphan notes — nothing to archive")
            return 0
        for k in sorted(moved):
            print(f"  archive {k:<24} {str(moved[k])[:70]}")
        print(f"{len(moved)} orphan note(s)"
              + ("" if a.write else "  (not written; pass --write)"), file=sys.stderr)
        if a.write:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

            def archive(fresh):
                # Re-derive from the doc read under the lock: `moved` above came
                # from a snapshot and the notes may have changed since.
                again, err2 = archive_orphan_notes(fresh, stamp)
                if err2 or not again:
                    return ABORT
                notes = fresh["held_item_notes"]
                arch = fresh.setdefault(ARCHIVE_KEY, {})
                for k in again:
                    arch[k] = {"note": notes.pop(k), "archived_at": stamp}
                return None

            if locked_update(state, archive, indent=2) is REFUSED:
                return 2
        return 0

    if a.audit_prs:
        return audit_prs(doc)

    if a.audit_notes:
        return audit_notes(doc, a.audit_notes)

    if a.remove and len(a.reason) != len(a.remove):
        print(f"REFUSED: {len(a.remove)} --remove but {len(a.reason)} --reason. "
              "A silent shrink is the failure this tool exists to stop.", file=sys.stderr)
        return 1

    adds = []
    for spec in a.add:
        if ":" not in spec:
            print(f"REFUSED: --add {spec!r} must be ID:GATE", file=sys.stderr)
            return 1
        i, g = spec.split(":", 1)
        if not i.strip() or not g.strip():
            print(f"REFUSED: --add {spec!r} has an empty id or gate", file=sys.stderr)
            return 1
        adds.append((i.strip(), g.strip()))

    before = doc[KEY]
    after, errs = apply_ops(before, adds, a.remove)
    if errs:
        for e in errs:
            print(f"REFUSED: {e}", file=sys.stderr)
        return 1

    print(json.dumps(after, separators=(",", ":")))
    print(f"held {len(before)} -> {len(after)}"
          + (f"  +{len(adds)}" if adds else "")
          + (f"  -{len(a.remove)}" if a.remove else "")
          + ("  (not written; pass --write)" if not a.write else ""),
          file=sys.stderr)
    for rid, why in zip(a.remove, a.reason):
        print(f"  removed {rid}: {why}", file=sys.stderr)

    if a.write:
        def apply_under_lock(fresh):
            # The ops are a DIFF, so re-apply them to the doc read under the
            # lock rather than writing back the snapshot `after` came from.
            fresh[KEY], errs2 = apply_ops(fresh.get(KEY) or [], adds, a.remove)
            if errs2:
                for e in errs2:
                    print(f"REFUSED: {e}", file=sys.stderr)
                return ABORT
            log = fresh.setdefault("held_item_removals", [])
            for rid, why in zip(a.remove, a.reason):
                log.append({"id": rid, "reason": why})
            return None

        res = locked_update(state, apply_under_lock, indent=2)
        if res is REFUSED:
            return 2
        if res is ABORT:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
