#!/usr/bin/env python3
"""Regression pin: the shell-lane classifier must serialize process-table suites.

`.github/workflows/ci.yml`'s "Run shell standalone tests" step splits suites into
parallel lanes and a serial tail, routing anything that reads the host process
table (pgrep/pkill/ps-by-pattern) to the tail — those suites see sibling lanes'
own watcher processes and misidentify them as their own otherwise. The original
regex covered `ps -ef`/`ps ax`/`ps -A`/`ps -e ` but not `ps -p`, so
`tests/watcher-sentinel-ownership.test.sh` and
`tests/startup-watcher-reaper-ownership.test.sh` — both of which watch a PID via
`ps -p "$pid"` — ran in the parallel pool and could collide with a sibling
lane's watcher (qingyun-wu's Codex seat, PR #4534 round 2).

This extracts the classifier's own grep pattern from ci.yml (never re-derives
it), so the test tracks the source rather than a copy of it.

Run: python3 tests/shell-ci-lane-classification.test.py
Exit code: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CI = REPO / ".github" / "workflows" / "ci.yml"

# The two suites known to read a PID's own process-table entry via `ps -p`.
KNOWN_PROCESS_TABLE_SUITES = [
    "tests/watcher-sentinel-ownership.test.sh",
    "tests/startup-watcher-reaper-ownership.test.sh",
]

# Mentions "ps" only in prose, never as a command: must stay parallel.
NEGATIVE_FIXTURE_BODY = "#!/usr/bin/env bash\n# corresponds to the process, not a literal ps call\necho ok\n"


def extract_pattern() -> str:
    """The grep -lE pattern the classifier step uses, verbatim from ci.yml."""
    text = CI.read_text()
    m = re.search(r"grep -lE '([^']*)'", text)
    if not m:
        raise AssertionError("could not find the classifier's grep -lE pattern in ci.yml")
    return m.group(1)


def classify(pattern: str, files: list[Path]) -> set[str]:
    """Run the real classifier command against real files, return the serial set."""
    out = subprocess.run(
        ["grep", "-lE", pattern, *[str(f) for f in files]],
        capture_output=True, text=True,
    )
    return {Path(p).name for p in out.stdout.splitlines()}


def case_known_suites_are_serial() -> list[str]:
    pattern = extract_pattern()
    files = [REPO / s for s in KNOWN_PROCESS_TABLE_SUITES]
    missing = [str(f) for f in files if not f.is_file()]
    if missing:
        return [f"fixture missing, cannot test: {missing}"]
    serial = classify(pattern, files)
    fails = []
    for s in KNOWN_PROCESS_TABLE_SUITES:
        name = Path(s).name
        if name not in serial:
            fails.append(f"{s} uses `ps -p` but the classifier did not route it to the serial tail")
    return fails


def case_negative_control_stays_parallel(tmp_path: Path) -> list[str]:
    pattern = extract_pattern()
    f = tmp_path / "no-process-table.test.sh"
    f.write_text(NEGATIVE_FIXTURE_BODY)
    serial = classify(pattern, [f])
    return [] if f.name not in serial else [
        "a suite with no pgrep/pkill/ps invocation was swept into the serial tail — pattern is too broad"
    ]


def main() -> int:
    import tempfile
    fails: list[str] = []
    fails += case_known_suites_are_serial()
    with tempfile.TemporaryDirectory() as td:
        fails += case_negative_control_stays_parallel(Path(td))

    if fails:
        print("FAIL:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("PASS: shell-lane classifier serializes both known process-table suites, "
          "leaves an unrelated suite parallel")
    return 0


if __name__ == "__main__":
    sys.exit(main())
