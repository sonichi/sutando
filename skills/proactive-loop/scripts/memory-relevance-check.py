#!/usr/bin/env python3
"""Force consultation of a relevant memory BEFORE a choice, not after it recurs.

WHY THIS EXISTS (Chi, 2026-09-11, event-mining room): "the expected behavior is
the system gets auto improved from all my interactions w/ the system." Recording
already happens automatically-ish -- almost every correction becomes a
`feedback_*.md` memory file. What was missing is APPLICATION: nothing forced
this session to consult `feedback_own_defaults_are_not_decisions.md` at the
exact moment it repeated that pattern on the refresh-button design, three
correction rounds later, despite the memory already being on file. A memory
loaded once at session start is passive; this makes the check active, at the
one moment it can still change what happens next.

Same shape as `warn-already-triaged.py` (extract, then match across a fixed
corpus) applied to a different question: that script asks "is this ALREADY
triaged", this one asks "have I ALREADY been corrected on this" -- but a naive
port of its word-matching (first version of this script, kept as a lesson, not
deleted) treated any shared common word as a hit and called 31 of 32 files
relevant to everything -- a detector that always fires is not a detector.
Fixed by scoring on DISCRIMINATIVE tokens only: a token counts only if it
appears in at most a quarter of the corpus (document frequency), the same
reason tf-idf downweights "the" -- a word every file uses tells you nothing
about which one matches THIS claim. Verified on the actual case it exists for:
the refresh-button claim below now surfaces exactly the one directly-on-point
file first, not a same-sized slice of the whole corpus.

  python3 memory-relevance-check.py --claim "shipping a UI-facing feature with no explicit spec for one behavior"

Output: RELEVANT (memory file + matching line + the tokens that made it match)
or NONE FOUND. A RELEVANT hit is a pointer to re-read before proceeding, not a
verdict already applied -- reading the snippet is not the same as reading the
file, and reading the file is not the same as changing the decision.

exit 0 nothing relevant found · 1 relevant memory found -- read it before proceeding · 2 cannot answer
"""
from __future__ import annotations

import pathlib
import re
import sys

MIN_LEN = 5
MAX_DOC_FRACTION = 0.25  # a token in more than this share of files is too common to discriminate
MIN_SHARED_TOKENS = 3
SHOW = 8


def memory_files():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src"))
    from util_paths import memory_dir
    mem = memory_dir()
    if not mem.is_dir():
        return []
    return sorted(p for p in mem.glob("feedback_*.md") if p.is_file())


def words(text):
    """Lowercased alnum/hyphen/underscore tokens, length >= MIN_LEN. No hand-listed
    stopwords: document frequency below does that job, and does it without a list
    someone has to remember to extend."""
    return {w for w in re.findall(r"[a-z][a-z0-9_-]{%d,}" % (MIN_LEN - 1), text.lower())}


def main():
    if "--claim" not in sys.argv:
        print('usage: memory-relevance-check.py --claim "<what you are about to do>"')
        return 2
    text = " ".join(sys.argv[sys.argv.index("--claim") + 1:]).strip()
    if not text:
        print("--claim needs a sentence describing the action about to be taken")
        return 2

    files = memory_files()
    if not files:
        print("MEMORY DIR NOT FOUND — cannot answer; do NOT read this as 'nothing relevant'")
        return 2

    claim_tokens = words(text)
    if not claim_tokens:
        print("NO NOUNS — extracted 0 searchable tokens from the claim; cannot answer")
        return 2

    # Document frequency across the corpus, so commonness is measured, not guessed.
    file_words = {}
    for f in files:
        try:
            file_words[f] = words(f.read_text(errors="ignore"))
        except OSError:
            file_words[f] = set()
    doc_freq = {t: sum(1 for w in file_words.values() if t in w) for t in claim_tokens}
    max_docs = max(1, int(len(files) * MAX_DOC_FRACTION))
    discriminative = {t for t, n in doc_freq.items() if 0 < n <= max_docs}

    if not discriminative:
        common = sorted(claim_tokens)[:SHOW]
        print(f"NO DISCRIMINATIVE TOKENS — every token in the claim appears in more than "
              f"{max_docs} of {len(files)} memory files (checked: {', '.join(common)}"
              f"{'…' if len(claim_tokens) > SHOW else ''}). Cannot answer; re-state with a "
              f"more specific term (a name, a file, a quoted phrase).")
        return 2

    print(f"searching {len(files)} feedback memory file(s) for "
          f"{len(discriminative)} discriminative token(s) of {len(claim_tokens)} total\n")

    hits = []
    for f, fw in file_words.items():
        matched = discriminative & fw
        if len(matched) >= MIN_SHARED_TOKENS:
            hits.append((f.name, matched))

    if not hits:
        print(f"NONE FOUND — no feedback memory shares {MIN_SHARED_TOKENS}+ discriminative "
              f"tokens with this claim (discriminative set: {', '.join(sorted(discriminative))})")
        return 0

    # Rarer shared tokens are stronger evidence; rank by their total rarity, not just count.
    hits.sort(key=lambda h: (-len(h[1]), sum(doc_freq[t] for t in h[1])))
    print(f"RELEVANT — {len(hits)} feedback memory file(s) share specific vocabulary with "
          f"this claim. READ before proceeding, not just noted:")
    for name, matched in hits[:SHOW]:
        ordered = sorted(matched, key=lambda t: doc_freq[t])
        print(f"  memory/{name}  (shared: {', '.join(ordered)})")
    if len(hits) > SHOW:
        print(f"  +{len(hits) - SHOW} further file(s) not shown — narrow the claim to see them")
    return 1


if __name__ == "__main__":
    sys.exit(main())
