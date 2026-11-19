#!/usr/bin/env python3
"""List the owner's open non-draft PRs in the configured repos and classify each one by the
next step it needs, so a shepherd pass acts on every PR instead of the ones it remembers.

  own_prs.py list   [--repo o/r ...] [--author LOGIN] [--email ADDR] [--json]
  own_prs.py record o/r#N CLASS --sha SHA

`list` prints one `PR` line per pull request and a closing `SUMMARY` line. It pages GitHub's
search API to the end (no `--limit` cut-off) and exits 1 when the listing is incomplete, so a
partial pass is never mistaken for a whole one. A PR is the owner's only when its last non-merge
commit is authored by the owner's email: a login several agents share does not make a PR ours.
`record` notes that CLASS was handled at SHA: the last non-merge commit, so a base merge does not
re-ask, or the head for the merge-base classes. `list` reports `acted=yes` until that commit
moves. A reviewer ask logged by notify_reviewers.py since the last non-merge commit counts too.

Settings (CLI > env > manifest `config`): OWN_PR_SHEPHERD_REPOS (comma or space separated;
required, empty means SKIP), OWN_PR_SHEPHERD_AUTHOR (empty means the signed-in gh login),
OWN_PR_SHEPHERD_EMAIL (empty means `git config user.email`; none means SKIP),
OWN_PR_SHEPHERD_MIN_QUOTA (stop when fewer GraphQL points remain).
State: <workspace>/state/own-pr-shepherd.json.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
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
RECENT_COMMITS = 30
DEFAULT_MIN_QUOTA = 500
ASK_CLASSES = {"needs_reviewer", "changes_addressed"}
HEAD_KEYED = {"conflicting", "behind", "ci_red"}  # the base moves these, so the same content can need them again

# Primary class first: the first applicable one decides `next`.
NEXT = {
    "queued": "none",
    "changes_requested": "fix",
    "changes_addressed": "re-solicit",
    "conflicting": "merge-base",
    "ci_red": "triage-ci",
    "behind": "merge-base",
    "approved": "ask-owner",
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
  recent: commits(last: %d) { nodes { commit { oid committedDate parents { totalCount }
    author { email } } } }
""" % RECENT_COMMITS

SEARCH = ("query($q: String!, $n: Int!, $after: String) { rateLimit { remaining resetAt } "
          "search(query: $q, type: ISSUE, "
          "first: $n, after: $after) { issueCount pageInfo { hasNextPage endCursor } "
          "nodes { ... on PullRequest { " + PR_FIELDS + " } } } }")


class QuotaLow(Exception):
    """GitHub's GraphQL budget fell below the floor; installs on one login share it."""


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


def search_prs(repo: str, author: str, gql=gh_graphql, min_quota: int = 0) -> tuple[list, bool, int]:
    """Every open non-draft PR by author in repo: (nodes, complete, total GitHub reported).
    Raises QuotaLow (carrying what was fetched) once fewer than min_quota points remain."""
    q = f"repo:{repo} is:pr is:open draft:false author:{author}"
    nodes, after, total = [], None, -1
    while True:
        page = data = None
        for n in PAGE_SIZES:
            try:
                data = gql(SEARCH, {"q": q, "n": n, "after": after})
                page = data["search"]
                break
            except (RuntimeError, ValueError, subprocess.TimeoutExpired, KeyError):
                continue
        if page is None:
            return nodes, False, total
        quota = (data.get("rateLimit") or {})
        if isinstance(quota.get("remaining"), int) and quota["remaining"] < min_quota:
            nodes += [p for p in page.get("nodes") or [] if p and not p.get("isDraft")]
            raise QuotaLow(quota["remaining"], quota.get("resetAt") or "?", nodes)
        total = page.get("issueCount", total)
        nodes += [p for p in page.get("nodes") or [] if p and not p.get("isDraft")]
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return nodes, len(nodes) >= total, total
        after = info.get("endCursor")


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


def _recent(pr: dict) -> list:
    return [(n or {}).get("commit") or {} for n in ((pr.get("recent") or {}).get("nodes") or [])]


def _is_merge(commit: dict) -> bool:
    return ((commit.get("parents") or {}).get("totalCount") or 0) >= 2


def last_author_email(pr: dict) -> str:
    """Author email of the last non-merge commit: whose work the branch is, as resolve_actor reads it."""
    for c in reversed(_recent(pr)):
        if not _is_merge(c):
            return ((c.get("author") or {}).get("email") or "").strip().lower()
    return ""


