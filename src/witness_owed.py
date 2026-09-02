#!/usr/bin/env python3
"""Durable record of a live-path witness a merged PR still owes.

REVIEW.md lesson 15 lets a live-path PR merge on harness proof only when no
host can produce its post-restart round trip. That deferral is a promise made
in a PR thread, which nothing on the deployment path reads. This module is the
record the deployment path reads: `self-upgrade` refuses to activate a head
that contains an owed PR until the record is closed with the witness, or the
activation is an explicit canary on the host that owes it.

Records live under <workspace>/state/witness-owed/<owner>-<repo>#<pr>.json and
move to closed/ once the witness is posted. Policy only: no transport, no
provider calls, no workspace resolution beyond the path handed in.
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


def records_dir(workspace: Path) -> Path:
    return Path(workspace) / "state" / "witness-owed"


def record_path(workspace: Path, repo: str, pr: int) -> Path:
    return records_dir(workspace) / f"{repo.replace('/', '-')}#{int(pr)}.json"


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
    path = record_path(workspace, repo, pr)
    _atomic_write(path, {"repo": repo, "pr": int(pr), "head": head, "host": host,
                         "reason": reason.strip(), "opened_by": opened_by,
                         "opened_at": _now(), "canary": None})
    return path


def list_open(workspace: Path) -> list[dict]:
    """Open records, malformed ones included as blocking: a record that
    cannot be read is not evidence the witness was posted."""
    out = []
    d = records_dir(workspace)
    if not d.is_dir():
        return out
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


def _is_ancestor(repo_root: Path, ancestor: str, ref: str) -> bool:
    r = subprocess.run(["git", "-C", str(repo_root), "merge-base", "--is-ancestor",
                        ancestor, ref], capture_output=True, text=True)
    if r.returncode in (0, 1):
        return r.returncode == 0
    # An unknown sha is not proof of absence; treat as contained (fail closed).
    return True


def blocking(workspace: Path, repo_root: Path, target_ref: str,
             current_ref: str | None = None, host: str | None = None) -> list[dict]:
    """Open records whose head is contained in target_ref and not already in
    current_ref. A canary record for THIS host does not block: that host is
    the one producing the witness, and it cannot do so without activating."""
    hits = []
    for rec in list_open(workspace):
        if rec.get("malformed"):
            hits.append(rec)
            continue
        if not _is_ancestor(repo_root, rec["head"], target_ref):
            continue
        if current_ref and _is_ancestor(repo_root, rec["head"], current_ref):
            continue
        if host and rec.get("canary") == host:
            continue
        hits.append(rec)
    return hits


def mark_canary(workspace: Path, repo: str, pr: int, host: str) -> Path:
    path = record_path(workspace, repo, pr)
    data = json.loads(path.read_text())
    data["canary"] = host
    data["canary_at"] = _now()
    _atomic_write(path, data)
    return path


def close_record(workspace: Path, repo: str, pr: int, witness: str) -> Path:
    """Retire the record with the witness's location; the closed copy keeps
    the audit trail beside the open ones."""
    if not isinstance(witness, str) or not witness.strip():
        raise ValueError("witness must name where the round trip was posted")
    path = record_path(workspace, repo, pr)
    data = json.loads(path.read_text())
    data["witness"] = witness.strip()
    data["closed_at"] = _now()
    closed = records_dir(workspace) / "closed" / path.name
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
    ap.add_argument("--workspace", required=True)
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
    ws = Path(a.workspace)
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
        repo, pr = _split(a.key); print(mark_canary(ws, repo, pr, a.host)); return 0
    if a.cmd == "close":
        repo, pr = _split(a.key); print(close_record(ws, repo, pr, a.witness)); return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
