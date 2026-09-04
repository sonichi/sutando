#!/usr/bin/env python3
"""Refuse to file a GitHub issue that duplicates one already open.

WHY: on 2026-09-04 I ran the duplicate search, its answer PRINTED the existing
issue, and I filed anyway — the search and the `gh issue create` were in one
command block, so nothing consumed the result. A check that does not GATE the
action is decoration. This exits non-zero so a caller can chain on it:

    python3 gh-duplicate-check.py --repo owner/name --title "..." && gh issue create ...

⚠ A PHRASE search cannot find the duplicate that matters. The pair that produced
this tool had titles sharing almost no wording: "CI: eslint job times out in npm
ci since ..." vs "eslint: 34 of 300 runs are killed by timeout-minutes: 5". Only
shared ENTITY tokens (`eslint`, `npm`, `timeout-minutes`) connect them, which is
why this scores token overlap rather than matching a string.

Tokenisation DELEGATES to warn-already-triaged.py so the two cannot drift; if it
cannot be imported this refuses (exit 2) rather than falling back to a private
copy, because a guard that tokenises differently from its sibling clears what the
sibling would catch.

exit 0 no candidate found · 1 candidate(s) found -> DO NOT FILE · 2 cannot answer
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path


def load_tokens():
    """Borrow `tokens()` from the sibling guard. Refuse if unavailable."""
    sib = Path(__file__).with_name("warn-already-triaged.py")
    if not sib.is_file():
        return None, None
    try:
        spec = importlib.util.spec_from_file_location("_wat", sib)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.tokens, getattr(mod, "STOP", set())
    except Exception:
        return None, None


def search(repo: str, term: str, timeout: float = 25.0):
    """One search query. Returns None on failure — never an empty list.

    The distinction is load-bearing: an empty list means "searched, found
    nothing", None means "did not search". Collapsing them is how a failed
    fetch becomes a green light.
    """
    q = f"repo:{repo} in:title {term}"
    try:
        r = subprocess.run(
            ["gh", "api", "-X", "GET", "search/issues",
             "-f", f"q={q}", "-f", "per_page=20"],
            capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None
    try:
        return json.loads(r.stdout).get("items") or []
    except Exception:
        return None


BARE_STOP = {
    "the","and","for","not","but","with","from","that","this","when","then","than","only",
    "into","over","under","after","before","since","because","while","which","what","where",
    "runs","run","job","jobs","are","was","were","has","have","had","its","it's","one","two",
    "all","any","its","new","old","fix","fixes","fixed","bug","issue","pr","ci","by","of","in",
    "on","at","to","is","as","a","an","it","we","i","my","our","you","your","killed","reported",
}


def bare_words(title: str, stop) -> list:
    """Bare lowercase nouns the ENTITY tokenizer cannot see.

    NOT a competing tokenizer — the entity tokens still come from the sibling.
    This ADDS the shape a GitHub title carries and a parking file does not: a
    plain product noun (`eslint`, `npm`) matches none of backticked /
    with-extension / hyphenated / snake_case, so the sibling returns it never.
    Measured: the duplicate that produced this tool shared ONLY bare words with
    its original, and the entity path alone scored it 0 and said PROCEED.
    """
    out = []
    for w in re.findall(r"[A-Za-z][A-Za-z0-9]{3,}", title):
        lw = w.lower()
        if lw in BARE_STOP or lw in stop:
            continue
        out.append(lw)
    seen, uniq = set(), []
    for w in out:
        if w not in seen:
            seen.add(w); uniq.append(w)
    return uniq


def score(item, toks) -> int:
    hay = ((item.get("title") or "") + " " + (item.get("body") or "")[:2000]).lower()
    return sum(1 for t in toks if t.lower() in hay)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", required=True, help="owner/name")
    ap.add_argument("--title", required=True, help="the title you are about to file")
    ap.add_argument("--min-overlap", type=int, default=2,
                    help="entity tokens shared with a candidate before it is reported")
    ap.add_argument("--max-queries", type=int, default=4)
    a = ap.parse_args(argv)

    # A decision parameter that can silence the search makes the gate fail OPEN
    # by construction -- `--max-queries 0` scored NO CANDIDATE, rc 0 (found in review).
    if a.max_queries < 1 or a.min_overlap < 1:
        print(f"CANNOT ANSWER: --max-queries={a.max_queries} --min-overlap={a.min_overlap}; "
              f"either below 1 makes a clean result arithmetic rather than evidence.",
              file=sys.stderr)
        return 2

    tokens, stop = load_tokens()
    if tokens is None:
        print("CANNOT ANSWER: warn-already-triaged.py is not importable, so tokenisation "
              "would have to be re-implemented here and could drift from it.", file=sys.stderr)
        return 2

    toks = list(tokens(None, a.title))
    for w in bare_words(a.title, stop):
        low = {t.lower() for t in toks}
        # Skip a word already CONTAINED in an entity token: `watch-tasks-stream`
        # then `watch`/`tasks`/`stream` spends the query budget re-asking itself.
        if w in low or any(w in t for t in low):
            continue
        toks.append(w)
    if not toks:
        print(f"CANNOT ANSWER: no entity tokens in {a.title!r}, so nothing would be "
              f"searched and a clean result would be produced by construction.", file=sys.stderr)
        return 2

    # A title yielding fewer distinct tokens than the threshold makes the MAXIMUM
    # attainable score unreachable, so rc 0 would be arithmetic, not evidence.
    if len(toks) < a.min_overlap:
        print(f"CANNOT ANSWER: {len(toks)} distinct token(s) in {a.title!r} but "
              f"--min-overlap={a.min_overlap}; no candidate could reach that score, so even an "
              f"exact duplicate would score clean. Lower --min-overlap or give a longer title.",
              file=sys.stderr)
        return 2

    seen, failures = {}, 0
    for t in toks[:a.max_queries]:
        items = search(a.repo, t)
        if items is None:
            failures += 1
            continue
        for it in items:
            seen[it["number"]] = it

    hits = sorted(((score(it, toks), it) for it in seen.values()),
                  key=lambda p: -p[0])
    hits = [(s, it) for s, it in hits if s >= a.min_overlap]

    used, dropped = toks[:a.max_queries], toks[a.max_queries:]
    print(f"searched {a.repo} on {len(used)} of {len(toks)} token(s): {', '.join(used)}")
    if dropped:
        # Naming them keeps the bound visible: a duplicate sharing ONLY a dropped
        # token scores 0, and token order is title order, not distinctiveness.
        print(f"  ⚠ NOT searched (--max-queries={a.max_queries}): {', '.join(dropped)}")
    if failures:
        print(f"  ⚠ {failures} query(ies) failed — coverage is partial")
    if not hits:
        if dropped:
            # The unsearched remainder can hold the ONLY qualifying overlap, and rc 0
            # chains straight into `gh issue create` — a printed warning gates nothing.
            print(f"CANNOT ANSWER: {len(dropped)} token(s) were never searched "
                  f"({', '.join(dropped)}) and nothing matched the {len(used)} that were. "
                  f"A duplicate sharing only a dropped token is invisible here. "
                  f"Re-run with --max-queries {len(toks)}.", file=sys.stderr)
            return 2
        if failures:
            # Partial coverage cannot produce a clean bill: the queries that did
            # not run are exactly where an unseen duplicate would be.
            print(f"CANNOT ANSWER: {failures} of {len(toks[:a.max_queries])} queries failed "
                  f"and the rest matched nothing, so this is 'not fully searched', "
                  f"not 'nothing is there'.", file=sys.stderr)
            return 2
        bound = (f" and {len(dropped)} token(s) were never searched"
                 if dropped else "")
        print(f"  NO CANDIDATE  — but that is 'no title matched these tokens'{bound}, "
              f"not proof of absence. A duplicate worded differently in the TITLE "
              f"is invisible to an in:title search.")
        return 0

    print(f"  DO NOT FILE — {len(hits)} candidate(s) share >= {a.min_overlap} tokens:")
    for s, it in hits[:5]:
        print(f"    #{it['number']} [{it['state']}] overlap={s}  {(it.get('title') or '')[:78]}")
        print(f"       {it['html_url']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
