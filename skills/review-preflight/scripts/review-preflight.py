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
import os
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

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
    return Path(__file__).resolve().parents[3]  # skills/review-preflight/scripts/ -> repo


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


def resolve_repo(explicit: "str | None" = None, env: "dict | None" = None) -> str:
    """`--repo` > $SUTANDO_REVIEW_REPO > gh's `{owner}/{repo}` remote inference.

    Remote inference is LAST, not only: an app-pinned install has no `.git`, so
    `{owner}/{repo}` cannot resolve and the prior-art check silently degrades on
    every run. A hardcoded repo would fix that host and break every fork.
    """
    if explicit:
        return explicit
    environ = os.environ if env is None else env
    return environ.get("SUTANDO_REVIEW_REPO") or "{owner}/{repo}"


DECISIVE = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")


class PriorArt(NamedTuple):
    """Whether the check ran, and what it found — as separate fields, never one value.

    `checked=False` and an empty `items` are both falsy, so a single return
    value lets one `if not x` merge "unchecked" into "nothing there".
    """
    checked: bool
    items: "list[str]"
    verdicts: "list[str]"


def prior_art(pr: str, runner=None,
              repo: "str | None" = None) -> PriorArt:
    """(prose to read, decisive verdicts) already on the PR, oldest first.

    checked=False means COULD NOT CHECK, which is not the same as "nothing
    there" — a reviewer told nothing and a reviewer told the check failed
    behave differently, so the two must never render alike.

    `verdicts` is latest-state-per-login and is NOT subject to the
    display cap, so a verdict cannot be lost to prose or to truncation.
    """
    run = runner or (lambda a: subprocess.run(a, capture_output=True,
                                              text=True, timeout=20))
    out: "list[str]" = []
    latest: "dict[str, tuple[str, str]]" = {}
    for kind, path, when, verdict in (
            ("review", f"pulls/{pr}/reviews", "submitted_at", "state"),
            ("comment", f"issues/{pr}/comments", "created_at", None)):
        try:
            r = run(["gh", "api", f"repos/{resolve_repo(repo)}/" + path, "--paginate"])
        except (OSError, subprocess.SubprocessError):
            return PriorArt(False, [], [])
        if r.returncode != 0:
            return PriorArt(False, [], [])
        try:
            rows = json.loads(r.stdout) if r.stdout.strip() else []
        except ValueError:
            return PriorArt(False, [], [])
        for row in rows:
            state = row.get(verdict) if verdict else None
            who = (row.get("user") or {}).get("login", "?")
            ts = row.get(when, "?")
            # An empty body is not an empty verdict, and the newest verdict
            # wins by timestamp — arrival order is the endpoint's, not ours.
            if (state or "").upper() in DECISIVE and ts >= latest.get(who, ("",))[0]:
                latest[who] = (ts, state.upper())
            if not (row.get("body") or "").strip():
                continue
            label = kind if not state else f"{kind}, {state}"
            out.append(f"{ts}  {who} ({label})")
    return PriorArt(True, sorted(out), sorted(
        f"{ts}  {who}: {state}" for who, (ts, state) in latest.items()))


PRIOR_ART_SHOWN = 8


def _gh_json(run, args):
    """None on any failure: a call that did not answer must not read as an empty answer."""
    try:
        r = run(["gh", "api"] + args)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout) if r.stdout.strip() else None
    except ValueError:
        return None


def stale_approvals(pr: str, runner=None, repo: "str | None" = None) -> "list[dict] | None":
    """Approvals counted toward the gate whose reviewer has not seen the current head.

    A ruleset leaving dismiss_stale_reviews_on_push off keeps them green, so
    nothing on the PR page separates such an approval from a live one.
    """
    run = runner or (lambda a: subprocess.run(a, capture_output=True,
                                              text=True, timeout=20))
    base = f"repos/{resolve_repo(repo)}/"
    pull = _gh_json(run, [base + f"pulls/{pr}"])
    reviews = _gh_json(run, [base + f"pulls/{pr}/reviews", "--paginate"])
    commits = _gh_json(run, [base + f"pulls/{pr}/commits", "--paginate"])
    if pull is None or reviews is None or commits is None:
        return None
    head = (pull.get("head") or {}).get("sha") or ""
    if not head:
        return None
    # The PR's own commits, not a base compare: a base merge drags in main's
    # history, which was never part of what this reviewer read.
    shas = [c.get("sha", "") for c in commits]
    # COMMENTED never supersedes a verdict, so it must not overwrite the latest state.
    latest: "dict[str, dict]" = {}
    for row in reviews:
        if (row.get("state") or "").upper() not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        who = (row.get("user") or {}).get("login", "?")
        prev = latest.get(who)
        if prev is None or (row.get("submitted_at") or "") >= (prev.get("submitted_at") or ""):
            latest[who] = row
    out: "list[dict]" = []
    for who, row in sorted(latest.items()):
        if (row.get("state") or "").upper() != "APPROVED":
            continue
        when = row.get("submitted_at") or ""
        if not when:
            continue
        # Anchor on the review's own timestamp, never on commit_id: GitHub
        # re-points commit_id forward, so position cannot represent the review.
        after = [c for c in commits
                 if ((c.get("commit") or {}).get("committer") or {}).get("date", "") > when]
        if not after:
            continue
        content = [c for c in after if len(c.get("parents") or []) == 1]
        at = row.get("commit_id") or ""
        out.append({"user": who, "submitted_at": when, "commit_id": at, "head": head,
                    "since": len(after), "content": len(content),
                    "merges": len(after) - len(content),
                    "first_unseen": after[0].get("sha", ""),
                    "locatable": bool(at) and at in shas})
    return sorted(out, key=lambda r: r["submitted_at"])


