#!/usr/bin/env python3
"""The runner and the schedule surfaces must agree on which form an entry runs
as. They disagreed silently before: a present-but-blank shell_command made the
runner skip the entry while the dashboard advertised its fallback skill."""
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "cef", REPO / "src" / "cron_execution_form.py")
cef = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cef)

failures = []


def check(cond, label):
    print(("ok: " if cond else "FAIL: ") + label)
    if not cond:
        failures.append(label)


def sel(entry):
    return cef.select_execution_form(entry)


# --- precedence: shell > skill > prompt -------------------------------------
check(sel({"shell_command": "echo hi", "prompt_skill": "f", "prompt": "p"})
      == (cef.SHELL, "echo hi"), "shell wins over skill and prompt")
check(sel({"prompt_skill": "f", "prompt": "p"}) == (cef.SKILL, "f"),
      "skill wins over prompt")
check(sel({"prompt": "do x"}) == (cef.PROMPT, "do x"), "prompt is the fallback")
check(sel({}) == (cef.PROMPT, ""), "an empty entry is an empty prompt")

# --- presence, not truthiness: the defect this module exists for ------------
for bad, why in [("   ", "blank"), ("", "empty"), (123, "int"),
                 (None, "None"), ([], "list")]:
    kind, detail = sel({"shell_command": bad, "prompt_skill": "fallback"})
    check(kind == cef.MALFORMED,
          f"a {why} shell_command is MALFORMED, never a fall-through to skill")
    check("fallback" not in detail,
          f"a {why} shell_command does not name the skill that will not run")

check(sel({"prompt_skill": "fallback"})[0] == cef.SKILL,
      "control: with NO shell key at all, the skill leg is correct")

# Production parity. A mirrored copy of the runner's predicate stood in for the
# runner here, and agreed with the selector while production disagreed with both.
_spec = importlib.util.spec_from_file_location(
    "cron_runner_prod", REPO / "src" / "cron-runner.py")
_runner = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_runner)
except SystemExit:
    pass
import dashboard_schedules as _ds  # noqa: E402


def _runner_emits(entry):
    """The task: line the REAL emit_task writes — not a re-derivation of it."""
    with tempfile.TemporaryDirectory() as td:
        tasks = Path(td) / "tasks"
        tasks.mkdir()
        prev = os.environ.get("SUTANDO_TASKS_DIR")
        os.environ["SUTANDO_TASKS_DIR"] = str(tasks)
        try:
            written = _runner.emit_task(entry.get("name", "j"), entry)
            m = re.search(r"^task:\s*(.*)$", written.read_text(), re.M)
            return m.group(1) if m else ""
        finally:
            if prev is None:
                os.environ.pop("SUTANDO_TASKS_DIR", None)
            else:
                os.environ["SUTANDO_TASKS_DIR"] = prev


def _dashboard_kind(entry):
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "crons.json"
        f.write_text(json.dumps([entry]))
        return _ds.list_schedules(f)[0].get("kind")


# Each adjacent prompt_skill shape, alongside a real prompt: emitted target and
# advertised kind must agree.
for why, extra in [("absent", {}),
                   ("blank", {"prompt_skill": "   "}),
                   ("padded", {"prompt_skill": " morning "}),
                   ("non-string", {"prompt_skill": 5}),
                   ("real skill", {"prompt_skill": "morning"})]:
    e = {"name": "j", "cron": "* * * * *", "launchd": True,
         "prompt": "do work", **extra}
    emitted = _runner_emits(e)
    runner_kind = cef.SKILL if emitted.startswith("/") else cef.PROMPT
    check(runner_kind == _dashboard_kind(e),
          f"{why} prompt_skill: runner ({runner_kind}, emits {emitted!r}) and "
          f"dashboard ({_dashboard_kind(e)!r}) agree")
    check(runner_kind == sel(e)[0],
          f"{why} prompt_skill: the runner follows the shared selector")

check(_runner_emits({"name": "j", "cron": "* * * * *", "launchd": True,
                     "prompt_skill": "   ", "prompt": "do work"}) == "do work",
      "a blank prompt_skill must not emit a slash-command with no skill name")

check(sel("not-a-dict")[0] == cef.MALFORMED, "a non-dict entry is MALFORMED")

# --- the payload is VERBATIM: whitespace decides, it never edits ------------

# Exact bytes: a .strip() reintroduced anywhere in the selector fails here.
check(sel({"prompt": "\n  preserve leading\ntrailing  \n\n"})
      == (cef.PROMPT, "\n  preserve leading\ntrailing  \n\n"),
      "prompt bytes survive the selector exactly")
check(sel({"prompt_skill": " morning \n"}) == (cef.SKILL, " morning \n"),
      "skill bytes survive the selector exactly")
check(sel({"shell_command": " echo hi \n"}) == (cef.SHELL, " echo hi \n"),
      "shell bytes survive the selector exactly")
check(sel({"prompt_skill": "   \t \n"})[0] == cef.PROMPT,
      "an all-whitespace skill is still not a skill")

# --- per-executor contract: an unrunnable form is TERMINAL, not a fallback --
mixed = {"shell_command": "echo hi", "prompt_skill": "fallback"}
check(cef.select_for_executor(mixed, cef.LAUNCHD_FORMS) == (cef.SHELL, "echo hi"),
      "launchd runs the shell leg of a mixed entry")
k, why = cef.select_for_executor(mixed, cef.CODEX_FORMS)
check(k == cef.MALFORMED and "not runnable" in why,
      "codex refuses a mixed entry instead of running its fallback skill")
k, _ = cef.select_for_executor({"shell_command": "   ", "prompt_skill": "fallback"},
                               cef.CODEX_FORMS)
check(k == cef.MALFORMED, "a blank shell never falls through to the skill leg")
check(cef.select_for_executor({}, cef.CODEX_FORMS) == (cef.PROMPT, ""),
      "emptiness is the executor's rule, not the form selector's")
check(cef.select_for_executor({"prompt_skill": "fallback"}, cef.CODEX_FORMS)
      == (cef.SKILL, "fallback"), "codex still runs an ordinary skill entry")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
