#!/usr/bin/env python3
"""The trusted core's image broker for a Signal Room answer: one fixed command, and worker text is only ever data.

Run by the TRUSTED CORE after the sandboxed delegate returns, with the ONE command the
task file states and nothing from the answer on the command line:

    python3 src/signal_image_broker.py --task-id <task-signal-…> < <the delegate's answer file>

The task id is server-authored — it comes from the task file, never from the worker —
and the whole answer arrives on stdin, where it stays data from end to end. The broker
applies the request protocol itself: the first MAX_IMAGE_REQUESTS well-formed standalone
`[generate-image: <prompt>]` lines, each prompt one line of at most
MAX_IMAGE_PROMPT_CHARS characters. For each it launches `signal_image_gen.py` with a
FIXED argv — `--task-id <id> --prompt <prompt>`, the prompt one element, no shell,
stdin closed — so a prompt is never a command, whatever it contains.

What the worker wrote can never become an attachment. BEFORE any request is honoured,
every line the shared marker parser (`result_markers.parse_markers`, the grammar the
egress guard executes) reads an attach action from is replaced by ATTACH_REMOVED_NOTE.
AFTER the wrapper runs, its printed marker is inserted only when it is exactly one
`[file: <root>/<name>]` line naming a bare `<name>` directly under the task's own root
(`signal_worker_launch.output_root_for`, the derivation the wrapper uses) whose realpath
sits under that root, that did not exist before the run, and that is a regular file
now; anything else becomes IMAGE_FAILED_NOTE. Finally the whole output is re-read with
the same parser, and if a live attach action survives that the wrapper did not issue
the answer is replaced by ATTACH_WITHHELD_NOTE.

Stdout is the brokered answer and is always safe to write as the result. Exit 2 means
the id was refused: nothing was generated and every request became IMAGE_FAILED_NOTE.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from result_markers import parse_markers  # noqa: E402
from signal_image_gen import valid_output_name  # noqa: E402

from signal_room_tasks import (  # noqa: E402
    IMAGE_FAILED_NOTE, IMAGE_REQUEST_RE, MAX_IMAGE_PROMPT_CHARS, MAX_IMAGE_REQUESTS)
from signal_worker_launch import LaunchRefused, output_root_for  # noqa: E402

WRAPPER = Path(__file__).resolve().with_name("signal_image_gen.py")
WRAPPER_TIMEOUT_SEC = 180
# Neither note contains `[`, so neither can ever parse as a marker.
ATTACH_REMOVED_NOTE = "(file marker removed: the host attaches only images it generated)"
ATTACH_WITHHELD_NOTE = "(answer withheld: it carried file markers the host did not issue)"


def neutralize_attachments(answer_text: str) -> str:
    """Every line the shared parser reads an attach action from — the same per-line
    test the egress allowance applies to a standalone marker — becomes the note."""
    return "\n".join(
        ATTACH_REMOVED_NOTE if any(a.kind == "attach" for a in parse_markers(line).actions) else line
        for line in answer_text.split("\n"))


def marker_path(marker) -> str | None:
    """The path of `marker` when, stripped, it is exactly one `[file: <path>]` line
    and nothing else — by the shared grammar, then rebuilt to pin that spelling."""
    if not isinstance(marker, str):
        return None
    line = marker.strip()
    parsed = parse_markers(line)
    if "\n" in line or parsed.body or len(parsed.actions) != 1 or parsed.actions[0].kind != "attach":
        return None
    path = parsed.actions[0].value
    return path if line == f"[file: {path}]" else None


def apply_generated_images(answer_text: str, task_id: str, runner) -> str:
    """The core-side step the block instructs, in code. Worker attachments are
    neutralized first; then each well-formed request line (the first
    MAX_IMAGE_REQUESTS only) becomes the `[file: …]` marker `runner(task_id, prompt)`
    returns, or IMAGE_FAILED_NOTE when it returns anything else or raises. Every other
    line, malformed and over-cap requests included, passes through unchanged. The
    output is then re-read whole: a live attach action nobody issued withholds it.
    """
    out, issued, used = [], [], 0
    for line in neutralize_attachments(answer_text).split("\n"):
        match = IMAGE_REQUEST_RE.match(line.strip())
        if match is None or used >= MAX_IMAGE_REQUESTS or len(match.group(1)) > MAX_IMAGE_PROMPT_CHARS:
            out.append(line)
            continue
        used += 1
        path = None
        try:
            path = marker_path(runner(task_id, match.group(1)))
        except Exception:  # noqa: BLE001 — any wrapper failure is "no image"
            pass
        if path is None:
            out.append(IMAGE_FAILED_NOTE)
        else:
            out.append(f"[file: {path}]")
            issued.append(path)
    result = "\n".join(out)
    live = [a.value for a in parse_markers(result).actions if a.kind == "attach"]
    return result if all(path in issued for path in live) else ATTACH_WITHHELD_NOTE


def fresh_marker(output: str, root: str, before) -> str | None:
    """The wrapper's stdout as the one marker the broker may insert: exactly
    `[file: <root>/<name>]`, a bare name not in `before`, realpath under the root,
    a regular file now. Anything else is None."""
    path = marker_path(output)
    if path is None:
        return None
    head, name = os.path.split(path)
    if head != root or not valid_output_name(name) or name in before:
        return None
    try:
        if not os.path.realpath(path).startswith(root + os.sep) or not stat.S_ISREG(os.lstat(path).st_mode):
            return None
    except OSError:
        return None
    return f"[file: {path}]"


def wrapper_runner(root: str, wrapper=None, timeout: float = WRAPPER_TIMEOUT_SEC):
    """A runner that launches the wrapper (WRAPPER unless given) with a fixed argv —
    the prompt is ONE element, never read by a shell — and accepts only a marker
    for a file the wrapper just created under `root`."""
    def run(task_id: str, prompt: str) -> str | None:
        before = set(os.listdir(root))
        proc = subprocess.run(
            [sys.executable, str(wrapper or WRAPPER), "--task-id", task_id, "--prompt", prompt],
            shell=False, stdin=subprocess.DEVNULL, capture_output=True,
            encoding="utf-8", errors="replace", timeout=timeout)
        if proc.returncode != 0:
            return None
        return fresh_marker(proc.stdout, root, before)
    return run


def broker(answer_text: str, task_id: str, tasks_dir, results_dir, runner=None) -> str:
    """The brokered answer for a verified task id; LaunchRefused for any other."""
    root = output_root_for(task_id, tasks_dir, results_dir)
    return apply_generated_images(answer_text, task_id, runner or wrapper_runner(root))


def run(argv, stdin, tasks_dir, results_dir, out=sys.stdout, err=sys.stderr, runner=None) -> int:
    parser = argparse.ArgumentParser(prog="signal_image_broker", add_help=True)
    parser.add_argument("--task-id", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0
    answer = stdin.read()
    try:
        out.write(broker(answer, args.task_id, tasks_dir, results_dir, runner))
    except LaunchRefused as exc:
        # Still a safe body on stdout: requests fail, worker markers go, nothing runs.
        print(f"signal_image_broker: refused: {exc}", file=err)
        out.write(apply_generated_images(answer, args.task_id, lambda task_id, prompt: None))
        return 2
    return 0


def main() -> int:
    from workspace_default import resolve_workspace
    # The same resolution agent-api's TASK_DIR / RESULT_DIR and the wrapper come from.
    workspace = resolve_workspace()
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8")
    return run(sys.argv[1:], sys.stdin, workspace / "tasks", workspace / "results")


if __name__ == "__main__":
    sys.exit(main())
