#!/usr/bin/env python3
"""Regression checks for the generated Codex instruction file."""

from _helpers.hermetic_env import isolate_os_environ

# Before anything reads the environment: no outer credential selector survives.
isolate_os_environ()

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = next(p for p in Path(__file__).resolve().parents if (p / "src" / "vault_intercept.py").is_file())
CLAUDE = (REPO / "CLAUDE.md").read_text()
AGENTS = (REPO / "AGENTS.md").read_text()

failures = []
passed = 0


def check(name: str, condition: bool) -> None:
    global passed
    print(("  ok  " if condition else "  FAIL ") + name)
    if not condition:
        failures.append(name)
    else:
        passed += 1


sync = subprocess.run(
    ["bash", str(REPO / "scripts" / "agents-md-sync.sh"), "--check"],
    cwd=REPO,
    capture_output=True,
    text=True,
)
check("AGENTS.md matches generated output", sync.returncode == 0)
check(
    "real personal override filename survives generation",
    "PERSONAL_CLAUDE.md" in AGENTS and "PERSONAL_AGENTS.md" not in AGENTS,
)
check(
    "task-progress command is runtime-neutral",
    "python3 skills/task-progress/scripts/notify.py" in CLAUDE
    and "python3 skills/task-progress/scripts/notify.py" in AGENTS
    and "$CLAUDE_CONFIG_DIR/skills/task-progress" not in AGENTS,
)
check(
    "skill refresh guidance distinguishes runtimes",
    "For the Claude runtime" in AGENTS
    and "For the Codex runtime" in AGENTS
    and "refresh-skill.sh` does not update Codex" in AGENTS,
)

DELEGATION_DOC = (REPO / "docs" / "subagent-delegation.md").read_text()
check(
    "delegation doc names each runtime's mechanism",
    "run_in_background: true" in DELEGATION_DOC and "`spawn_agent`" in DELEGATION_DOC,
)
check(
    "CLAUDE.md and AGENTS.md pointers name the delegation doc",
    all("docs/subagent-delegation.md" in f and "per-runtime mechanism" in f for f in (CLAUDE, AGENTS)),
)

# Execute the documented import setup from a nested, repo-contained script.
with tempfile.NamedTemporaryFile(
    mode="w",
    suffix=".py",
    dir=REPO / "tests",
    delete=False,
) as script:
    script_path = Path(script.name)
    script.write(
        "import sys\n"
        "from pathlib import Path\n"
        "repo = next(p for p in Path(__file__).resolve().parents\n"
        '            if (p / "src" / "vault_intercept.py").is_file())\n'
        'sys.path.insert(0, str(repo / "src"))\n'
        "from vault_intercept import get_vault_key, list_vault_keys\n"
        "assert callable(get_vault_key) and callable(list_vault_keys)\n"
    )

try:
    vault_import = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
finally:
    script_path.unlink(missing_ok=True)

check("documented vault import works from a nested script", vault_import.returncode == 0)

DOC = (REPO / "docs" / "subagent-delegation.md").read_text()
check(
    "anti-deferral wording stays in CLAUDE.md",
    "not a licence to defer to a later session" in CLAUDE
    and "Too large for your remaining context is the\nreason TO delegate" in CLAUDE,
)
check(
    "heavy-work default takes the quota tier into account",
    "with the quota tier in mind" in CLAUDE,
)
check(
    "doc says the loop tiers gate the loop, not owner-requested work",
    "gate the autonomous loop; for work the owner\n  asked for, the tier is guidance to weigh here, not a gate." in DOC,
)
check(
    "heavy is defined once in the doc and CLAUDE.md points to it",
    "single definition of \"heavy\"" in DOC
    and "Heavy work (defined in `docs/subagent-delegation.md`)" in CLAUDE
    and "defined in the doc" not in DOC,
)

if failures:
    if sync.returncode != 0:
        print(sync.stderr)
    if vault_import.returncode != 0:
        print(vault_import.stderr)
    print(f"Results: {len(failures)} failed")
    raise SystemExit(1)

print(f"Results: {passed} passed")
