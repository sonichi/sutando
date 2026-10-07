#!/usr/bin/env python3
"""`eslint over first-party TS + JS` is a REQUIRED check: a `paths:` filter would
leave it unreported, so the skip lives in step guards and the job always runs."""

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
BASH = shutil.which("bash")
if os.name == "nt" and (git := shutil.which("git")):
    git_bash = Path(git).resolve().parent.parent / "bin" / "bash.exe"
    if git_bash.is_file():
        BASH = str(git_bash)

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
        with gh.open("w", encoding="utf-8", newline="\n") as stub:
            stub.write("#!/bin/sh\nexit 1\n" if changed is None
                       else "#!/bin/sh\ncat <<'EOF'\n" + changed + "\nEOF\n")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
        out = tmp / "gh_output"
        out.touch()
        env = dict(os.environ)
        env["PATH"] = f"{tmp}{os.pathsep}{env['PATH']}"
        env["GITHUB_OUTPUT"] = str(out)
        env["PR"] = "1"
        env["GH_TOKEN"] = "stub"
        subprocess.run(
            [BASH, "-c", script.replace("${{ github.event_name }}", event)],
            env=env, capture_output=True, text=True, timeout=30, check=True,
        )
        return out.read_text().strip()


check("workflow carries no `paths:` filter (required check must always report)",
      not re.search(r"^\s*paths(-ignore)?:", TEXT, re.M))

check("every npm step is gated on the scope output",
      TEXT.count("if: steps.scope.outputs.run == 'true'") == 3)

if BASH:
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
    # Every dependency input `npm ci` consults, not just the lockfile: a shrinkwrap
    # takes precedence over it, and .npmrc can change what install even fetches.
    check("a shrinkwrap change lints",
          run_gate(script, "npm-shrinkwrap.json") == "run=true")
    check("an .npmrc change lints",
          run_gate(script, ".npmrc") == "run=true")
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
