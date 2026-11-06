#!/usr/bin/env python3
"""One publisher for every `results/` body: staged beside the target, fsynced, renamed whole.

A result file is claimed on sight. `Path.write_text` and a shell `>` create the
name empty and fill it afterwards, so a drain woken by the creation reads a
zero-byte file or a prefix, delivers that, and archives the task as answered —
the real body written moments later is a "late duplicate" nobody sends (#3956).

Every in-repo writer of a result body publishes through here; adapters keep
only what to say and where. The staged name starts with a dot and ends in
`.tmp`, so no drain's `*.txt` glob can match it, and it is unique per call so
two publishers of one name never rename each other's bytes. Shell writers use
the CLI: `python3 src/result_publish.py <path>` with the body on stdin.
`--title`/`--outcome` publish the result's Activity labels first, bound to this body
(`result_labels`); a `[no-send]` reason is the outcome when none is given.

The CLI refuses a `source: cron` task's result that names no destination and
carries no skip marker: no bridge routes it, a `[channel:]` redirect included, so it would be archived unsent.
Owner-facing cron output goes through a channel-bound result instead (a task
with a destination header, or a proactive result routed to the owner's channel);
`--allow-undeliverable` publishes anyway. A skip-marked body for such a task is
published straight into the month's `results/archive/`: the same missing bridge is
the one that would otherwise retire it, so a live copy stays in `results/` forever.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

__all__ = ["STAGED_SUFFIX", "stage_text", "is_staged_for", "publish_staged", "publish_text",
           "undeliverable_reason", "retired_path", "result_labels_for", "main"]

STAGED_SUFFIX = ".tmp"


def stage_text(path: str | Path, text: str, mode: int = 0o666) -> Path:
    """Write `text` to a fresh sibling of `path` and fsync it; returns the staged file.

    `mode` is filtered by the umask, as `open()` would; pass 0o600 for a private record.

    Staging is separate from publishing because a sidecar the published file
    refers to may have to commit in between (the gateway's task media record).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL on a per-call name: honours the umask like a plain write would (mkstemp forces 0600).
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{secrets.token_hex(8)}{STAGED_SUFFIX}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def is_staged_for(name: str, target_name: str) -> bool:
    """True when `name` has the shape `stage_text` gives a staging of `target_name`."""
    prefix = f".{target_name}."
    if not (name.startswith(prefix) and name.endswith(STAGED_SUFFIX)):
        return False
    pid, _, token = name[len(prefix):-len(STAGED_SUFFIX)].partition(".")
    return (pid.isascii() and pid.isdigit() and len(token) == 16
            and all(c in "0123456789abcdef" for c in token))


def publish_staged(tmp: str | Path, path: str | Path, *, _paired=False, no_replace=False, pair_result=False) -> Path:
    """Rename a staged file onto `path` and fsync the directory, so the name survives a crash."""
    target = Path(path)
    if not _paired and target.suffix == ".txt" and (pair_result or any(
            p.name == "results" for p in target.absolute().parents)):
        try:
            from .result_write import result_context, receipt_path, locked_file, record_pairing
        except ImportError:
            from result_write import result_context, receipt_path, locked_file, record_pairing
        context = result_context(target)
        if context and target.suffix == ".txt":
            receipts, task_id = context
            receipt = receipt_path(receipts, task_id)
            body = Path(tmp).read_bytes().decode("utf-8")
            with locked_file(receipt.with_suffix(".lock")):
                if no_replace and target.exists():
                    raise FileExistsError(str(target))
                record_pairing(receipts, task_id, body, current=not any(p.name == "archive" or p.name.startswith("archive-") for p in target.parents))
                return publish_staged(tmp, target, _paired=True, no_replace=no_replace)
    if no_replace:
        os.link(tmp, target)
        os.unlink(tmp)
    else:
        os.replace(tmp, target)
    if os.name != "nt":
        dfd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    return target


def publish_text(path: str | Path, text: str, mode: int = 0o666, *, _paired=False, pair_result=False) -> Path:
    """Publish `text` at `path` whole: a reader sees the name absent or complete, never a prefix."""
    tmp = stage_text(path, text, mode)
    try:
        return publish_staged(tmp, path, _paired=_paired, pair_result=pair_result)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _helpers():
    try:
        from . import local_task_protocol, result_markers, task_archive, workspace_default
    except ImportError:
        import local_task_protocol
        import result_markers
        import task_archive
        import workspace_default
    return local_task_protocol, result_markers, task_archive, workspace_default


def _unrouted_cron_task(path: str | Path) -> str | None:
    """The task id when `path` is the live result of a `source: cron` task naming no destination.

    Fails open: an unknown name, a missing task file or a helper that will not load reads as routed."""
    try:
        protocol, _, archive, wsd = _helpers()
    except ImportError:
        return None
    target = Path(path).resolve()
    task_id = archive.task_id_from_filename(target.name)
    if task_id is None:
        return None
    try:
        workspace = wsd.resolve_workspace(migrate=False).resolve()
        if target.parent != workspace / "results":
            return None
        tasks_dir = workspace / "tasks"
        task_file = (archive.find_task_file(tasks_dir, task_id)
                     or protocol.find_archived_task(tasks_dir, task_id))
        if task_file is None:
            return None
        headers = protocol.parse_task_headers(task_file.read_text(encoding="utf-8")).headers
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if (headers.get("source") or "").strip().casefold() != "cron":
        return None
    if any((headers.get(k) or "").strip() for k in ("channel_id", "chat_id")):
        return None
    return task_id


def _skips_delivery(text: str) -> bool:
    markers = _helpers()[1]
    return markers.has_skip_action(markers.parse_markers(text).actions)


def undeliverable_reason(path: str | Path, text: str) -> str | None:
    """Why no bridge would deliver `text` published at `path`, or None when one would.

    Fails open: an unknown name, a missing task file or a helper that will not load publishes."""
    task_id = _unrouted_cron_task(path)
    # A [channel:] redirect does not count: no consumer reads a channel-less cron result to see it.
    if task_id is None or _skips_delivery(text):
        return None
    return f"{task_id} is a cron task with no channel_id or chat_id"


def retired_path(path: str | Path, text: str) -> Path | None:
    """Where a skip-marked body for an unrouted cron task is published instead of `path`, or None.

    No bridge visits such a result, so none would archive it after reading the skip; it goes
    straight into the month's `results/archive/`, which every result reader already walks."""
    task_id = _unrouted_cron_task(path)
    if task_id is None or not _skips_delivery(text):
        return None
    import time
    from datetime import datetime
    protocol = _helpers()[0]
    target = Path(path).resolve()
    dest = protocol.archive_month_dir(target.parent, datetime.now().isoformat()) / target.name
    # Never overwrite a record: an occupied slot takes the epoch-suffixed re-archive name readers
    # know, and the stamp is bumped past any neighbour taken within the same second.
    stem, stamp = dest.stem, int(time.time())
    while dest.exists():
        dest = dest.with_name(f"{stem}-{stamp}.txt")
        stamp += 1
    return dest


def _labels_module():
    try:
        from . import result_labels
    except ImportError:
        import result_labels
    return result_labels


def _no_send_reason(text: str) -> str:
    """What follows a leading `[no-send]`; empty for any other body."""
    try:
        _, markers, _, _ = _helpers()
    except ImportError:
        return ""
    if not any(a.kind == "skip" and a.value == "no-send" for a in markers.parse_markers(text).actions):
        return ""
    at = text.casefold().find("[no-send]")
    return text[at + len("[no-send]"):] if at >= 0 else ""


def result_labels_for(text: str, title=None, outcome=None) -> "tuple[dict, list]":
    """(labels, notes) for a body: each flag fitted to its limit, with a note when cut; a
    `[no-send]` body with no --outcome takes its reason as the outcome."""
    labels_mod = _labels_module()
    labels, notes = labels_mod.check(title, outcome)
    if "outcome" not in labels and not labels_mod.clean(outcome):
        reason = labels_mod.fit(_no_send_reason(text), labels_mod.OUTCOME_MAX)
        if reason:
            labels["outcome"] = reason
    return labels, notes


_USAGE = ("usage: result_publish.py [--allow-undeliverable] [--title TEXT] [--outcome TEXT] <path>"
          "   (body on stdin)")


def _parse(args: list[str]):
    """(path, allow, title, outcome), or None for a malformed command line."""
    allow, flags, paths = False, {}, []
    it = iter(args)
    for a in it:
        if a == "--allow-undeliverable":
            allow = True
        elif a in ("--title", "--outcome"):
            value = next(it, None)
            if value is None or a in flags:
                return None
            flags[a] = value
        elif a.startswith("-"):
            return None
        else:
            paths.append(a)
    if len(paths) != 1:
        return None
    return paths[0], allow, flags.get("--title"), flags.get("--outcome")


def main(argv: list[str]) -> int:
    parsed = _parse(argv[1:])
    if parsed is None:
        print(_USAGE, file=sys.stderr)
        return 2
    path, allow, title, outcome = parsed
    text = sys.stdin.read()
    reason = None if allow else undeliverable_reason(path, text)
    if reason:
        print(f"result_publish: refused, {reason}: nothing would deliver this body; send it "
              "as a channel-bound result (a task with a destination header, or a "
              "proactive result routed to the owner's channel), start it with [no-send], "
              "or pass --allow-undeliverable", file=sys.stderr)
        return 2
    dest = None if allow else retired_path(path, text)
    if dest is not None:
        # Nothing delivers a retired body, so it carries no Activity labels.
        publish_text(dest, text, pair_result=True)
        print(f"result_publish: published {Path(path).name} to {dest.parent} instead of results/: "
              "a cron task with no channel_id or chat_id has no bridge, so nothing would retire "
              "this skipped body from the live directory")
        return 0
    try:
        labels_mod = _labels_module()
    except ImportError:
        # Labels are optional: a copy without its label writer still publishes the result.
        if title is not None or outcome is not None:
            print("result_publish: warning: no result_labels module; published without "
                  "--title/--outcome", file=sys.stderr)
        publish_text(path, text, pair_result=True)
        return 0
    labels, notes = result_labels_for(text, title, outcome)
    for note in notes:
        print(f"result_publish: warning: {note}", file=sys.stderr)
    try:
        labels_mod.write(Path(path), labels, text)
    except Exception as exc:  # noqa: BLE001 — labels are optional; the result never waits on them
        print(f"result_publish: warning: published without --title/--outcome: {exc}",
              file=sys.stderr)
    publish_text(path, text, pair_result=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
