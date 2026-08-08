#!/usr/bin/env python3
"""Tests that src/agent/claude/cli/start-cli.sh IGNORES $SUTANDO_CORE_MODEL.

INVERTED 2026-08-08. This suite asserted the opposite (PR #1428): that the script
threaded the var through as `--model` for health-check's wedge-recovery downgrade.
That producer is removed, and honoring the var is what made the downgrade permanent
— the value lived in the tmux session env, so every later launch re-read it and
re-applied `--model`. One host ran 17 days on a pin set for one incident: 165
autocompactions in 25h. An ambient env var cannot be told apart from a current
choice, so the launcher no longer reads one.
  - unset → NO --model flag (core inherits the global model; 1M stays default)
  - set   → STILL no --model flag; the stale pin is ignored
  - the tmux defaults hook clears the var in BOTH scopes so a host pinned before
    this change heals itself

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
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"


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


def case_env_set_is_ignored() -> list[str]:
    """A pin in the environment must NOT reach claude's argv."""
    fails = []
    argv = _launch_argv("opus")
    if not argv:
        return ["set) claude was never exec'd (fallback path didn't run)"]
    if "--model" in argv:
        fails.append(f"set) a stale SUTANDO_CORE_MODEL pin still reached claude: {argv}")
    if "opus" in argv:
        fails.append(f"set) the pinned value leaked into argv: {argv}")
    return fails


def case_tmux_defaults_clear_both_scopes() -> list[str]:
    """The self-healing clear must target BOTH tmux env scopes.

    Static, so it runs on a CI box with no tmux: a behavioural-only test would
    skip there and prove nothing — the trap #2717's own follow-up commit hit.
    -g is invisible to a per-session query, so clearing one scope leaves the pin
    live in the other.
    """
    src = SCRIPT.read_text()
    # Comments discuss the removed flag by name, so scan CODE only — otherwise the
    # explanation of the removal trips the guard against the thing it removed.
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    fails = []
    if "setenv -gu SUTANDO_CORE_MODEL" not in code:
        fails.append("clear) apply_tmux_defaults must clear the GLOBAL scope (setenv -gu)")
    if "setenv -u SUTANDO_CORE_MODEL" not in code:
        fails.append("clear) apply_tmux_defaults must clear the SESSION scope (setenv -u)")
    # The pass-through must be gone, not merely bypassed.
    if "MODEL_ARGS" in code:
        fails.append("clear) MODEL_ARGS is back — an empty array is one edit from a pin")
    if "--model" in code:
        fails.append("clear) start-cli.sh reintroduced a --model flag")
    return fails


def main() -> int:
    cases = [
        ("default", case_default_no_model_flag),
        ("env-ignored", case_env_set_is_ignored),
        ("tmux-clear", case_tmux_defaults_clear_both_scopes),
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
    print("\nstart-cli.sh ignores the model pin and clears it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
