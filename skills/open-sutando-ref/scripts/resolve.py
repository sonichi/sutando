#!/usr/bin/env python3
"""Resolve a fuzzy GitHub reference to a URL and optionally open it.

Usage:
    python3 resolve.py "#874"
    python3 resolve.py "PR 874"
    python3 resolve.py "issue 874"
    python3 resolve.py "the result-marker PR"      # fuzzy search
    python3 resolve.py --open "the multi-core issue"

Outputs the resolved URL to stdout. With --open, also navigates to it in
the default browser.

Repo is read from $SUTANDO_GH_REPO (e.g. "sonichi/sutando") or inferred
from `gh repo view --json nameWithOwner`.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import json


def get_repo() -> str:
    repo = os.environ.get("SUTANDO_GH_REPO", "")
    if repo:
        return repo
    try:
        out = subprocess.check_output(
            ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
            text=True, stderr=subprocess.DEVNULL,
            cwd=_find_repo_root(),
        ).strip()
        if out:
            return out
    except Exception:
        pass
    return "sonichi/sutando"


def _find_repo_root() -> str:
    # Walk up from script location looking for .git
    path = os.path.dirname(os.path.abspath(__file__))
    for _ in range(10):
        if os.path.isdir(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return os.getcwd()


def resolve_numeric(ref_type: str, number: int, repo: str) -> str:
    """Return the canonical GitHub URL for a numeric PR or issue."""
    kind = "pull" if ref_type == "pr" else "issues"
    return f"https://github.com/{repo}/{kind}/{number}"


def _gh_search(query: str, repo: str, kind: str) -> list[dict]:
    """Run gh pr/issue list --search and return JSON results."""
    try:
        out = subprocess.check_output(
            ["gh", kind, "list", "--repo", repo,
             "--search", query,
             "--limit", "5",
             "--json", "number,title,url,state"],
            text=True, stderr=subprocess.DEVNULL,
        )
        return json.loads(out)
    except Exception:
        return []


def fuzzy_resolve(query: str, repo: str) -> str | None:
    """Try to find the best match for a text query across PRs and issues."""
    # Search PRs first (more common in daily use), then issues
    for kind, label in [("pr", "PR"), ("issue", "issue")]:
        results = _gh_search(query, repo, kind)
        if results:
            # Pick the top result — gh's relevance ranking is decent
            top = results[0]
            return top["url"]
    return None


def parse_ref(arg: str) -> tuple[str, int | None, str | None]:
    """
    Parse the user's reference string.

    Returns (ref_type, number, fuzzy_query) where:
    - ref_type is "pr", "issue", or "fuzzy"
    - number is the int for numeric refs, else None
    - fuzzy_query is the text for fuzzy refs, else None
    """
    arg = arg.strip()

    # "#874" or "874"
    m = re.fullmatch(r"#?(\d+)", arg)
    if m:
        return ("numeric", int(m.group(1)), None)

    # "PR 874" / "PR#874" / "pull request 874"
    m = re.match(r"(?:PR|pull\s*request)\s*#?(\d+)", arg, re.IGNORECASE)
    if m:
        return ("pr", int(m.group(1)), None)

    # "issue 874" / "issue #874"
    m = re.match(r"issue\s*#?(\d+)", arg, re.IGNORECASE)
    if m:
        return ("issue", int(m.group(1)), None)

    # Everything else is a fuzzy query
    return ("fuzzy", None, arg)


def open_url(url: str) -> None:
    subprocess.run(["open", url], check=False)


def main() -> int:
    args = sys.argv[1:]
    do_open = "--open" in args
    args = [a for a in args if a != "--open"]

    if not args:
        print("Usage: resolve.py [--open] <ref>", file=sys.stderr)
        print("  ref: #874 | PR 874 | issue 874 | 'fuzzy description'", file=sys.stderr)
        return 1

    query = " ".join(args)
    repo = get_repo()

    ref_type, number, fuzzy_query = parse_ref(query)

    if ref_type == "numeric":
        # Ambiguous — try as PR first, fall back to issue
        # Check both and prefer whichever exists
        pr_url = resolve_numeric("pr", number, repo)
        issue_url = resolve_numeric("issue", number, repo)
        # Quick existence check via gh
        for kind, url in [("pr", pr_url), ("issue", issue_url)]:
            try:
                subprocess.check_call(
                    ["gh", kind, "view", str(number), "--repo", repo,
                     "--json", "number", "-q", ".number"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                print(url)
                if do_open:
                    open_url(url)
                return 0
            except subprocess.CalledProcessError:
                continue
        # Neither exists — default to issue URL and let the user see the 404
        print(issue_url)
        if do_open:
            open_url(issue_url)
        return 0

    if ref_type == "pr":
        url = resolve_numeric("pr", number, repo)
        print(url)
        if do_open:
            open_url(url)
        return 0

    if ref_type == "issue":
        url = resolve_numeric("issue", number, repo)
        print(url)
        if do_open:
            open_url(url)
        return 0

    # Fuzzy
    url = fuzzy_resolve(fuzzy_query, repo)
    if url:
        print(url)
        if do_open:
            open_url(url)
        return 0

    print(f"No match found for: {fuzzy_query!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
