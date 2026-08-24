#!/usr/bin/env python3
"""The runner and the schedule surfaces must agree on which form an entry runs
as. They disagreed silently before: a present-but-blank shell_command made the
runner skip the entry while the dashboard advertised its fallback skill."""
import importlib.util
import sys
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

# --- the runner's own predicate, mirrored ------------------------------------
def runner_would_skip(entry):
    """Verbatim shape of src/cron-runner.py's pre-existing guard."""
    if "shell_command" not in entry:
        return False
    v = entry.get("shell_command")
    return not isinstance(v, str) or not v.strip()


for entry in [{"shell_command": "echo hi"}, {"shell_command": "  "},
              {"shell_command": 1}, {"prompt_skill": "f"}, {},
              {"shell_command": "x", "prompt_skill": "f"}]:
    check((sel(entry)[0] == cef.MALFORMED) == runner_would_skip(entry),
          f"selector agrees with the runner's skip predicate on {entry}")

check(sel("not-a-dict")[0] == cef.MALFORMED, "a non-dict entry is MALFORMED")

print(f"\n{'FAILED' if failures else 'OK'} — {len(failures)} failure(s)")
sys.exit(1 if failures else 0)
