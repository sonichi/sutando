#!/usr/bin/env python3
"""Regression checks for the generated Codex instruction file."""

import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CLAUDE = (REPO / "CLAUDE.md").read_text()
AGENTS = (REPO / "AGENTS.md").read_text()

failures = []


def check(name: str, condition: bool) -> None:
    print(("  ok  " if condition else "  FAIL ") + name)
    if not condition:
        failures.append(name)


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

if failures:
    if sync.returncode != 0:
        print(sync.stderr)
    if vault_import.returncode != 0:
        print(vault_import.stderr)
    print(f"Results: {len(failures)} failed")
    raise SystemExit(1)

print("Results: 5 passed")
