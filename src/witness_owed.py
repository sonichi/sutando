#!/usr/bin/env python3
"""Durable record of a live-path witness a merged PR still owes.

REVIEW.md lesson 15 lets a live-path PR merge on harness proof only when no
host can produce its post-restart round trip. That deferral is a promise made
in a PR thread, which nothing on the deployment path reads. This module is the
record the deployment path reads: `self-upgrade` refuses to activate a head
that contains an owed PR until the record is closed with the witness, or the
activation is an explicit canary on the host that owes it.

Records live under <workspace>/hosts/<host>/witness-owed/<owner>-<repo>#<pr>.json
— inside the per-host subtree the vault carries to every host — and move to
closed/ once the witness is posted. Readers scan every host's directory, so a
record opened on one host refuses activation on all of them. Policy only: no
transport, no provider calls, no workspace resolution beyond what is handed in.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


_RECORD_KEY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
FIELDS = ("repo", "pr", "head", "host", "reason", "opened_by", "opened_at")


def records_dir(workspace: Path, host: str) -> Path:
    """Where THIS host writes: the carried per-host subtree, never state/."""
    return Path(workspace) / "hosts" / host / "witness-owed"


def all_records_dirs(workspace: Path) -> list[Path]:
    """Every host's record directory present in this workspace."""
    hosts = Path(workspace) / "hosts"
    if not hosts.is_dir():
        return []
    return sorted(d / "witness-owed" for d in hosts.iterdir() if (d / "witness-owed").is_dir())


def record_path(workspace: Path, host: str, repo: str, pr: int) -> Path:
    return records_dir(workspace, host) / f"{repo.replace('/', '-')}#{int(pr)}.json"


def find_record(workspace: Path, repo: str, pr: int) -> Path | None:
    name = f"{repo.replace('/', '-')}#{int(pr)}.json"
    for d in all_records_dirs(workspace):
        if (d / name).is_file():
            return d / name
    return None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


def open_record(workspace: Path, repo: str, pr: int, head: str, host: str,
                reason: str, opened_by: str) -> Path:
    """Write the owed-witness record. Every field is required: a record that
    cannot say which head, which host, or why is not a deferral."""
    if not _RECORD_KEY.match(f"{repo}#{pr}"):
        raise ValueError(f"repo#pr must look like owner/name#N, got {repo}#{pr}")
    if not _SHA.match(head or ""):
        raise ValueError("head must be a commit sha")
    for name, value in (("host", host), ("reason", reason), ("opened_by", opened_by)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    path = record_path(workspace, host, repo, pr)
    _atomic_write(path, {"repo": repo, "pr": int(pr), "head": head, "host": host,
                         "reason": reason.strip(), "opened_by": opened_by,
                         "opened_at": _now(), "canary": None})
    return path


def list_open(workspace: Path) -> list[dict]:
    """Open records from EVERY host directory; malformed ones are included as
    blocking, since a record that cannot be read is not evidence the witness
    was posted."""
    out = []
    for d in all_records_dirs(workspace):
        for p in sorted(d.glob("*.json")):
            try:
                data = json.loads(p.read_text())
                if not isinstance(data, dict) or any(k not in data for k in FIELDS):
                    raise ValueError("missing fields")
            except (OSError, ValueError) as exc:
                data = {"repo": "?", "pr": 0, "head": "", "host": "?", "opened_by": "?",
                        "opened_at": "?", "reason": f"unreadable record {p.name}: {exc}",
                        "canary": None, "malformed": True}
            data["path"] = str(p)
            out.append(data)
    return out


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_root), *args],
                          capture_output=True, text=True)


def _is_ancestor(repo_root: Path, ancestor: str, ref: str) -> bool | None:
    """True / False, or None when Git could not answer. A head this clone has
    never fetched is the normal squash/rebase case, not an error: an absent
    object cannot be an ancestor, so that is False and the subject scan
    decides. A bad ref or a broken repository is None; callers fail closed."""
    if _git(repo_root, "cat-file", "-e", f"{ancestor}^{{commit}}").returncode != 0:
        if _git(repo_root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}").returncode != 0:
            return None
        return False
    r = _git(repo_root, "merge-base", "--is-ancestor", ancestor, ref)
    if r.returncode in (0, 1):
        return r.returncode == 0
    return None


_MERGE_SUBJECT = re.compile(r"\(#(\d+)\)\s*$")


def _contains(repo_root: Path, rec: dict, ref: str, since: str | None = None
              ) -> bool | None:
    """Does `ref` (optionally only the range since..ref) contain the owed PR?

    A merge commit keeps the PR head as an ancestor; a squash or rebase merge
    does not, so the PR is also recognised by its merge subject `(#N)` and by a
    commit body naming the recorded head. None when Git could not answer."""
    anc = _is_ancestor(repo_root, rec["head"], ref)
    if anc is None:
        return None
    if anc:
        if since is None:
            return True
        prior = _is_ancestor(repo_root, rec["head"], since)
        if prior is None:
            return None
        if not prior:
            return True
    rng = f"{since}..{ref}" if since else ref
    r = _git(repo_root, "log", "--format=%H%x1f%s%x1f%b%x1e", rng)
    if r.returncode != 0:
        return None
    pr = str(int(rec["pr"]))
    for entry in r.stdout.split("\x1e"):
        parts = entry.strip("\n").split("\x1f")
        if len(parts) < 2:
            continue
        subject = parts[1]
        body = parts[2] if len(parts) > 2 else ""
        m = _MERGE_SUBJECT.search(subject)
        if (m and m.group(1) == pr) or (rec["head"] and rec["head"] in body):
            return True
    return False


