#!/usr/bin/env python3
"""core.effort must resolve from config AND reach claude's argv — a resolver-only
test would pass against the bug this fixes (a value nothing consumes)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"
sys.path.insert(0, str(REPO))

from src.sutando_config import resolve_core_effort  # noqa: E402


# --------------------------- resolver cases --------------------------- #

def _with_config(td: Path, cfg: "dict | None") -> Path:
    """A repo-shaped dir carrying only sutando.config.local.json."""
    root = td / "repo"
    root.mkdir(parents=True, exist_ok=True)
    if cfg is not None:
        (root / "sutando.config.local.json").write_text(json.dumps(cfg))
    return root


def case_resolver() -> list[str]:
    fails = []
    saved = os.environ.pop("SUTANDO_CORE_EFFORT", None)
    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)

            root = _with_config(td / "a", None)
            if resolve_core_effort(root) != "":
                fails.append("resolver) unset config should yield '' (no flag), got "
                             f"{resolve_core_effort(root)!r}")

            root = _with_config(td / "b", {"core": {"effort": "xhigh"}})
            if resolve_core_effort(root) != "xhigh":
                fails.append(f"resolver) core.effort=xhigh -> {resolve_core_effort(root)!r}")

            # Every level on the scale must be accepted, or the config silently
            # rejects a value the CLI itself documents.
            for lvl in ("low", "medium", "high", "xhigh", "max"):
                r = _with_config(td / f"lvl-{lvl}", {"core": {"effort": lvl}})
                if resolve_core_effort(r) != lvl:
                    fails.append(f"resolver) level {lvl!r} not accepted")

            root = _with_config(td / "c", {"core": {"effort": "low"}})
            os.environ["SUTANDO_CORE_EFFORT"] = "max"
            if resolve_core_effort(root) != "max":
                fails.append("resolver) env SUTANDO_CORE_EFFORT must win over config")
            del os.environ["SUTANDO_CORE_EFFORT"]

            root = _with_config(td / "d", {"core": {"effort": "maximum"}})
            try:
                got = resolve_core_effort(root)
                fails.append(f"resolver) invalid 'maximum' should raise, returned {got!r}")
            except ValueError as e:
                if "maximum" not in str(e):
                    fails.append(f"resolver) error should name the bad value: {e}")
    finally:
        os.environ.pop("SUTANDO_CORE_EFFORT", None)
        if saved is not None:
            os.environ["SUTANDO_CORE_EFFORT"] = saved
    return fails


# --------------------------- launcher cases --------------------------- #

def _isolated_workspace(td: Path) -> str:
    """start-cli.sh writes <ws>/state/... and aborts under set -e without it."""
    ws = td / "workspace"
    (ws / "state").mkdir(parents=True, exist_ok=True)
    return str(ws)


def _launch(effort_env: "str | None") -> tuple[list[str], str]:
    """Run start-cli.sh's no-tmux fallback; return (claude argv, stderr)."""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bind = td / "bin"
        bind.mkdir()
        args_file = td / "argv.txt"

        claude = bind / "claude"
        claude.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$ARGS_FILE"\n')
        claude.chmod(0o755)
        pgrep = bind / "pgrep"
        pgrep.write_text("#!/bin/bash\nexit 1\n")
        pgrep.chmod(0o755)
        # /usr/bin is kept off PATH so the no-tmux fallback is what runs; the
        # config helper still needs python and coreutils, so link those in.
        os.symlink(sys.executable, bind / "python3")
        for tool in ("dirname", "basename", "uname", "sed", "awk", "grep",
                     "env", "cut", "head", "tail", "tr", "cat", "mktemp"):
            found = shutil.which(tool)
            if found and not (bind / tool).exists():
                os.symlink(found, bind / tool)

        env = {
            "PATH": f"{bind}:/bin",
            "HOME": str(td),
            "ARGS_FILE": str(args_file),
            "SUTANDO_TEST_MODE": "1",
            "SUTANDO_WORKSPACE": _isolated_workspace(td),
        }
        if effort_env is not None:
            env["SUTANDO_CORE_EFFORT"] = effort_env

        p = subprocess.run(["/bin/bash", str(SCRIPT)], env=env,
                           capture_output=True, text=True, timeout=60)
        argv = []
        if args_file.exists():
            argv = [ln for ln in args_file.read_text().splitlines() if ln != ""]
        return argv, p.stderr


def case_launcher() -> list[str]:
    fails = []

    argv, _ = _launch(None)
    if not argv:
        return ["launcher) claude was never exec'd — the fallback path did not run"]
    if "--effort" in argv:
        fails.append(f"launcher) unconfigured install must pass no --effort: {argv}")

    argv, _ = _launch("xhigh")
    if "--effort" not in argv:
        fails.append(f"launcher) configured effort never reached argv: {argv}")
    else:
        i = argv.index("--effort")
        if i + 1 >= len(argv) or argv[i + 1] != "xhigh":
            fails.append(f"launcher) --effort not followed by the value: {argv}")
        if "--name" not in argv or "-p" in argv[:1]:
            fails.append(f"launcher) argv no longer looks like a core launch: {argv}")

    # A bad value must degrade, never cost the host its core.
    argv, err = _launch("maximum")
    if not argv:
        fails.append("launcher) invalid effort aborted the launch instead of degrading")
    elif "--effort" in argv:
        fails.append(f"launcher) invalid effort must not be forwarded: {argv}")
    if "core effort" not in err:
        fails.append(f"launcher) invalid effort must warn on stderr, got: {err[-300:]!r}")

    return fails


def main() -> int:
    fails = []
    for name, fn in (("resolver", case_resolver), ("launcher", case_launcher)):
        got = fn()
        print(f"  {'ok ' if not got else 'FAIL'} {name}")
        fails += got
    if fails:
        print("\nFAILURES:")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("\nPASS — core.effort resolves and reaches claude's argv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
