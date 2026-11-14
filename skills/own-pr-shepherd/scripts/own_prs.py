#!/usr/bin/env python3
"""List the owner's open non-draft PRs in the configured repos and classify each one by the
next step it needs, so a shepherd pass acts on every PR instead of the ones it remembers.

  own_prs.py list   [--repo o/r ...] [--author LOGIN] [--json]
  own_prs.py record o/r#N CLASS --head SHA

`list` prints one `PR` line per pull request and a closing `SUMMARY` line. It pages GitHub's
search API to the end (no `--limit` cut-off) and exits 1 when the listing is incomplete, so a
partial pass is never mistaken for a whole one. `record` notes that CLASS was handled at head
SHA; `list` then reports `acted=yes` for it until the head moves.

Settings (CLI > env > manifest `config`): OWN_PR_SHEPHERD_REPOS (comma or space separated;
empty means the repo of the current checkout), OWN_PR_SHEPHERD_AUTHOR (empty means the signed-in
gh login). State: <workspace>/state/own-pr-shepherd.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

SKILL_DIR = Path(__file__).resolve().parents[1]  # lint-workspace-resolution: allow-repo-root
REPO_ROOT = SKILL_DIR.parents[1]  # lint-workspace-resolution: allow-repo-root
sys.path.insert(0, str(REPO_ROOT / "src"))

from skill_config import config_value  # noqa: E402

MANIFEST = SKILL_DIR / "manifest.json"
PAGE_SIZES = (20, 10, 5)  # a large page of check rollups times out (HTTP 504); retry smaller
FAILED_RUNS = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
RECORD_CAP = 500

# Primary class first: the first applicable one decides `next`.
NEXT = {
    "queued": "none",
    "changes_requested": "fix",
    "changes_addressed": "re-solicit",
    "conflicting": "merge-base",
    "ci_red": "triage-ci",
    "behind": "merge-base",
    "approved": "enqueue",          # becomes ask-owner where the base has no merge queue
    "approved_ci_pending": "none",
    "needs_reviewer": "request-review",
    "unknown": "recheck",
    "waiting_review": "none",
}

PR_FIELDS = """
  number url isDraft isInMergeQueue headRefOid headRefName baseRefName
  reviewDecision mergeable mergeStateStatus author { login }
  reviewRequests(first: 1) { totalCount }
  latestReviews(first: 30) { nodes { state submittedAt author { login } commit { oid } } }
  commits(last: 1) { nodes { commit { oid committedDate statusCheckRollup { state
    contexts(first: 100) { nodes { __typename
      ... on CheckRun { name conclusion }
      ... on StatusContext { context state } } } } } } }