def blocking(workspace: Path, repo_root: Path, target_ref: str,
             current_ref: str | None = None, host: str | None = None) -> list[dict]:
    """Open records whose PR is contained in target_ref and not already in
    current_ref. A Git error on either question blocks (fail closed). A canary
    record releases only the host it names, and only when that is the host
    that owes the witness."""
    hits = []
    for rec in list_open(workspace):
        if rec.get("malformed"):
            hits.append(rec)
            continue
        verdict = _contains(repo_root, rec, target_ref, current_ref)
        if verdict is None:
            rec["reason"] = f"git could not answer for head {rec['head'][:8]}: {rec['reason']}"
            hits.append(rec)
            continue
        if not verdict:
            continue
        if host and rec.get("canary") == host and rec.get("host") == host:
            continue
        hits.append(rec)
    return hits


def mark_canary(workspace: Path, repo: str, pr: int, host: str) -> Path:
    """Declare `host` the canary. Only the host that owes the witness may:
    a record marked by any other host would release an activation the
    deferral never covered."""
    path = find_record(workspace, repo, pr)
    if path is None:
        raise FileNotFoundError(f"no open witness-owed record for {repo}#{pr}")
    data = json.loads(path.read_text())
    if data.get("host") != host:
        raise ValueError(f"{repo}#{pr} is owed by {data.get('host')!r}, not {host!r}")
    data["canary"] = host
    data["canary_at"] = _now()
    _atomic_write(path, data)
    return path


def close_record(workspace: Path, repo: str, pr: int, witness: str) -> Path:
    """Retire the record with the witness's location; the closed copy keeps
    the audit trail beside the open ones."""
    if not isinstance(witness, str) or not witness.strip():
        raise ValueError("witness must name where the round trip was posted")
    path = find_record(workspace, repo, pr)
    if path is None:
        raise FileNotFoundError(f"no open witness-owed record for {repo}#{pr}")
    data = json.loads(path.read_text())
    data["witness"] = witness.strip()
    data["closed_at"] = _now()
    closed = path.parent / "closed" / path.name
    _atomic_write(closed, data)
    path.unlink()
    return closed


def _split(key: str) -> tuple[str, int]:
    if not _RECORD_KEY.match(key):
        raise SystemExit(f"witness-owed: expected owner/name#N, got {key!r}")
    repo, pr = key.rsplit("#", 1)
    return repo, int(pr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="witness_owed")
    ap.add_argument("--workspace", help="defaults to the resolved Sutando workspace")
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open")
    o.add_argument("key"); o.add_argument("--head", required=True)
    o.add_argument("--host", required=True); o.add_argument("--reason", required=True)
    o.add_argument("--by", required=True)
    sub.add_parser("list")
    c = sub.add_parser("check")
    c.add_argument("--ref", required=True); c.add_argument("--current")
    c.add_argument("--repo-root", default="."); c.add_argument("--host")
    m = sub.add_parser("canary"); m.add_argument("key"); m.add_argument("--host", required=True)
    x = sub.add_parser("close"); x.add_argument("key"); x.add_argument("--witness", required=True)
    a = ap.parse_args(argv)
    if a.workspace:
        ws = Path(a.workspace)
    else:
        # Resolved lazily: with --workspace given, the helper must run in any
        # checkout, including one that ships without the resolver module.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from workspace_default import resolve_workspace
        ws = resolve_workspace(migrate=False)
    if a.cmd == "open":
        repo, pr = _split(a.key)
        print(open_record(ws, repo, pr, a.head, a.host, a.reason, a.by)); return 0
    if a.cmd == "list":
        for rec in list_open(ws):
            print(f"{rec['repo']}#{rec['pr']} head={rec['head'][:8]} host={rec['host']} "
                  f"canary={rec.get('canary')} reason={rec['reason']}")
        return 0
    if a.cmd == "check":
        hits = blocking(ws, Path(a.repo_root), a.ref, a.current, a.host)
        for rec in hits:
            print(f"witness owed: {rec['repo']}#{rec['pr']} head={rec['head'][:8]} "
                  f"host={rec['host']} — {rec['reason']}", file=sys.stderr)
        return 3 if hits else 0
    if a.cmd == "canary":
        repo, pr = _split(a.key)
        try:
            print(mark_canary(ws, repo, pr, a.host)); return 0
        except (FileNotFoundError, ValueError) as exc:
            print(f"witness-owed: {exc}", file=sys.stderr); return 5
    if a.cmd == "close":
        repo, pr = _split(a.key)
        try:
            print(close_record(ws, repo, pr, a.witness)); return 0
        except (FileNotFoundError, ValueError) as exc:
            print(f"witness-owed: {exc}", file=sys.stderr); return 5
    return 2


if __name__ == "__main__":
    sys.exit(main())