def stale_approval_block(pr: str, rows: "list[dict] | None") -> "list[str]":
    """Render so "none stale" and "could not tell" can never read alike."""
    if rows is None:
        return ["STALE APPROVALS: *** COULD NOT CHECK *** — gh is unavailable or the call",
                "failed. Do not read this as none: compare each approval's timestamp with",
                "the commit dates yourself before treating the approvals gate as satisfied."]
    if not rows:
        return ["STALE APPROVALS: none — no commit on this PR postdates any counted approval."]
    out = ["STALE APPROVALS — these still COUNT toward the required-approvals gate, but a",
           "commit landed after each one, so the approver has not read the current head.",
           "CONTRIBUTING.md requires re-checking whether approvals still apply after any",
           "update or rebase — a merge commit carries a tree nobody reviewed either:"]
    for r in rows:
        kinds = []
        if r["content"]:
            kinds.append(f"{r['content']} content")
        if r["merges"]:
            kinds.append(f"{r['merges']} merge")
        detail = ", ".join(kinds) or f"{r['since']}"
        note = "" if r["locatable"] else "; its commit_id is not in the PR's commits (force-push)"
        base_only = r["merges"] and not r["content"] and r["locatable"]
        verdict = ("    -> base-only, approval still fits (merges carry no reviewed-tree"
                   " change; spot-check conflicts)" if base_only
                   else "    -> RE-READ before counting this approval")
        out += [f"  {r['user']}  APPROVED {r['submitted_at']}",
                f"    {r['since']} commit(s) since ({detail}), first unseen"
                f" {r['first_unseen'][:10]}, head {r['head'][:10]}{note}",
                verdict]
    return out

def verdict_block(art: PriorArt) -> "list[str]":
    """Every login's current decisive state, uncapped and prose-independent.

    Separate from the prose list because the two answer different questions:
    what must I read, versus where does this PR actually stand.
    """
    # Same tri-state as prior_art_block, for the same reason: an unchecked call
    # and a PR with no verdicts are both empty, and only `checked` tells them apart.
    state = "unchecked" if not art.checked else "none" if not art.verdicts else "found"
    if state == "unchecked":
        return ["DECISIVE STATE: *** COULD NOT CHECK *** — treat as unknown, not clean."]
    if state == "none":
        return ["DECISIVE STATE: none — no APPROVED or CHANGES_REQUESTED on record."]
    verdicts = art.verdicts
    return (["DECISIVE STATE (latest per login; a bare verdict carries no prose to read):"]
            + [f"  {v}" for v in verdicts])


def prior_art_block(pr: str, art: PriorArt,
                    repo: "str | None" = None) -> "list[str]":
    """Render prior art so "nothing there" can never read as "unchecked"."""
    # Branch on a distinct state, never on falsiness: equality cannot merge two
    # cases, so reordering these arms is a no-op rather than a silent bug.
    state = "unchecked" if not art.checked else "empty" if not art.items else "found"
    if state == "unchecked":
        # An unexpanded placeholder is the one COULD-NOT-CHECK with a fix the
        # reader can apply, so it must not read as generic gh flakiness.
        if (repo or resolve_repo()) == "{owner}/{repo}":
            return ["ALREADY ON THIS THREAD: *** COULD NOT CHECK *** — no repo context.",
                    "This install has no .git, so gh cannot expand {owner}/{repo}. Re-run",
                    "with --repo owner/name or set $SUTANDO_REVIEW_REPO; until then this",
                    "check is inert and every duplicate review passes it."]
        return ["ALREADY ON THIS THREAD: *** COULD NOT CHECK *** — gh is unavailable or",
                "the call failed. Read the thread yourself: an unchecked thread is not an",
                "empty one."]
    if state == "empty":
        return ["ALREADY ON THIS THREAD: nothing to read — no prose on the thread yet."]
    seen = art.items
    # Name the truncation: a bare count above a short list reads as the count
    # being wrong, not the list being cut.
    head = (f"ALREADY ON THIS THREAD — showing last {PRIOR_ART_SHOWN} of {len(seen)};"
            " read these before writing yours." if len(seen) > PRIOR_ART_SHOWN
            else f"ALREADY ON THIS THREAD ({len(seen)}) — read these before writing yours.")
    return ([head,
             "Findings hide in BOTH endpoints: an issue comment is not in the review",
             "list, and a COMMENTED review's body is not in the comments list:"]
            + [f"  {line}" for line in seen[-PRIOR_ART_SHOWN:]])


def render(guide: Path, pr: str | None, repo: "str | None" = None) -> str:
    text = guide.read_text(encoding="utf-8", errors="replace")
    lessons = extract_section(text, LESSONS_HEADING)
    checks = extract_section(text, CHECKS_HEADING)
    out = [f"review-preflight: criteria from {guide}", ""]
    if pr:
        out += [f"Reviewing PR #{pr}. Every lesson below is a criterion, not a suggestion.", ""]
        art = prior_art(pr, repo=repo)
        out += verdict_block(art) + prior_art_block(pr, art, repo=repo) + [""]
        out += stale_approval_block(pr, stale_approvals(pr, repo=repo)) + [""]
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
    ap.add_argument("--repo", help="owner/name for the prior-art lookup; "
                    "defaults to $SUTANDO_REVIEW_REPO, then the git remote")
    args = ap.parse_args(argv)

    guide = resolve_guide(args.guide)
    if not guide.is_file():
        print(f"review-preflight: guide not found at {guide}", file=sys.stderr)
        return 1
    try:
        print(render(guide, args.pr, args.repo))
    except OSError as exc:
        print(f"review-preflight: cannot read {guide}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
