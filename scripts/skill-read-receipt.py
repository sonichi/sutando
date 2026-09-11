#!/usr/bin/env python3
"""Record and check that THIS session read a skill file in full — for ANY skill.

Generalised from `pr-triage/scripts/skill-read-receipt.py`, which proved the shape on
one procedure file. Two properties make it a read rather than a memory of one, and
both are inherited unchanged:

  * Keyed on the sha256 of the BYTES, not a path or a git blob. A working copy that
    differs from HEAD, or any edit at all, invalidates it.
  * Scoped to CLAUDE_CODE_SESSION_ID. A receipt written by another session is exactly
    the "never from memory of it" the contract forbids, so --check ignores it and
    --record refuses to write one that is unscoped.

WHY IT GENERALISES, and what it does NOT claim. A skill's text reaches an agent by
injection from the Claude Code harness -- not this repo's code, and not something we
can make stamp what it served. This works on our side of that boundary: it records
what the agent READ FROM DISK, so a skill with no receipt is one whose injected copy
has never been checked against the file.

Measured 2026-09-11, both directions in one session: an injected skill body was three
days behind disk while `refresh-skill.sh --all` had run twenty minutes earlier, and
separately a served body contained text present in NO file on this machine. A content
hash has no opinion about which side is newer, so it catches both; an mtime comparison
catches neither reliably, which is why this is a hash and not a timestamp.

  --check   0 proceed on this session's receipt / 1 FULL READ REQUIRED
            2 cannot answer (missing file, missing marker, no session id)
  --record  write the receipt AFTER the full read completes
  --audit   every receipt THIS session holds, and whether its bytes still match.
            Catches a skill that changed UNDERNEATH a live session (git pull,
            refresh-skill.sh) after it was read. rc 1 if any drifted. No --skill.
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

RECEIPTS = "skill-read-receipts.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _session_id() -> str:
    return (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()


def _load(state_dir: Path) -> dict:
    try:
        data = json.loads((state_dir / RECEIPTS).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _key(session: str, path: Path) -> str:
    return f"{session}:{path.resolve()}"


def _preconditions(skill: Path, marker: str):
    """(ok, message). Same refusal on both paths, so they cannot disagree."""
    if not skill.is_file():
        return False, f"CANNOT ANSWER: {skill} does not exist — this is the contract's loud STOP"
    try:
        text = skill.read_text(errors="ignore")
    except OSError as e:
        return False, f"CANNOT ANSWER: {skill} is unreadable: {e}"
    if marker and marker not in text:
        return False, (f"CANNOT ANSWER: {skill} lacks the marker clause {marker!r} — "
                       "the contract's loud STOP; do not improvise a pass")
    return True, ""


def check(skill: Path, state_dir: Path, marker: str) -> int:
    ok, why = _preconditions(skill, marker)
    if not ok:
        print(why, file=sys.stderr)
        return 2
    session = _session_id()
    if not session:
        print("CANNOT ANSWER: CLAUDE_CODE_SESSION_ID is unset, so a receipt cannot be "
              "scoped to a reader — read the file in full", file=sys.stderr)
        return 2
    rec = _load(state_dir).get(_key(session, skill))
    digest = _digest(skill)
    if not rec:
        print(f"FULL READ REQUIRED: no receipt for this session ({session[:8]}) — "
              f"read {skill} in full, then --record")
        return 1
    if rec.get("sha256") != digest:
        print(f"FULL READ REQUIRED: content changed since this session's receipt "
              f"({str(rec.get('sha256'))[:12]} -> {digest[:12]}) — read it again")
        return 1
    print(f"receipt valid: this session read {skill.name} in full at {rec.get('recorded_at')} "
          f"and the bytes are unchanged (sha {digest[:12]})")
    return 0


def record(skill: Path, state_dir: Path, marker: str) -> int:
    ok, why = _preconditions(skill, marker)
    if not ok:
        print(why, file=sys.stderr)
        return 2
    session = _session_id()
    if not session:
        # An unscoped receipt would authorise a later session to skip a read it
        # never performed -- the precise failure this file exists to prevent.
        print("REFUSED: CLAUDE_CODE_SESSION_ID is unset; an unscoped receipt would let a "
              "different session skip a read it never did", file=sys.stderr)
        return 2
    import datetime
    state_dir.mkdir(parents=True, exist_ok=True)
    data = _load(state_dir)
    text = skill.read_text(errors="ignore")
    data[_key(session, skill)] = {
        "sha256": _digest(skill),
        "bytes": skill.stat().st_size,
        "lines": text.count("\n") + 1,
        "recorded_at": datetime.datetime.now(datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = state_dir / (RECEIPTS + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    tmp.replace(state_dir / RECEIPTS)
    print(f"recorded: {skill.name} {skill.stat().st_size} B read in full by session "
          f"{session[:8]} (sha {_digest(skill)[:12]})")
    return 0


def audit(state_dir: Path) -> int:
    """Every receipt THIS session holds, and whether the bytes still match.

    A receipt says "I read these exact bytes". If the file changed afterwards the
    session still runs on what it read, and the receipt is the only thing that can
    notice -- nothing else in the process knows a read ever happened.
    """
    session = _session_id()
    if not session:
        print("CANNOT ANSWER: CLAUDE_CODE_SESSION_ID is unset, so this session's "
              "receipts cannot be identified", file=sys.stderr)
        return 2
    mine = {k: v for k, v in _load(state_dir).items() if k.startswith(session + ":")}
    if not mine:
        print(f"no receipts for this session ({session[:8]}) -- nothing read in full yet")
        return 0
    drifted = []
    for key, rec in sorted(mine.items()):
        path = Path(key.split(":", 1)[1])
        if not path.is_file():
            drifted.append((path, "file is GONE since the read"))
            continue
        now = _digest(path)
        if now != rec.get("sha256"):
            drifted.append((path, f"{str(rec.get('sha256'))[:12]} -> {now[:12]}"))
    print(f"{len(mine)} receipt(s) this session; {len(drifted)} drifted since the read")
    for path, why in drifted:
        print(f"  DRIFTED  {path}  ({why})")
    return 1 if drifted else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skill", help="required for --check/--record; unused by --audit")
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--marker", default="",
                    help="required clause; empty means none. pr-triage passes its own.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--record", action="store_true")
    g.add_argument("--audit", action="store_true")
    a = ap.parse_args(argv)
    state_dir = Path(a.state_dir)
    if a.audit:
        return audit(state_dir)
    if not a.skill:
        ap.error("--skill is required for --check and --record")
    skill = Path(a.skill)
    return check(skill, state_dir, a.marker) if a.check else record(skill, state_dir, a.marker)


if __name__ == "__main__":
    sys.exit(main())
