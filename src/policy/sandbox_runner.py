#!/usr/bin/env python3
"""Which CLI runs a non-owner request in the read-only, no-network sandbox, and how.

One owner for a choice two kinds of adapter make: the in-band guardrail text the
gateway and Slack write for the core to execute, and the Discord bridge, which
both renders that text and spawns the sandbox itself. Each runner's invocation
lives here once, so the text and the argv cannot drift.

Order: the first runner that is both SAFE and installed, from
`SUTANDO_SANDBOX_RUNNERS` (env override, then the value a skill manifest's `config`
declares, then DEFAULT_ORDER). A runner is SAFE only when its flags alone, with no
persisted settings, give a read-only, no-network session; one that cannot is listed
with the reason and always skipped. None installed: `select_runner` returns None and
callers render the codex command, whose absence the core reports as "unavailable".

Stdlib only: vendored verbatim into ag2-sparrow beside the guardrail.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Sequence

CONFIG_KEY = "SUTANDO_SANDBOX_RUNNERS"
DEFAULT_ORDER = ("agy", "codex", "claude")

# Launchd bridges run with brew bin + system PATH only, but the claude and agy
# installers put their CLI here; without it a claude-only host reads as "none".
USER_BIN_DIRS = (Path.home() / ".local" / "bin",)

# Read/Grep/Glob only: no Bash, Write, Edit, NotebookEdit, WebFetch or WebSearch.
CLAUDE_READONLY_TOOLS = "Read,Grep,Glob"


class Runner(NamedTuple):
    name: str
    safe: bool
    # Seconds of silence before codex-bounded.sh kills it; 0 = the CLI prints
    # nothing until it finishes, so silence is not a stall signal.
    stall_seconds: int
    # True: the answer is the process's stdout. False: the CLI writes it to `-o`.
    answer_on_stdout: bool
    unsafe_reason: str = ""


RUNNERS = {
    "agy": Runner(
        "agy", safe=False, stall_seconds=0, answer_on_stdout=True,
        unsafe_reason=(
            "agy 1.3.x has no flag-only read-only, no-network mode: --sandbox restricts "
            "terminal commands only (its own scratch dirs stay writable), --mode plan is a "
            "planning mode not a write ban, search_web/read_url_content stay available, and "
            "headless runs honor whatever persisted settings.json permissions allow"),
    ),
    "codex": Runner("codex", safe=True, stall_seconds=45, answer_on_stdout=False),
    "claude": Runner("claude", safe=True, stall_seconds=0, answer_on_stdout=True),
}


class Invocation(NamedTuple):
    runner: Runner
    argv: list
    # Process cwd the caller must set (claude has no -C flag); None = inherit.
    cwd: Optional[str]


def _skills_dirs() -> list:
    """`<repo>/skills` when this file sits inside a checkout (src/policy or the
    vendored package); empty for a standalone install."""
    here = Path(__file__).resolve()
    for parent in list(here.parents)[:4]:
        cand = parent / "skills"
        if (cand / "MANIFEST.md").is_file():
            return [cand]
    return []


def declared_order(skills_dirs: Optional[Sequence] = None) -> Optional[str]:
    """The value an enabled skill manifest's `config` block declares for CONFIG_KEY."""
    for d in (_skills_dirs() if skills_dirs is None else skills_dirs):
        for manifest in sorted(Path(d).glob("*/manifest.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("enabled") is False:
                continue
            cfg = data.get("config")
            if isinstance(cfg, dict) and isinstance(cfg.get(CONFIG_KEY), str):
                return cfg[CONFIG_KEY]
    return None


def resolve_order(env=None, skills_dirs: Optional[Sequence] = None) -> tuple:
    """env > manifest config > DEFAULT_ORDER. Unknown names are dropped; an order
    with no known name falls back to the default rather than to no sandbox."""
    env = os.environ if env is None else env
    raw = env.get(CONFIG_KEY) or declared_order(skills_dirs) or ""
    names = tuple(n for n in (p.strip().lower() for p in raw.split(",")) if n in RUNNERS)
    return names or DEFAULT_ORDER


def find_binary(name: str, which: Callable = shutil.which) -> Optional[str]:
    """Absolute path of `name` on PATH, else in USER_BIN_DIRS."""
    found = which(name)
    if found:
        return found
    extra = os.pathsep.join(str(d) for d in USER_BIN_DIRS)
    return which(name, path=extra)


def select_runner(order: Optional[Sequence] = None, which: Callable = shutil.which,
                  env=None) -> Optional[tuple]:
    """(Runner, absolute binary) for the first safe, installed runner; None if none."""
    for name in (resolve_order(env) if order is None else order):
        runner = RUNNERS.get(name)
        if runner is None or not runner.safe:
            continue
        path = find_binary(name, which)
        if path:
            return runner, path
    return None


def _base_args(runner: Runner, cwd: Optional[str], out_path: Optional[str],
               model: Optional[str]) -> list:
    if runner.name == "codex":
        args = ["exec", "--sandbox", "read-only", "--skip-git-repo-check"]
        if cwd:
            args += ["-C", cwd]
        if out_path:
            args += ["-o", out_path]
        if model:
            args += ["-m", model]
        return args
    if runner.name == "claude":
        # --restricted drops code-running tools and WebFetch and ignores user/project
        # settings; --strict-mcp-config with no --mcp-config loads no MCP server.
        args = ["-p", "--restricted", "--strict-mcp-config",
                "--tools", CLAUDE_READONLY_TOOLS, "--permission-mode", "dontAsk",
                "--disable-slash-commands", "--no-session-persistence"]
        if model:
            args += ["--model", model]
        return args
    raise ValueError(f"no safe invocation for runner {runner.name!r}")


def invocation(runner: Runner, binary: str, prompt: str, *, cwd: Optional[str] = None,
               out_path: Optional[str] = None, model: Optional[str] = None) -> Invocation:
    """argv for a spawn. Callers close stdin and, when `answer_on_stdout`, read stdout."""
    argv = [binary, *_base_args(runner, cwd, None if runner.answer_on_stdout else out_path,
                                model if runner.name == "codex" else None), "--", prompt]
    return Invocation(runner, argv, None if runner.name == "codex" else cwd)


def shell_command(runner: Runner, prompt_arg: str, *, cwd: Optional[str] = None,
                  out_path: Optional[str] = None) -> str:
    """The command line the core runs: bare CLI name (the core's own PATH), stdin from
    /dev/null, and the answer in `out_path` when given. `prompt_arg` is inserted
    as already-quoted shell text; paths may carry `{id}`-style placeholders."""
    args = _base_args(runner, cwd, None if runner.answer_on_stdout else out_path, None)
    cmd = " ".join([runner.name, *args, "--", prompt_arg, "< /dev/null"])
    if runner.answer_on_stdout and out_path:
        cmd += f" > {out_path}"
    if runner.name != "codex" and cwd:
        cmd = f"/bin/sh -c 'cd \"$0\" && exec \"$@\"' {cwd} {cmd}"
    return cmd


def selected_or_codex(order: Optional[Sequence] = None, which: Callable = shutil.which,
                      env=None) -> Runner:
    """The runner to render into instruction text: the selected one, or codex when none
    is installed, so the core's attempt fails and it reports the sandbox unavailable."""
    picked = select_runner(order, which, env)
    return picked[0] if picked else RUNNERS["codex"]
