#!/usr/bin/env python3
"""The eslint workflow's docs-only skip must never strand its required check.

`eslint over first-party TS + JS` is a required status check on `main`. A
workflow-level `paths:` filter would leave it permanently unreported, so the
skip has to live in step `if:` guards while the job itself always runs. These
checks execute the gate's real shell, extracted from the workflow, rather than
a restatement of it.
"""

import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "eslint.yml"
TEXT = WORKFLOW.read_text()

failures = []
checks_run = 0


def check(name: str, condition: bool) -> None:
    global checks_run
    checks_run += 1
    print(("  ok  " if condition else "  FAIL ") + name)
    if not condition:
        failures.append(name)


def scope_script():
    """The `run:` body of the gate step, dedented, with GH expressions bound."""
    lines = TEXT.splitlines()
    # Anchored on the gate step: grabbing another step's script would make
    # every behavioural check below vacuous rather than red.
    step = next(i for i, ln in enumerate(lines) if ln.strip() == "id: scope")
    start = next(i for i in range(step, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[start]) - len(lines[start].lstrip()) + 2
    body = []
    for ln in lines[start + 1:]:
        if ln.strip() and len(ln) - len(ln.lstrip()) < indent:
            break
        body.append(ln[indent:] if len(ln) >= indent else "")
    return "\n".join(body)


def run_gate(script, changed, event="pull_request"):
    """Run the gate with `gh pr diff` stubbed; return run=true / run=false."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        gh = tmp / "gh"
        if changed is None:
            gh.write_text("#!/bin/sh\nexit 1\n")
        else:
            gh.write_text("#!/bin/sh\ncat <<'EOF'\n" + changed + "\nEOF\n")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        out = tmp / "gh_output"
        out.touch()
        env = dict(os.environ)
        env["PATH"] = f"{tmp}:{env['PATH']}"
        env["GITHUB_OUTPUT"] = str(out)
        env["PR"] = "1"
        env["GH_TOKEN"] = "stub"
        subprocess.run(
            ["bash", "-c", script.replace("${{ github.event_name }}", event)],
            env=env, capture_output=True, text=True, timeout=30,
        )
        return out.read_text().strip()


check("workflow carries no `paths:` filter (required check must always report)",
      not re.search(r"^\s*paths(-ignore)?:", TEXT, re.M))

check("every npm step is gated on the scope output",
      TEXT.count("if: steps.scope.outputs.run == 'true'") == 3)

if shutil.which("bash"):
    script = scope_script()
    check("the extracted script is the gate itself",
          "GITHUB_OUTPUT" in script and "gh pr diff" in script and "run=false" in script)

    check("docs-only diff skips the lint",
          run_gate(script, "docs/README.md\ndocs/catalog.json\nCLAUDE.md") == "run=false")
    check("a .ts change lints",
          run_gate(script, "docs/README.md\nsrc/voice-agent.ts") == "run=true")
    check("a .mjs change lints",
          run_gate(script, "scripts/build-bundle.mjs") == "run=true")
    check("a lockfile change lints",
          run_gate(script, "package-lock.json") == "run=true")
    check("an edit to this workflow lints",
          run_gate(script, ".github/workflows/eslint.yml") == "run=true")
    # The two ways the probe can break must both fail toward doing the work,
    # never toward a silent pass.
    check("a failed `gh pr diff` lints rather than skipping",
          run_gate(script, None) == "run=true")
    check("an empty file list lints rather than skipping",
          run_gate(script, "") == "run=true")
    check("a push to main lints unconditionally",
          run_gate(script, "docs/README.md", event="push") == "run=true")
    # A path merely CONTAINING a lintable extension is not a lintable file.
    check("a .md path with a .ts substring does not force a lint",
          run_gate(script, "docs/rewrite.ts.notes.md") == "run=false")

if failures:
    print(f"Results: {len(failures)} failed, {checks_run - len(failures)} passed")
    raise SystemExit(1)

print(f"Results: {checks_run} passed")
