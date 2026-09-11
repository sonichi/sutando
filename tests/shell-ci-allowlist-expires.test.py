#!/usr/bin/env python3
"""Regression pin: the shell-suite known-failures allowlist must expire itself.

`tests/shell-ci-known-failures.txt` stops a listed suite's failure from gating
CI. Nothing used to notice when a listed suite started passing, so an entry
outlived the failure it excused and left that suite un-gated — a real
regression in it would have been printed and then forgiven. Measured across 17
consecutive green CI runs on 12 branches: 5 of the 7 entries passed on
ubuntu-latest every time.

This runs the workflow's own loop body against fixtures, so it pins behaviour
rather than wording:

  a) unlisted + fails  → gates
  b) listed   + fails  → does not gate (the allowlist still works)
  c) listed   + passes → GATES, naming the stale entry
  d) unlisted + passes → does not gate
  e) every entry in the shipped allowlist names a file that exists

Run: python3 tests/shell-ci-allowlist-expires.test.py
Exit code: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CI = REPO / ".github" / "workflows" / "ci.yml"
ALLOWLIST = REPO / "tests" / "shell-ci-known-failures.txt"


def loop_body() -> str:
    """The `run:` script of the 'Run shell standalone tests' step, dedented."""
    text = CI.read_text()
    m = re.search(r"- name: Run shell standalone tests\n\s*run: \|\n(.*?)(?=\n {6}- name:|\n {2}\w|\Z)",
                  text, re.S)
    if not m:
        raise AssertionError("could not find the 'Run shell standalone tests' run: block in ci.yml")
    return textwrap.dedent(m.group(1))


def run_fixture(tmp: Path, suites: dict[str, bool], listed: list[str]) -> subprocess.CompletedProcess:
    """suites: {name: passes?}. listed: names written into the allowlist."""
    (tmp / "tests").mkdir(parents=True, exist_ok=True)
    for name, passes in suites.items():
        p = tmp / "tests" / name
        p.write_text("#!/usr/bin/env bash\n" + ("exit 0\n" if passes else "echo boom; exit 1\n"))
        p.chmod(0o755)
    (tmp / "tests" / "shell-ci-known-failures.txt").write_text(
        "".join(f"tests/{n}\n" for n in listed))

    # GNU `timeout` is absent on macOS; the workflow runs on ubuntu where it exists.
    shim = tmp / "bin"
    shim.mkdir(exist_ok=True)
    (shim / "timeout").write_text(
        '#!/usr/bin/env bash\n'
        'while [ "$1" = "-k" ]; do shift 2; done\n'   # drop -k <kill-after>
        'shift\n'                                      # drop the duration
        'exec "$@"\n')
    (shim / "timeout").chmod(0o755)

    env = dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}")
    return subprocess.run(["bash", "-c", loop_body()], cwd=tmp, env=env,
                          capture_output=True, text=True)


def case_a_unlisted_failure_gates() -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        r = run_fixture(Path(td), {"x.test.sh": False}, listed=[])
    return [] if r.returncode != 0 else ["a) an unlisted failing suite did not gate CI"]


def case_b_listed_failure_does_not_gate() -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        r = run_fixture(Path(td), {"x.test.sh": False}, listed=["x.test.sh"])
    fails = []
    if r.returncode != 0:
        fails.append(f"b) a listed failing suite gated CI (rc={r.returncode})")
    if "known failure" not in r.stdout:
        fails.append("b) the allowlisted-failure branch did not report itself")
    return fails


def case_c_listed_pass_gates_as_stale() -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        r = run_fixture(Path(td), {"x.test.sh": True}, listed=["x.test.sh"])
    fails = []
    if r.returncode == 0:
        fails.append("c) a listed suite that PASSED did not gate CI — the allowlist cannot expire")
    if "stale allowlist entry" not in r.stdout:
        fails.append("c) the stale entry was not named in the output")
    elif "x.test.sh" not in r.stdout:
        fails.append("c) the stale-entry message did not name the suite")
    return fails


def case_d_unlisted_pass_is_quiet() -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        r = run_fixture(Path(td), {"x.test.sh": True}, listed=[])
    fails = []
    if r.returncode != 0:
        fails.append(f"d) an ordinary passing suite gated CI (rc={r.returncode})")
    if "stale allowlist entry" in r.stdout:
        fails.append("d) an unlisted passing suite was reported as a stale entry")
    return fails


def case_e_entries_name_real_files() -> list[str]:
    fails = []
    for line in ALLOWLIST.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not (REPO / line).is_file():
            fails.append(f"e) allowlist names a missing file: {line}")
    return fails


def main() -> int:
    cases = [
        ("a", case_a_unlisted_failure_gates),
        ("b", case_b_listed_failure_does_not_gate),
        ("c", case_c_listed_pass_gates_as_stale),
        ("d", case_d_unlisted_pass_is_quiet),
        ("e", case_e_entries_name_real_files),
    ]
    all_failures: list[str] = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            all_failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if all_failures:
        print(f"\n{len(all_failures)} failure(s)")
        return 1
    print("\nThe known-failures allowlist gates correctly and expires itself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
