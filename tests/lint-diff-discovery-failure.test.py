#!/usr/bin/env python3
"""A `--diff` lint must not exit 0 when its own file discovery failed.

The four `--diff` lints all discover their scan set the same way:

    files="$(git diff --name-only --diff-filter=AM "$base"...HEAD -- <pathspec>)"
    if [[ -z "$files" ]]; then echo "nothing to scan"; exit 0; fi

An empty `$files` is LEGITIMATE (a PR may touch nothing the lint cares about),
so the empty case has to stay a quiet pass. That is precisely why a *failed*
discovery is so dangerous here: it produces the same empty string, prints the
same reassuring line, and exits with the same 0. A required gate that reports
clean because it could not look is worse than no gate -- it occupies the slot
where a real check would go and answers with the same word.

Three of the four are already safe, and not by intent so much as by inheritance:
`set -euo pipefail` makes a failing command substitution in a plain assignment
abort the script. Measured, not assumed:

    files="$(git diff … bad-ref…HEAD)"            -> shell exits, rc 128/129
    files="$(git diff … bad-ref…HEAD || true)"    -> SURVIVES, files='', exit 0

`lint-hotkey-assertions.sh` carried that `|| true`, which defeats `set -e`
exactly here. Measured against `origin/main` before the fix:

    $ BASE_REF=refs/heads/no-such-ref-xyz bash scripts/lint-hotkey-assertions.sh --diff
    fatal: bad revision 'refs/heads/no-such-ref-xyz...HEAD'
    lint-hotkey-assertions: nothing to scan (mode=--diff)
    rc=0

The `fatal:` goes to stderr, above a green step nobody re-reads.

This suite pins the invariant for all four rather than the one file that was
broken, so the next `|| true` added for a quick fix fails a test instead of
silently disarming a gate. The three already-passing lints are not padding --
they are the positive control that the assertion can distinguish scripts, and
the HEAD-vs-bad-ref pair below is the control that it distinguishes *inputs*.

Run:  python3 tests/lint-diff-discovery-failure.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

LINTS = [
    "scripts/lint-hotkey-assertions.sh",
    "scripts/lint-workspace-resolution.sh",
    "scripts/lint-claude-home-path.sh",
    "scripts/lint-sutando-home-path.sh",
]

#: A ref that cannot resolve. `git diff <this>...HEAD` exits 128 with
#: `fatal: bad revision`, which is the shape a shallow checkout or a missing
#: remote-tracking branch produces in CI.
BAD_REF = "refs/heads/no-such-ref-for-lint-discovery-test"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)
        if detail:
            print(f"        {detail[:400]}")


def run(script: str, base: str):
    env = dict(os.environ)
    env["BASE_REF"] = base
    return subprocess.run(["bash", script, "--diff"], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=180)


print("--diff lint discovery-failure contract")

for script in LINTS:
    name = Path(script).stem
    check(f"{name}: the script exists", (REPO / script).is_file(), script)
    if not (REPO / script).is_file():
        continue

    # --- control: a RESOLVABLE base whose diff is legitimately empty ---------
    # `HEAD...HEAD` resolves fine and yields no files, so this is the exact
    # "nothing to scan, exit 0" path the failure case counterfeits. Without this
    # half, an assertion that every run exits non-zero would also pass.
    ok_run = run(script, "HEAD")
    check(f"{name}: control — a resolvable base with an empty diff exits 0",
          ok_run.returncode == 0,
          f"rc={ok_run.returncode} out={ok_run.stdout[-200:]} err={ok_run.stderr[-200:]}")

    # --- the regression -----------------------------------------------------
    bad_run = run(script, BAD_REF)
    check(f"{name}: an UNRESOLVABLE base does NOT exit 0",
          bad_run.returncode != 0,
          "exited 0 after git failed — a discovery that did not run reported clean.\n"
          f"        stdout={bad_run.stdout.strip()[:200]!r}\n"
          f"        stderr={bad_run.stderr.strip()[:200]!r}")

    # The sharpest statement of the defect: two opposite inputs, one output.
    check(f"{name}: ...so the two cases are DISTINGUISHABLE by exit code",
          ok_run.returncode != bad_run.returncode,
          f"both exited {ok_run.returncode} — a caller cannot tell 'clean' from 'never looked'")

print()
if failures:
    print(f"{len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
print("all checks passed — no --diff lint can report clean on a failed discovery")
