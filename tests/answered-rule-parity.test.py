#!/usr/bin/env python3
"""The watcher's shell `handler_result_is_answer` and task_dispatch's `_is_answer`
(the sweep plan) give the same verdict for every case in one shared table.

The shell function is lifted out of src/watch-tasks-stream.sh as written, and the
refusal wording comes from the script's own TERMINAL_REFUSAL_MARK.

Run: python3 tests/answered-rule-parity.test.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from delivery import task_dispatch as td  # noqa: E402

WATCHER = (REPO / "src" / "watch-tasks-stream.sh").read_text()
MARK = re.search(r'^TERMINAL_REFUSAL_MARK="([^"]*)"$', WATCHER, re.M).group(1)
FUNC = re.search(r"^handler_result_is_answer\(\) \{\n.*?^\}\n", WATCHER, re.M | re.S).group(0)
REFUSAL = f"I {MARK}".encode()
NAME = "task-parity.txt"

# (case, live result bytes or None or "dir", archived result bytes or None, expected answered)
CASES = [
    ("no result at all", None, None, False),
    ("plain answer", b"done\n", None, True),
    ("answer without a trailing newline", b"done", None, True),
    ("terminal refusal", REFUSAL + b" this: failed\n", None, False),
    ("terminal refusal without a trailing newline", REFUSAL + b" this: failed", None, False),
    ("refusal prefix and nothing else", REFUSAL, None, False),
    ("refusal with CRLF", REFUSAL + b"\r\n", None, False),
    ("answer that mentions the refusal mid-line", b"Done. Earlier " + REFUSAL + b" it.\n", None, True),
    ("refusal on the second line only", b"ok\n" + REFUSAL + b"\n", None, True),
    ("refusal after a bare CR on line one", b"ok\r" + REFUSAL + b"\n", None, True),
    ("refusal indented by a space", b" " + REFUSAL + b"\n", None, True),
    ("whitespace-only live placeholder", b"  \n", None, False),
    ("empty live placeholder", b"", None, False),
    ("archive-only answer", None, b"done\n", False),
    ("empty live placeholder, answer archived", b"", b"done\n", True),
    ("empty live placeholder, refusal archived", b"", REFUSAL + b" x\n", False),
    ("live answer beside an archived refusal", b"done\n", REFUSAL + b" x\n", True),
    ("non-UTF-8 bytes: not a READY body", b"\xff\xfe" + REFUSAL + b"\n", None, False),
    ("a directory at the live name", "dir", None, False),
]


def build(root: Path, live, archived) -> Path:
    results = root / "results"
    (results / "archive").mkdir(parents=True)
    if live == "dir":
        (results / NAME).mkdir()
    elif live is not None:
        (results / NAME).write_bytes(live)
    if archived is not None:
        (results / "archive" / f"{NAME[:-4]}-1790000000.txt").write_bytes(archived)
    return results


def shell_verdict(results: Path) -> bool:
    script = (f'RESULTS_DIR="{results}"; SUTANDO_PY_BIN="{sys.executable}"; __REPO_ROOT="{REPO}"\n'
              f'TERMINAL_REFUSAL_MARK="{MARK}"\n{FUNC}\nhandler_result_is_answer "{NAME}"')
    env = {k: v for k, v in os.environ.items() if not k.startswith("SUTANDO_")}
    return subprocess.run(["bash", "-c", script], env=env).returncode == 0


def python_verdict(results: Path) -> bool:
    return td._is_answer(results, NAME, set(os.listdir(results)), f"I {MARK}")


def main() -> int:
    bad = []
    for case, live, archived, expected in CASES:
        with tempfile.TemporaryDirectory() as d:
            results = build(Path(d), live, archived)
            sh, py = shell_verdict(results), python_verdict(results)
        ok = sh == py == expected
        print(("  ok   " if ok else "  FAIL ") + f"{case}: shell={sh} python={py} expected={expected}")
        if not ok:
            bad.append(case)
    print("\nPASS" if not bad else f"\nFAIL — {len(bad)} of {len(CASES)} case(s) disagree")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