def content_commit(pr: dict) -> dict:
    """The last non-merge commit (the head when none is listed): what a base merge leaves unchanged."""
    for c in reversed(_recent(pr)):
        if not _is_merge(c):
            return c
    return _head_commit(pr)


def acted_sha(pr: dict, cls: str) -> str:
    """The commit an `acted` mark for cls is keyed on."""
    if cls in HEAD_KEYED:
        return pr.get("headRefOid") or ""
    return content_commit(pr).get("oid") or pr.get("headRefOid") or ""


def is_mine(pr: dict, email: str) -> bool:
    return bool(email) and last_author_email(pr) == email.strip().lower()


def content_since(review: dict, pr: dict) -> bool:
    """True when a non-merge commit landed after the review; a base merge alone addresses nothing."""
    commits = _recent(pr)
    oid = (review.get("commit") or {}).get("oid") or ""
    oids = [c.get("oid") for c in commits]
    if oid and oid in oids:
        later = commits[oids.index(oid) + 1:]
    else:
        at = review.get("submittedAt") or ""
        later = [c for c in commits if at and (c.get("committedDate") or "") > at]
    return any(not _is_merge(c) for c in later)


def classify(pr: dict) -> list:
    """Every class that applies, primary first (order of NEXT)."""
    if pr.get("isInMergeQueue"):
        return ["queued"]
    commit = _head_commit(pr)
    rollup = (commit.get("statusCheckRollup") or {}).get("state")
    me = ((pr.get("author") or {}).get("login") or "").lower()
    reviews = [r for r in ((pr.get("latestReviews") or {}).get("nodes") or [])
               if ((r.get("author") or {}).get("login") or "").lower() != me]
    decision, mergeable, mss = pr.get("reviewDecision"), pr.get("mergeable"), pr.get("mergeStateStatus")
    classes = []
    blocking = [r for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
    if decision == "CHANGES_REQUESTED" or blocking:
        addressed = bool(blocking) and all(content_since(r, pr) for r in blocking)
        classes.append("changes_addressed" if addressed else "changes_requested")
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


def next_step(cls: str) -> str:
    return NEXT[cls]


# --- acted record -----------------------------------------------------------------------------

def state_path() -> Path:
    from workspace_default import resolve_workspace
    return resolve_workspace() / "state" / "own-pr-shepherd.json"


def load_record(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(path: Path, key: str, cls: str, sha: str, now: Optional[float] = None) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Read-modify-write under one lock: concurrent records must not drop each other's entry.
    with open(path.with_name(path.name + ".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _record_locked(path, key, cls, sha, now)


def _record_locked(path: Path, key: str, cls: str, sha: str, now: Optional[float]) -> dict:
    data = load_record(path)
    entry = data.get(key) if isinstance(data.get(key), dict) else {}
    marks = entry.get("acted") if isinstance(entry.get("acted"), dict) else {}
    marks[cls] = sha
    entry = {"acted": marks, "at": int(now if now is not None else time.time())}
    data[key] = entry
    if len(data) > RECORD_CAP:
        keep = sorted(data, key=lambda k: data[k].get("at", 0), reverse=True)[:RECORD_CAP]
        data = {k: data[k] for k in keep}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".own-pr-shepherd.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)
    return data


def acted(rec: dict, key: str, cls: str, sha: str) -> bool:
    marks = (rec.get(key) or {}).get("acted")
    return bool(sha) and isinstance(marks, dict) and marks.get(cls) == sha


def _ledger_path() -> Optional[Path]:
    """notify_reviewers.py's ask ledger, from its own writer, or None where that skill is absent."""
    script = REPO_ROOT / "skills" / "collaboration-intelligence" / "scripts" / "notify_reviewers.py"
    try:
        spec = importlib.util.spec_from_file_location("_own_pr_notify_reviewers", script)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.ledger_path()
    except Exception:  # noqa: BLE001 - an unreadable ledger only means no shared asks are seen
        return None


def load_asks(path: Optional[Path]) -> dict:
    """{"o/r#N": latest ask ts} from the shared review-asks ledger."""
    out: dict = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path else []
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
            key, ts = f"{row['repo']}#{int(row['pr'])}", str(row.get("ts") or "")
        except (ValueError, KeyError, TypeError):
            continue
        out[key] = max(out.get(key, ""), ts)
    return out


def asked_since_content(asks: dict, key: str, cls: str, pr: dict) -> bool:
    """A reviewer ask any path logged after the last non-merge commit covers its ask class."""
    committed = content_commit(pr).get("committedDate") or ""
    return cls in ASK_CLASSES and bool(committed) and asks.get(key, "") >= committed


# --- CLI ------------------------------------------------------------------------------------

def configured_repos(cli: list) -> list:
    raw = " ".join(cli) if cli else config_value("OWN_PR_SHEPHERD_REPOS", MANIFEST)
    return [r for r in re.split(r"[\s,]+", raw) if re.fullmatch(r"[\w.-]+/[\w.-]+", r)]


def configured_email(cli: Optional[str], runner=subprocess.run) -> str:
    email = config_value("OWN_PR_SHEPHERD_EMAIL", MANIFEST, cli=cli)
    if email:
        return email.lower()
    try:
        out = runner(["git", "config", "user.email"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip().lower() if out.returncode == 0 else ""


def configured_min_quota() -> int:
    raw = config_value("OWN_PR_SHEPHERD_MIN_QUOTA", MANIFEST)
    return int(raw) if raw.isdigit() else DEFAULT_MIN_QUOTA


def configured_author(cli: Optional[str], gql=gh_graphql) -> str:
    author = config_value("OWN_PR_SHEPHERD_AUTHOR", MANIFEST, cli=cli)
    if author:
        return author
    return gql("query { viewer { login } }", {})["viewer"]["login"]


def cmd_list(args, gql=gh_graphql) -> int:
    repos = configured_repos(args.repo)
    if not repos:
        print("SKIP no repos configured (OWN_PR_SHEPHERD_REPOS)")
        return 0
    email = configured_email(args.email)
    if not email:
        print("SKIP no owner email (OWN_PR_SHEPHERD_EMAIL or git config user.email)")
        return 0
    try:
        author = configured_author(args.author, gql=gql)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired, OSError, KeyError) as e:
        print(f"SKIP gh is not signed in or unreachable: {e}")
        return 0
    rec, asks, min_quota = load_record(state_path()), load_asks(_ledger_path()), configured_min_quota()
    complete, rows, others, low = True, [], 0, False
    for repo in repos:
        try:
            nodes, whole, total = search_prs(repo, author, gql=gql, min_quota=min_quota)
        except QuotaLow as exc:
            remaining, reset, nodes = exc.args
            print(f"QUOTA_LOW remaining={remaining} floor={min_quota} reset={reset}")
            complete, whole, total, low = False, True, len(nodes), True
        if not whole:
            complete = False
            print(f"INCOMPLETE {repo} fetched={len(nodes)} reported={total}")
        for pr in nodes:
            if not is_mine(pr, email):
                others += 1
                continue
            classes = classify(pr)
            key, head, sha = f"{repo}#{pr['number']}", pr.get("headRefOid") or "", acted_sha(pr, classes[0])
            rows.append({"key": key, "url": pr.get("url"), "head": head, "base": pr.get("baseRefName"),
                         "sha": sha, "classes": classes, "next": next_step(classes[0]),
                         "failing": failing_checks(pr),
                         "acted": acted(rec, key, classes[0], sha)
                         or asked_since_content(asks, key, classes[0], pr)})
        if low:
            break
    todo = 0
    for r in rows:
        due = r["next"] != "none" and not r["acted"]
        todo += due
        if args.json:
            print(json.dumps(r, sort_keys=True))
            continue
        failing = f" failing={','.join(r['failing'])}" if r["failing"] else ""
        print(f"PR {r['key']} class={','.join(r['classes'])} next={r['next']} "
              f"acted={'yes' if r['acted'] else 'no'} head={r['head'][:8]} sha={r['sha']} "
              f"base={r['base']}"
              f"{failing} url={r['url']}")
    print(f"SUMMARY author={author} email={email} repos={len(repos)} prs={len(rows)} "
          f"not_mine={others} due={todo} complete={'yes' if complete else 'no'}")
    return 0 if complete else 1


def cmd_record(args) -> int:
    if args.cls not in NEXT or not re.fullmatch(r"[\w.-]+/[\w.-]+#\d+", args.pr):
        print("usage: record o/r#N CLASS --sha SHA", file=sys.stderr)
        return 2
    record(state_path(), args.pr, args.cls, args.sha)
    print(f"RECORDED {args.pr} {args.cls} sha={args.sha[:8]}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--repo", action="append", default=[])
    ls.add_argument("--author")
    ls.add_argument("--email")
    ls.add_argument("--json", action="store_true")
    rc = sub.add_parser("record")
    rc.add_argument("pr")
    rc.add_argument("cls")
    rc.add_argument("--sha", required=True)
    args = ap.parse_args(argv)
    return cmd_list(args) if args.cmd == "list" else cmd_record(args)


if __name__ == "__main__":
    sys.exit(main())
