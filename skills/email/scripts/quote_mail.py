#!/usr/bin/env python3
"""The email playbook's only writer of its result: checks, quotes and publishes the owner's note.

Anything quoted from a mail is untrusted, and a `[file: ...]`, `[channel: ...]` or `[dm-only]`
in a subject would otherwise be acted on by the delivering bridge. The grammar's own inverse,
`result_markers.neutralize_markers`, does the quoting, and `result_publish` writes the file, so
a note cannot reach `results/` unquoted by following the playbook.

Usage:
  quote_mail.py check   --task-file <task>                 # prints `deliver` or `skip: <why>`
  quote_mail.py publish --task-file <task> <result path>   # note on stdin
  quote_mail.py skip    --task-file <task> <result path>   # publishes `[no-send]`

`check` exits 0 for `deliver`, 3 for `skip`. `publish` re-runs the check and publishes
`[no-send]` instead of the note when the mail was already delivered.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # lint-workspace-resolution: allow-repo-root
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))
from local_task_protocol import parse_task_headers  # noqa: E402
from result_markers import neutralize_markers  # noqa: E402

PUBLISHER = REPO / "src" / "result_publish.py"
SKIP_BODY = "[no-send]"
NO_DISPLAY_NAME = ("The mail carries no sender display name, so I could not check it against "
                   "the address.")
_FROM_RE = re.compile(r"^Email from[ \t]+(.*?)[ \t]*$")
_NAMED_RE = re.compile(r"^(.*?)[ \t]*<[^<>\s]+>$")


def quote(text: str) -> str:
    return neutralize_markers(text)


def trigger_id(task_text: str) -> str | None:
    """The task's own `agent_trigger_id`, read only from its header block."""
    value = parse_task_headers(task_text).get("agent_trigger_id")
    return value.strip() if value and value.strip() else None


def display_name(task_text: str) -> str | None:
    """The display name on the body's `Email from` line; None when it carries a bare address."""
    for line in parse_task_headers(task_text).body.split("\n"):
        m = _FROM_RE.match(line)
        if m:
            named = _NAMED_RE.match(m.group(1))
            name = named.group(1).strip().strip('"').strip() if named else ""
            return name or None
    return None


def delivered_before(task_file: Path) -> Path | None:
    """An archived task, other than this one, whose header block names the same trigger id."""
    tid = trigger_id(task_file.read_text(encoding="utf-8", errors="replace"))
    archive = task_file.parent / "archive"
    if tid is None or not archive.is_dir():
        return None
    for path in sorted(archive.rglob("*.txt")):
        if path.name == task_file.name:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if trigger_id(text) == tid:
            return path
    return None


def skip_reason(task_file: Path) -> str | None:
    earlier = delivered_before(task_file)
    return f"already delivered as {earlier.name}" if earlier else None


def note_for(task_text: str, note: str) -> str:
    """The note as published: quoted, with the missing display name stated, not skipped."""
    body = note.rstrip("\n")
    if display_name(task_text) is None and NO_DISPLAY_NAME not in body:
        body = f"{body}\n{NO_DISPLAY_NAME}"
    return quote(body) + "\n"


def publish(result: str, text: str, extra: list[str]) -> int:
    return subprocess.run([sys.executable, str(PUBLISHER), *extra, result], input=text,
                          text=True).returncode


def _labels(args) -> list[str]:
    out: list[str] = []
    for flag in ("title", "outcome", "parent"):
        value = getattr(args, flag, None)
        if value is not None:
            out += [f"--{flag}", value]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "publish", "skip"):
        p = sub.add_parser(name)
        p.add_argument("--task-file", required=True, type=Path)
        if name != "check":
            p.add_argument("result")
            for flag in ("--title", "--outcome", "--parent"):
                p.add_argument(flag)
    args = ap.parse_args(argv)
    task_text = args.task_file.read_text(encoding="utf-8", errors="replace")
    reason = skip_reason(args.task_file)
    if args.cmd == "check":
        print(f"skip: {reason}" if reason else "deliver")
        return 3 if reason else 0
    if args.cmd == "skip" or reason:
        if reason:
            print(f"quote_mail: {reason}; published {SKIP_BODY}", file=sys.stderr)
        return publish(args.result, SKIP_BODY + "\n", _labels(args))
    note = sys.stdin.read()
    if not note.strip():
        print("quote_mail: empty note on stdin; nothing published", file=sys.stderr)
        return 2
    return publish(args.result, note_for(task_text, note), _labels(args))


if __name__ == "__main__":
    raise SystemExit(main())
