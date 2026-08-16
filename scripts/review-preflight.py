#!/usr/bin/env python3
"""Print the repo's review criteria before a human/agent reviews a PR.

`REVIEW.md` names three ways its lessons reach reviewers: this preflight,
`scripts/review-checks.sh` in CI, and the managed GitHub-App reviewer. The
latter two serve CI and the App — neither surfaces anything to an agent doing a
manual review, which is the case `CLAUDE.md` points at ("when you review,
`review-preflight.py` reads `REVIEW.md` and prints the criteria inline").

That script was never written: no commit in the repo's history touches the path,
while the prose describing it has shipped since #2281 (2026-07-25). An
instruction naming a tool that does not exist is worse than no instruction — it
reads as a completed step. This closes the gap #2281 documented.

Guide resolution mirrors `scripts/review-checks.sh` exactly, so the two can
never disagree about which file is authoritative: `--guide` wins, else
`<repo>/REVIEW.md`.

A missing or unreadable guide exits non-zero. A preflight that prints nothing
and succeeds is the failure mode this exists to prevent.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

LESSONS_HEADING = re.compile(r"^##\s+Lessons\b", re.I)
CHECKS_HEADING = re.compile(r"^##\s+Checks\b", re.I)
NEXT_H2 = re.compile(r"^##\s+")


def repo_root(start: Path | None = None) -> Path:
    """Repo root via git, falling back to this file's parent directory."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start or Path(__file__).resolve().parent),
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        if out:
            return Path(out)
    except Exception:
        pass
    return Path(__file__).resolve().parent.parent


def resolve_guide(explicit: str | None, root: Path | None = None) -> Path:
    """`--guide` wins, else <repo>/REVIEW.md — same order as review-checks.sh."""
    if explicit:
        return Path(explicit)
    return (root or repo_root()) / "REVIEW.md"


def extract_section(text: str, heading: re.Pattern) -> str:
    """Return one `##` section verbatim, heading included; '' when absent."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if heading.match(ln)), None)
    if start is None:
        return ""
    end = next((j for j in range(start + 1, len(lines)) if NEXT_H2.match(lines[j])),
               len(lines))
    return "\n".join(lines[start:end]).rstrip()


def count_check_patterns(checks_section: str) -> "tuple[int, int]":
    """(flag, allow) pattern counts from the machine-readable block."""
    flag = allow = None
    counts = {"flag": 0, "allow": 0}
    for raw in checks_section.splitlines():
        stripped = raw.strip()
        if re.match(r"^flag\s*:", stripped):
            flag, allow = True, False
            continue
        if re.match(r"^allow\s*:", stripped):
            flag, allow = False, True
            continue
        if stripped.startswith("- "):
            if flag:
                counts["flag"] += 1
            elif allow:
                counts["allow"] += 1
        elif stripped and not stripped.startswith("#") and not stripped.startswith("```"):
            flag = allow = False
    return counts["flag"], counts["allow"]


def render(guide: Path, pr: str | None) -> str:
    text = guide.read_text(encoding="utf-8", errors="replace")
    lessons = extract_section(text, LESSONS_HEADING)
    checks = extract_section(text, CHECKS_HEADING)
    out = [f"review-preflight: criteria from {guide}", ""]
    if pr:
        out += [f"Reviewing PR #{pr}. Every lesson below is a criterion, not a suggestion.", ""]
    out.append(lessons if lessons else
               "WARNING: no '## Lessons' section found — the guide's criteria could not be read.")
    n_flag, n_allow = count_check_patterns(checks)
    out += ["", "-" * 72,
            f"Machine-readable checks: {n_flag} flag pattern(s), {n_allow} allow pattern(s).",
            "CI runs these; it does NOT scan an arbitrary PR diff for you. To scan one:",
            "    gh pr diff <PR> > /tmp/pr.diff && bash scripts/review-checks.sh --diff /tmp/pr.diff",
            "Note: with no --diff the runner reads the diff from STDIN — and with",
            "nothing piped it exits 2, because an unscanned diff is not a pass."]
    return "\n".join(out)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Print review criteria before reviewing a PR.")
    ap.add_argument("pr", nargs="?", help="PR number, for the header line only")
    ap.add_argument("--guide", help="path to the review guide (default <repo>/REVIEW.md)")
    args = ap.parse_args(argv)

    guide = resolve_guide(args.guide)
    if not guide.is_file():
        print(f"review-preflight: guide not found at {guide}", file=sys.stderr)
        return 1
    try:
        print(render(guide, args.pr))
    except OSError as exc:
        print(f"review-preflight: cannot read {guide}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
