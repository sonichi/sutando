#!/usr/bin/env python3
"""Paired result write — the one validating writer for `results/task-<id>.txt`.

A session holding two tasks at once can compose a reply for A and write it into
B's result file; nothing downstream can tell, because a result file carries no
statement of which task it answers. Both replies then reach the wrong user. The
fix is to make the writer refuse a body that does not name its task: the body's
FIRST line must be exactly `task: <id>`, matching the id the caller was told to
answer. On mismatch nothing is written at all — not a partial file, not a temp
file. On success the echo line is stripped (users never see it) and the result
lands atomically under the canonical `task-<id>.txt` name.

This is the protocol layer, shared by every runtime's completion step: the
Claude entry (`skills/proactive-loop/SKILL.md`), the Codex entry
(`src/agent/codex/cli/task-notifier.sh`), and the pool follower. Runtimes differ
in how they learn about a task and what else completion entails (done-flags,
archiving); the pairing rule and the atomic write do not, so they live here once.

`write_paired_result` also drops a pairing receipt under
`state/result-pairing/`. That receipt is what lets an EXTERNAL process — one
holding the correct task id but not the body — distinguish "the agent completed
this task through the sanctioned path" from "a file with the right name
appeared". Without it, an after-the-fact reader cannot recover the pairing,
because the echo line is stripped before the body is written.

Task-file (read-side) schema lives in `src/local_task_protocol.py`; result-body
markers live in `src/result_markers.py`. Stdlib only, no network, no daemon.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

RECEIPTS_SUBPATH = ("state", "result-pairing")


def task_id_from(name: str) -> str:
    """Accept a task id, a task filename, or a result filename; return the id.

    Callers hold different spellings of the same thing — the notifier knows
    `task-123.txt`, the pool knows `123`. Normalizing here keeps ONE accepted
    spelling in the body (`task: 123`), so laxity about the argument never
    becomes laxity about the check.
    """
    value = str(name).strip()
    if not value:
        raise ValueError("empty task id")
    value = Path(value).name
    if value.endswith(".txt"):
        value = value[:-len(".txt")]
    if value.startswith("task-"):
        value = value[len("task-"):]
    if not value or "/" in value or value in (".", ".."):
        raise ValueError(f"unusable task id: {name!r}")
    return value


def strip_pairing_echo(task_id: str, body: str) -> str:
    """Return the body minus its `task: <id>` first line, or raise ValueError.

    Pure — it decides, it does not write. The caller must not write anything
    until this has returned.
    """
    if not body or not body.strip():
        raise ValueError("empty result body")
    first, _, rest = body.partition("\n")
    if first.rstrip("\r") != f"task: {task_id}":
        raise ValueError(
            f"pairing echo mismatch: need 'task: {task_id}' "
            f"as first line, got {first!r}")
    if not rest.strip():
        raise ValueError("result body is only the pairing echo line")
    return rest


def receipts_dir_for(workspace) -> Path:
    return Path(workspace).joinpath(*RECEIPTS_SUBPATH)


def receipt_path(receipts_dir, task_id: str) -> Path:
    """Accepts `abc` or `task-abc`. Writers hold the bare id; delivery consumers
    hold the prefixed one, and a lookup that misses fails OPEN — silently."""
    return Path(receipts_dir) / f"task-{str(task_id).removeprefix('task-')}.ok"


def receipt_body(task_id: str, body: str) -> str:
    """What a receipt attests: which task, and the exact bytes published for it.
    An empty receipt (every pre-upgrade one) attests neither."""
    return json.dumps({"task_id": task_id,
                       "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                       "bytes": len(body.encode("utf-8"))}, sort_keys=True) + "\n"


def receipt_attests(receipts_dir, task_id: str, body: str) -> bool:
    """True only when the receipt names THIS task and THESE bytes. Presence is
    not attestation: an empty or stale receipt answers False."""
    try:
        raw = receipt_path(receipts_dir, task_id).read_text()
        rec = json.loads(raw)
    except (OSError, ValueError):
        return False
    if not isinstance(rec, dict) or rec.get("task_id") != task_id:
        return False
    return rec.get("sha256") == hashlib.sha256(body.encode("utf-8")).hexdigest()


def receipt_verifier(receipts_dir, task_id: str):
    """A readiness predicate for delivery: enforce the digest once a receipt
    exists, and only then.

    A MISSING receipt is a pre-upgrade result, not a forgery. Requiring one
    unconditionally would strand every reply written before this shipped — an
    outage, in the name of a check. That makes this a transition window, not a
    permanent exemption: once no unreceipted results remain, the caller can
    drop straight to `receipt_attests`.
    """
    bare = str(task_id).removeprefix("task-")

    def _attests(raw_body: str) -> bool:
        if not has_pairing_receipt(receipts_dir, bare):
            return True
        return receipt_attests(receipts_dir, bare, raw_body)
    return _attests


def has_pairing_receipt(receipts_dir, task_id: str) -> bool:
    try:
        return receipt_path(receipts_dir, task_id).is_file()
    except OSError:
        return False


def write_paired_result(results_dir, task_id: str, body: str,
                        receipts_dir=None, tmp_tag: str = "") -> Path:
    """Validate the pairing echo, then write `results/task-<id>.txt` atomically.

    Refuses with ValueError and ZERO writes when the echo does not match.
    `tmp_tag` disambiguates the temp file when several writers share a results
    directory; it defaults to a unique token, because a SHARED temp name lets
    one writer's os.replace move the file the other is about to replace. The
    receipt is best-effort: a result already durable on disk must never be
    rolled back because a diagnostic sidecar could not be written.
    """
    task_id = task_id_from(task_id)
    rest = strip_pairing_echo(task_id, body)

    results = Path(results_dir)
    results.mkdir(parents=True, exist_ok=True)
    result = results / f"task-{task_id}.txt"
    suffix = f"-{tmp_tag}" if tmp_tag else f"-{os.getpid()}-{secrets.token_hex(8)}"
    tmp = results / f".task-{task_id}.txt.tmp{suffix}"
    tmp.write_text(rest)
    os.replace(tmp, result)

    if receipts_dir is not None:
        try:
            d = Path(receipts_dir)
            d.mkdir(parents=True, exist_ok=True)
            receipt_path(d, task_id).write_text(receipt_body(task_id, rest))
        except OSError as e:
            print(f"result_write: pairing receipt not written: {e}",
                  file=sys.stderr)
    return result


def _resolve_dirs(args):
    """(results_dir, receipts_dir) from explicit flags, else the workspace."""
    results = args.get("results-dir")
    receipts = args.get("receipts-dir")
    if results is None or receipts is None:
        workspace = args.get("workspace")
        if workspace is None:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from workspace_default import resolve_workspace
            workspace = str(resolve_workspace())
        if results is None:
            results = str(Path(workspace) / "results")
        if receipts is None:
            receipts = str(receipts_dir_for(workspace))
    return Path(results), Path(receipts)


USAGE = ("usage: result_write.py write <task-id> "
         "[--workspace DIR] [--results-dir DIR] [--receipts-dir DIR] "
         "[--tmp-tag TAG]   # result body on stdin")


def _write_cli(argv: "list[str]") -> int:
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    task_id, rest = argv[0], argv[1:]
    opts: "dict[str, str]" = {}
    while rest:
        flag = rest.pop(0)
        if not flag.startswith("--") or not rest:
            print(USAGE, file=sys.stderr)
            return 2
        opts[flag[2:]] = rest.pop(0)
    if set(opts) - {"workspace", "results-dir", "receipts-dir", "tmp-tag"}:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        results, receipts = _resolve_dirs(opts)
        result = write_paired_result(results, task_id, sys.stdin.read(),
                                     receipts_dir=receipts,
                                     tmp_tag=opts.get("tmp-tag", ""))
    except ValueError as e:
        print(f"result write refused: {e}", file=sys.stderr)
        return 2
    print(result)
    return 0


def _attests_cli(argv) -> int:
    """`attests <task_id> [--results-dir X --receipts-dir Y]` -> 0 when the
    receipt names this task AND the bytes currently in the result file.

    Exists so a shell caller can check ATTESTATION rather than presence: an
    empty or stale receipt is exactly what presence cannot tell apart.
    """
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    task_id, rest = argv[0], argv[1:]
    opts: "dict[str, str]" = {}
    while rest:
        flag = rest.pop(0)
        if not flag.startswith("--") or not rest:
            print(USAGE, file=sys.stderr)
            return 2
        opts[flag[2:]] = rest.pop(0)
    if set(opts) - {"workspace", "results-dir", "receipts-dir"}:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        results, receipts = _resolve_dirs(opts)
        body = (Path(results) / f"task-{task_id}.txt").read_text()
    except (OSError, ValueError) as e:
        print(f"result unreadable: {e}", file=sys.stderr)
        return 1
    return 0 if receipt_attests(receipts, task_id, body) else 1


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "write":
        sys.exit(_write_cli(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "attests":
        sys.exit(_attests_cli(sys.argv[2:]))
    print(USAGE, file=sys.stderr)
    sys.exit(2)
