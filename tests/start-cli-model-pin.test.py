#!/usr/bin/env python3
"""Tests that scripts/start-cli.sh honors $SUTANDO_CORE_MODEL (PR #1428).

The recovery escalation in src/health-check.py restarts a re-wedging core with
SUTANDO_CORE_MODEL=opus so it falls back to standard 200K context. This verifies
the launch script actually threads that through:
  - unset  → NO --model flag (core inherits the global model; 1M stays default)
  - set    → `--model <value>` injected before the other claude flags

Drives the no-tmux fallback branch (the bare `exec claude …`) with a stub
`claude` that records its argv, and a stub `pgrep` that always reports "not
running" so the test is independent of any live sutando-core on the host.

Run: python3 tests/start-cli-model-pin.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "start-cli.sh"


def _launch_argv(model_env: "str | None") -> list[str]:
    """Run start-cli.sh through its no-tmux fallback and return claude's argv."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bind = td / "bin"
        bind.mkdir()
        args_file = td / "argv.txt"

        # Stub claude: record argv (one per line), exit 0.
        claude = bind / "claude"
        claude.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$ARGS_FILE"\n')
        claude.chmod(0o755)
        # Stub pgrep: always "no match" so the already-running guard passes.
        pgrep = bind / "pgrep"
        pgrep.write_text("#!/bin/bash\nexit 1\n")
        pgrep.chmod(0o755)

        env = {
            # /usr/bin excluded intentionally: tmux lives there on Ubuntu CI
            # and the test targets the no-tmux fallback exec path.
            "PATH": f"{bind}:/bin",
            "HOME": str(td),
            "ARGS_FILE": str(args_file),
        }
        if model_env is not None:
            env["SUTANDO_CORE_MODEL"] = model_env

        subprocess.run(
            ["/bin/bash", str(SCRIPT)],
            env=env, capture_output=True, text=True, timeout=30,
        )
        if not args_file.exists():
            return []
        return [ln for ln in args_file.read_text().splitlines() if ln != ""]


def case_default_no_model_flag() -> list[str]:
    fails = []
    argv = _launch_argv(None)
    if not argv:
        return ["default) claude was never exec'd (fallback path didn't run)"]
    if "--model" in argv:
        fails.append(f"default) unexpected --model in argv (1M should stay default): {argv}")
    if "--name" not in argv or "sutando-core" not in argv:
        fails.append(f"default) sanity: expected --name sutando-core, got {argv}")
    return fails


def case_env_set_injects_model() -> list[str]:
    fails = []
    argv = _launch_argv("opus")
    if not argv:
        return ["set) claude was never exec'd (fallback path didn't run)"]
    if "--model" not in argv:
        fails.append(f"set) --model not injected when SUTANDO_CORE_MODEL=opus: {argv}")
    else:
        i = argv.index("--model")
        if i + 1 >= len(argv) or argv[i + 1] != "opus":
            fails.append(f"set) --model not followed by 'opus': {argv}")
        # Must precede --remote-control (matches the launch line ordering).
        if "--remote-control" in argv and argv.index("--remote-control") < i:
            fails.append(f"set) --model should come before --remote-control: {argv}")
    return fails


def main() -> int:
    cases = [
        ("default", case_default_no_model_flag),
        ("env-set", case_env_set_injects_model),
    ]
    all_failures = []
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
    print("\nstart-cli.sh model-pin threading is correct.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
