#!/usr/bin/env python3
"""Validate a task-id echo, then publish a result and content-bound receipt.

The caller supplies both ids: this catches transposed ids, not prose that
semantically answers another task. The receipt records bytes, not meaning.
Publication uses result_publish under a per-task lock; readers must verify
raw bytes before normalizing text. Missing receipts are refused.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

try:
    from .file_lock import locked_file
    from .result_publish import publish_text
except ImportError:
    from file_lock import locked_file
    from result_publish import publish_text

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
    """Use one receipt name for bare and task-prefixed identifiers."""
    return Path(receipts_dir) / f"task-{str(task_id).removeprefix('task-')}.ok"


def receipt_body(task_id: str, body: str) -> str:
    """What a receipt attests: which task, and the exact bytes published for it.
    An empty receipt (every pre-upgrade one) attests neither."""
    return json.dumps({"task_id": task_id,
                       "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                       "bytes": len(body.encode("utf-8"))}, sort_keys=True) + "\n"


def history_receipts(receipts_dir, body):
    return Path(receipts_dir) / "generations" / hashlib.sha256(body.encode("utf-8")).hexdigest()


def record_pairing(receipts_dir, task_id, body, *, current=True):
    """Record proof before publication; the caller holds the task receipt lock."""
    proof = receipt_body(task_id, body)
    publish_text(receipt_path(history_receipts(receipts_dir, body), task_id), proof, _paired=True)
    if current:
        publish_text(receipt_path(receipts_dir, task_id), proof, _paired=True)


def receipt_attests(receipts_dir, task_id: str, body: str) -> bool:
    """True only when the receipt names THIS task and THESE bytes. Presence is
    not attestation: an empty or stale receipt answers False."""
    task_id = task_id_from(task_id)
    try:
        raw = receipt_path(receipts_dir, task_id).read_text()
        rec = json.loads(raw)
    except (OSError, ValueError):
        return False
    if not isinstance(rec, dict) or rec.get("task_id") != task_id:
        return False
    return (rec.get("sha256") == hashlib.sha256(body.encode("utf-8")).hexdigest()
            and rec.get("bytes") == len(body.encode("utf-8")))


def receipt_verifier(receipts_dir, task_id: str):
    """Require a receipt naming this task and these exact bytes."""
    return lambda raw_body: receipt_attests(receipts_dir, task_id, raw_body)


def result_root(path):
    """Locate the result namespace, including archive and claim paths."""
    p = Path(path).absolute()
    root = next((parent for parent in p.parents if parent.name == "results"), None)
    if root is None:
        archive = next((parent for parent in p.parents
                        if parent.name in {"archive", "undelivered", ".delivering"}
                        or parent.name.startswith("archive-")), None)
        root = archive.parent if archive else p.parent
    return root


def result_context(path):
    p = Path(path).absolute()
    root = result_root(p)
    name = p.name.split(".sending", 1)[0] if ".sending" in p.name else p.stem
    receipts = receipts_dir_for(root.parent) if root.name == "results" else root / ".result-pairing"
    return receipts, task_id_from(name)


def path_verifier(path):
    """Fail closed when the result path has no resolvable receipt namespace."""
    context = result_context(path)
    if context is None:
        return lambda body: False
    receipts, task_id = context
    ids = [task_id]
    if Path(path).absolute().parent != result_root(path):
        match = re.fullmatch(r"(.+)-[0-9]+(?:-late-duplicate)?(?:\.[0-9]+)?", task_id)
        if match:
            ids.append(match.group(1))
    archived = Path(path).absolute().parent != result_root(path)
    return lambda body: any(
        receipt_verifier(receipts, key)(body)
        or (archived and receipt_verifier(history_receipts(receipts, body), key)(body))
        for key in ids)


def has_pairing_receipt(receipts_dir, task_id: str) -> bool:
    try:
        return receipt_path(receipts_dir, task_id).is_file()
    except OSError:
        return False


def write_paired_result(results_dir, task_id: str, body: str,
                        receipts_dir=None, tmp_tag: str = "") -> Path:
    """Validate before any I/O, then serialize receipt and result publication.

    `tmp_tag` remains accepted for callers from older versions; the shared
    publisher always chooses a unique staging name. Receipt failure raises
    before replacing the result, rather than reporting a paired write.
    """
    task_id = task_id_from(task_id)
    rest = strip_pairing_echo(task_id, body)
    result = Path(results_dir) / f"task-{task_id}.txt"
    if receipts_dir is None:
        receipts_dir = result_context(result)[0]
    receipt = receipt_path(receipts_dir, task_id)
    # Keep both publications inside one per-task transaction. A failed receipt
    # publication prevents a result from becoming visible without its evidence.
    with locked_file(receipt.with_suffix(".lock")):
        record_pairing(receipts_dir, task_id, rest)
        return publish_text(result, rest, _paired=True)


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
        body = (Path(results) / f"task-{task_id_from(task_id)}.txt").read_bytes().decode("utf-8")
    except (OSError, ValueError) as e:
        print(f"result unreadable: {e}", file=sys.stderr)
        return 1
    return 0 if receipt_attests(receipts, task_id, body) else 1


def _read_cli(path):
    try:
        from delivery.readiness import read_ready_result
        body = read_ready_result(path)
    except (OSError, ValueError):
        return 1
    if body is None:
        return 1
    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "read":
        sys.exit(_read_cli(sys.argv[2]))
    if len(sys.argv) >= 2 and sys.argv[1] == "write":
        sys.exit(_write_cli(sys.argv[2:]))
    if len(sys.argv) >= 2 and sys.argv[1] == "attests":
        sys.exit(_attests_cli(sys.argv[2:]))
    print(USAGE, file=sys.stderr)
    sys.exit(2)
