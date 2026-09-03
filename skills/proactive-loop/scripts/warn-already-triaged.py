#!/usr/bin/env python3
"""For each health-check warn, say whether a triage is ALREADY PARKED.

Health-check warns are chronic: they re-fire every pass until an owner decision
lands, so the survivors are precisely the ones already investigated. Re-deriving
one is waste; ACTING on one is worse (a re-registration nearly undid a deliberate
cron-drift decision on 2026-08-28).

The hard part is that a warn is named for its DETECTOR (`memory-index`) while the
triage is filed under the SUBJECT (`MEMORY.md`, `sparrow-pr-shepherd`). Measured:
the probe name alone hit 2 of 5. So search BOTH — the probe name AND entities
lifted from the warn TEXT — across BOTH parking files.

  python3 src/health-check.py 2>&1 | python3 skills/proactive-loop/scripts/warn-already-triaged.py

Output per warn: PARKED (with file:line + date) or UNTRIAGED. A PARKED hit is a
pointer to read, not permission to skip: extend it with what is new, or say
plainly that nothing is new.
"""
import pathlib
import re
import sys

def parking_files():
    """The per-host parking files, resolved by the repo's own `personal_path`."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src"))
    from util_paths import personal_path
    return [p for p in (personal_path("pending-questions.md"),
                        personal_path("current-track.md")) if p.exists()]

# entities worth searching for: paths, dotted filenames, backticked identifiers
ENT = re.compile(r'`([^`]{3,40})`|([\w./-]+\.(?:py|sh|json|md|ts|yml))|\b([a-z][a-z0-9]+(?:-[a-z0-9]+){1,3})\b')
STOP = {"health-check", "not-running", "restart-needed", "session-read", "read-limit"}

def tokens(name, text):
    out = [name] if name else []
    for m in ENT.finditer(text):
        t = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        if 3 <= len(t) <= 40 and t.lower() not in STOP and t != name:
            out.append(t)
    seen, uniq = set(), []
    for t in out:
        if t.lower() not in seen:
            seen.add(t.lower()); uniq.append(t)
    return uniq[:8]

def report(name, text, files):
    """Print the parking verdict for one item. True if genuinely untriaged.

    Shared by the warn path and --claim so the two cannot drift: a claim and a
    warn are the same question asked about different input.
    """
    label = name or "this claim"
    toks = tokens(name, text)
    if not toks:
        # Zero tokens means the searches below never execute, so "no hits" is
        # produced by construction. That is cannot-answer, never untriaged.
        print(f"  NO NOUNS   {label:25} — extracted 0 searchable tokens, so NOTHING was "
              f"searched. Cannot answer; re-state with a filename, a `backticked` term, "
              f"or a hyphenated-name.")
        return "no_tokens"
    hits, seen_at = [], set()
    for tok in toks:
        for f in files:
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if tok.lower() in line.lower() and line.lstrip().startswith("#"):
                    if (f.name, i) not in seen_at:
                        seen_at.add((f.name, i))
                        hits.append((tok, f.name, i, line.strip()[:92]))
                    break
    if hits:
        # ALL candidates, not just the first. A probe warns for SEVERAL distinct
        # conditions and a parking for one does NOT cover another.
        print(f"  CANDIDATES {label:25} ({len(hits)}) — verify the CONDITION matches, not just the probe")
        for tok, fn, i, line in hits[:3]:
            print(f"             via '{tok}' -> {fn}:{i}  {line}")
        return "parked"
    body = []
    for tok in toks:
        for f in files:
            for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                if tok.lower() in line.lower():
                    body.append((tok, f.name, i)); break
    if body:
        # "No heading" is NOT "nothing written" — material is often parked in a
        # BODY under a neighbouring heading.
        tok, fn, i = body[0]
        print(f"  NO HEADING {label:25} — but {len(body)} body mention(s), first "
              f"'{tok}' -> {fn}:{i}. READ before investigating")
        return "parked"
    print(f"  NONE FOUND {label:25} — no heading, no body mention; genuinely "
          f"untriaged, OR every token missed (try one from the warn text)")
    return "untriaged"


def main():
    # A CLAIM is the case the warn path structurally cannot see: "X is how this
    # system behaves" carries no probe name, so nothing pipes it here.
    if "--claim" in sys.argv:
        text = " ".join(sys.argv[sys.argv.index("--claim") + 1:])
        if not text.strip():
            print("--claim needs the sentence you are about to say"); return 2
        files = parking_files()
        if not files:
            print("PARKING FILES NOT FOUND — cannot answer; do NOT read this as 'nothing parked'")
            return 2
        print(f"searching {len(files)} parking file(s) for this claim's nouns\n")
        return {"untriaged": 0, "parked": 1, "no_tokens": 2}[report("", text, files)]

    blob = sys.stdin.read()
    warns = re.findall(r'[\u26a0\u267b]\s+(\S+)\s+(?:warn|stale)\s+(.*)', blob)
    if not warns:
        print("no warns on stdin (pipe health-check.py 2>&1 into this)"); return 2
    files = parking_files()
    if not files:
        print("PARKING FILES NOT FOUND — cannot answer; do NOT read this as 'nothing parked'")
        return 2
    print(f"searching {len(files)} parking file(s) for {len(warns)} warn(s)\n")
    verdicts = [(n, report(n, t, files)) for n, t in warns]
    untriaged = [n for n, v in verdicts if v == "untriaged"]
    unanswerable = [n for n, v in verdicts if v == "no_tokens"]
    print(f"\n{sum(1 for _, v in verdicts if v == 'parked')} with candidate parkings, "
          f"{len(untriaged)} with none"
          + (f": {', '.join(untriaged)}" if untriaged else "")
          + (f"; {len(unanswerable)} UNANSWERABLE (no searchable nouns): "
             f"{', '.join(unanswerable)}" if unanswerable else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
