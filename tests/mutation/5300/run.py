#!/usr/bin/env python3
"""Mutation controls for the strict review-namespace rule.

Runs in a throwaway export of HEAD (`git archive`), never in the checkout. A passing baseline
comes first; bytecode is disabled; a mutant whose run fails with a syntax or import error is
INVALID, never KILLED; the export is removed on exit. Exit 0 only when every control is killed.
Usage, from the repository root: python3 tests/mutation/5300/run.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CANONICAL = "src/policy/egress/result.py"
VENDORED = "packages/ag2-sparrow/ag2_sparrow/team_result_guard.py"
INFRA = ("SyntaxError", "IndentationError", "ImportError", "ModuleNotFoundError")


def export_head(destination: Path) -> None:
    repo = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=HERE, check=True,
                          capture_output=True, text=True).stdout.strip()
    archive = subprocess.run(["git", "archive", "HEAD"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(destination)], input=archive.stdout, check=True)


def run_tests(tree: Path, tests: list) -> list:
    env = {"HOME": os.environ.get("HOME", str(tree)), "PATH": os.environ.get("PATH", ""),
           "PYTHONDONTWRITEBYTECODE": "1"}
    results = []
    for test in tests:
        try:
            done = subprocess.run([sys.executable, test], cwd=tree, env=env, capture_output=True,
                                  text=True, timeout=900)
        except subprocess.TimeoutExpired:
            results.append((test, "timeout", "FAIL: the run hung past 900s", False))  # a hang is caught
            continue
        output = done.stdout + done.stderr
        first = next((line for line in output.splitlines() if line.startswith("FAIL")), "")
        results.append((test, done.returncode, first, any(word in output for word in INFRA)))
    return results


def main() -> int:
    controls = json.loads((HERE / "controls.json").read_text(encoding="utf-8"))
    tree = Path(tempfile.mkdtemp(prefix="mutation-5300-"))
    try:
        export_head(tree)
        baseline = [r for r in run_tests(tree, controls["tests"]) if r[1] != 0]
        if baseline:
            print(f"BASELINE FAILS: {[(test, rc) for test, rc, _f, _i in baseline]}")
            return 2
        print(f"BASELINE pass ({sys.version.split()[0]})")
        verdicts = []
        for control in controls["mutants"]:
            target = tree / control["file"]
            original = target.read_text(encoding="utf-8")
            if original.count(control["old"]) != 1:
                print(f"INVALID  {control['name']} | the mutated text is not unique")
                verdicts.append("INVALID")
                continue
            mutated = original.replace(control["old"], control["new"])
            target.write_text(mutated, encoding="utf-8")
            if control["file"] == CANONICAL:
                (tree / VENDORED).write_text(mutated, encoding="utf-8")
            try:
                failed = [r for r in run_tests(tree, controls["tests"]) if r[1] != 0]
            finally:
                target.write_text(original, encoding="utf-8")
                if control["file"] == CANONICAL:
                    (tree / VENDORED).write_text(original, encoding="utf-8")
            killed = [r for r in failed if not r[3]]
            if any(r[3] for r in failed):
                verdict = "INVALID"  # any syntax or import failure, even beside a behavioural one
            else:
                verdict = "KILLED" if killed else "SURVIVED"
            detail = f"{Path(killed[0][0]).name}: {killed[0][2] or 'exit ' + str(killed[0][1])}" \
                if killed else ""
            print(f"{verdict:8} {control['name']} | {detail[:140]}")
            verdicts.append(verdict)
        print(f"{verdicts.count('KILLED')}/{len(verdicts)} killed")
        return 0 if verdicts and all(v == "KILLED" for v in verdicts) else 1
    finally:
        shutil.rmtree(tree, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