"""

SEARCH = ("query($q: String!, $n: Int!, $after: String) { search(query: $q, type: ISSUE, "
          "first: $n, after: $after) { issueCount pageInfo { hasNextPage endCursor } "
          "nodes { ... on PullRequest { " + PR_FIELDS + " } } } }")

QUEUE = ("query($owner: String!, $name: String!, $branch: String!) { repository(owner: $owner, "
         "name: $name) { mergeQueue(branch: $branch) { id } } }")


def gh_graphql(query: str, variables: dict, runner=subprocess.run) -> dict:
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is None:
            continue
        cmd += (["-F", f"{k}={v}"] if isinstance(v, int) else ["-f", f"{k}={v}"])
    out = runner(cmd, capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[:300] or "gh api graphql failed")
    data = json.loads(out.stdout)
    if data.get("errors"):
        raise RuntimeError(json.dumps(data["errors"])[:300])
    return data["data"]


def search_prs(repo: str, author: str, gql=gh_graphql) -> tuple[list, bool, int]:
    """Every open non-draft PR by author in repo: (nodes, complete, total GitHub reported)."""
    q = f"repo:{repo} is:pr is:open draft:false author:{author}"
    nodes, after, total = [], None, -1
    while True:
        page = None
        for n in PAGE_SIZES:
            try:
                page = gql(SEARCH, {"q": q, "n": n, "after": after})["search"]
                break
            except (RuntimeError, ValueError, subprocess.TimeoutExpired, KeyError):
                continue
        if page is None:
            return nodes, False, total
        total = page.get("issueCount", total)
        nodes += [p for p in page.get("nodes") or [] if p and not p.get("isDraft")]
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return nodes, len(nodes) >= total, total
        after = info.get("endCursor")


def has_merge_queue(repo: str, branch: str, gql=gh_graphql) -> Optional[bool]:
    owner, name = repo.split("/", 1)
    try:
        return gql(QUEUE, {"owner": owner, "name": name, "branch": branch})["repository"]["mergeQueue"] is not None
    except (RuntimeError, ValueError, subprocess.TimeoutExpired, KeyError, TypeError):
        return None


def _head_commit(pr: dict) -> dict:
    nodes = ((pr.get("commits") or {}).get("nodes") or [])
    return (nodes[0] or {}).get("commit") or {} if nodes else {}


def failing_checks(pr: dict) -> list:
    rollup = _head_commit(pr).get("statusCheckRollup") or {}
    out = []
    for c in ((rollup.get("contexts") or {}).get("nodes") or []):
        if c.get("__typename") == "CheckRun" and c.get("conclusion") in FAILED_RUNS:
            out.append(c.get("name") or "?")
        elif c.get("__typename") == "StatusContext" and c.get("state") in ("FAILURE", "ERROR"):
            out.append(c.get("context") or "?")
    return out


def _on_head(review: dict, head: str, committed: str) -> bool:
    oid = (review.get("commit") or {}).get("oid") or ""
    if oid:
        return oid == head
    return bool(committed) and (review.get("submittedAt") or "") >= committed


def classify(pr: dict) -> list:
    """Every class that applies, primary first (order of NEXT)."""
    if pr.get("isInMergeQueue"):
        return ["queued"]
    head = pr.get("headRefOid") or ""
    commit = _head_commit(pr)
    committed = commit.get("committedDate") or ""
    rollup = (commit.get("statusCheckRollup") or {}).get("state")
    me = ((pr.get("author") or {}).get("login") or "").lower()
    reviews = [r for r in ((pr.get("latestReviews") or {}).get("nodes") or [])
               if ((r.get("author") or {}).get("login") or "").lower() != me]
    decision, mergeable, mss = pr.get("reviewDecision"), pr.get("mergeable"), pr.get("mergeStateStatus")
    classes = []
    blocking = [r for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
    if decision == "CHANGES_REQUESTED" or blocking:
        fresh = any(_on_head(r, head, committed) for r in blocking) or not blocking
        classes.append("changes_requested" if fresh else "changes_addressed")
    if mergeable == "CONFLICTING" or mss == "DIRTY":
        classes.append("conflicting")
    if failing_checks(pr) or rollup in ("FAILURE", "ERROR"):
        classes.append("ci_red")
    if mss == "BEHIND":
        classes.append("behind")
    if not classes and decision == "APPROVED" and mergeable == "MERGEABLE":
        classes.append("approved_ci_pending" if rollup in ("PENDING", "EXPECTED") else "approved")
    if not classes and decision != "APPROVED":
        if not (pr.get("reviewRequests") or {}).get("totalCount") and not reviews:
            classes.append("needs_reviewer")
    if not classes and mergeable == "UNKNOWN":
        classes.append("unknown")
    return classes or ["waiting_review"]


def next_step(cls: str, queue: Optional[bool]) -> str:
    if cls == "approved":
        return "enqueue" if queue else "ask-owner"
    return NEXT[cls]


# --- acted-at-head record -------------------------------------------------------------------

def state_path() -> Path:
    from workspace_default import resolve_workspace
    return resolve_workspace() / "state" / "own-pr-shepherd.json"


def load_record(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(path: Path, key: str, cls: str, head: str, now: Optional[float] = None) -> dict:
    data = load_record(path)
    entry = data.get(key) if isinstance(data.get(key), dict) else {}
    if entry.get("head") != head:
        entry = {"head": head, "acted": []}
    if cls not in entry["acted"]:
        entry["acted"].append(cls)
    entry["at"] = int(now if now is not None else time.time())
    data[key] = entry
    if len(data) > RECORD_CAP:
        keep = sorted(data, key=lambda k: data[k].get("at", 0), reverse=True)[:RECORD_CAP]
        data = {k: data[k] for k in keep}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".own-pr-shepherd.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return data


def acted(rec: dict, key: str, cls: str, head: str) -> bool:
    entry = rec.get(key) or {}
    return entry.get("head") == head and cls in (entry.get("acted") or [])


# --- CLI ------------------------------------------------------------------------------------

def configured_repos(cli: list) -> list:
    raw = " ".join(cli) if cli else config_value("OWN_PR_SHEPHERD_REPOS", MANIFEST)
    repos = [r for r in re.split(r"[\s,]+", raw) if r]
    if repos:
        return repos
    out = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                         capture_output=True, text=True, timeout=30)
    return [out.stdout.strip()] if out.returncode == 0 and out.stdout.strip() else []


def configured_author(cli: Optional[str]) -> str:
    author = config_value("OWN_PR_SHEPHERD_AUTHOR", MANIFEST, cli=cli)
    if author:
        return author
    return gh_graphql("query { viewer { login } }", {})["viewer"]["login"]


def cmd_list(args) -> int:
    try:
        author = configured_author(args.author)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError) as e:
        print(f"SKIP gh is not signed in or unreachable: {e}")
        return 0
    repos = configured_repos(args.repo)
    if not repos:
        print("SKIP no repos configured (OWN_PR_SHEPHERD_REPOS) and no GitHub repo at the cwd")
        return 0
    rec = load_record(state_path())
    complete, rows = True, []
    for repo in repos:
        nodes, whole, total = search_prs(repo, author)
        if not whole:
            complete = False
            print(f"INCOMPLETE {repo} fetched={len(nodes)} reported={total}")
        queues = {b: has_merge_queue(repo, b) for b in {n.get("baseRefName") or "main" for n in nodes}}
        for pr in nodes:
            queue = queues.get(pr.get("baseRefName") or "main")
            classes = classify(pr)
            key, head = f"{repo}#{pr['number']}", pr.get("headRefOid") or ""
            rows.append({"key": key, "url": pr.get("url"), "head": head, "base": pr.get("baseRefName"),
                         "classes": classes, "next": next_step(classes[0], queue),
                         "merge_queue": queue, "failing": failing_checks(pr),
                         "acted": acted(rec, key, classes[0], head)})
    todo = 0
    for r in rows:
        due = r["next"] != "none" and not r["acted"]
        todo += due
        if args.json:
            print(json.dumps(r, sort_keys=True))
            continue
        q = {True: "yes", False: "no", None: "unknown"}[r["merge_queue"]]
        failing = f" failing={','.join(r['failing'])}" if r["failing"] else ""
        print(f"PR {r['key']} class={','.join(r['classes'])} next={r['next']} "
              f"acted={'yes' if r['acted'] else 'no'} head={r['head'][:8]} base={r['base']} "
              f"queue={q}{failing} url={r['url']}")
    print(f"SUMMARY author={author} repos={len(repos)} prs={len(rows)} due={todo} "
          f"complete={'yes' if complete else 'no'}")
    return 0 if complete else 1


def cmd_record(args) -> int:
    if args.cls not in NEXT or not re.fullmatch(r"[\w.-]+/[\w.-]+#\d+", args.pr):
        print("usage: record o/r#N CLASS --head SHA", file=sys.stderr)
        return 2
    record(state_path(), args.pr, args.cls, args.head)
    print(f"RECORDED {args.pr} {args.cls} head={args.head[:8]}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--repo", action="append", default=[])
    ls.add_argument("--author")
    ls.add_argument("--json", action="store_true")
    rc = sub.add_parser("record")
    rc.add_argument("pr")
    rc.add_argument("cls")
    rc.add_argument("--head", required=True)
    args = ap.parse_args(argv)
    return cmd_list(args) if args.cmd == "list" else cmd_record(args)


if __name__ == "__main__":
    sys.exit(main())
