#!/usr/bin/env python3
"""The prompt a crons.json entry is registered with, owner-task gate included.

A session cron (CronCreate'd by the core's /schedule-crons or a worker's
startup pass) that fires more than once a day yields to queued owner work by
construction: its registered prompt opens with a
`bash scripts/cron-gate.sh --check <name>` preamble that tells the firing agent
to stop when owner tasks are queued. Two registration passes build prompts from
the same file, so the rule lives here once — see
docs/architecture-boundaries.md "Shared adapter policy".

The default follows the entry's `cron` cadence; `"gate": true` / `"gate": false`
on the entry overrides it either way:
  - sub-daily (minute values x hour values > 1): gated — the next tick covers
    a deferred fire;
  - daily or rarer (one minute, one hour): not gated — a deferred fire would
    lose the job until its next day;
  - no `cron` (a dynamic loop) or one that does not parse: gated.

Never gated, and registered verbatim:
  - the canonical main loop (`main-loop` / `prompt_skill: proactive-loop` / a
    prompt that invokes /proactive-loop): it IS the owner-task handler, and
    gating it deadlocks the queue;
  - a prompt that already runs `bash scripts/cron-gate.sh ...` (never double-wrap).

CLI (prints the owner-filtered entries, each with `registered_prompt`):
    python3 src/cron_prompt_gate.py <crons.json> [--owner core|<worker id>]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cron_ownership import CORE, entries_for_owner  # noqa: E402

GATE_SCRIPT = "scripts/cron-gate.sh"
# An invocation of the gate, not a mention of it in prose.
_GATE_CALL = re.compile(r"(^|[\s`])bash " + re.escape(GATE_SCRIPT) + r"\s")
LOOP_NAME = "main-loop"
LOOP_SKILL = "proactive-loop"
# The slash command itself, not a path such as skills/proactive-loop/scripts/x.py.
_LOOP_CALL = re.compile(r"(^|[\s`])/" + re.escape(LOOP_SKILL) + r"(?![\w/-])")


def base_prompt(entry: dict) -> Optional[str]:
    """The prompt as /schedule-crons registered it before gating existed:
    `/skill` for a prompt_skill entry, else the prompt string; None for neither."""
    skill = entry.get("prompt_skill")
    if isinstance(skill, str) and skill.strip():
        return "/" + skill.strip().lstrip("/")
    prompt = entry.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt
    return None


def is_main_loop(entry: dict) -> bool:
    skill = entry.get("prompt_skill")
    if isinstance(skill, str) and skill.strip().lstrip("/") == LOOP_SKILL:
        return True
    if entry.get("name") == LOOP_NAME:
        return True
    prompt = entry.get("prompt")
    return isinstance(prompt, str) and _LOOP_CALL.search(prompt) is not None


def _field_values(field: str, lo: int, hi: int) -> Optional[set]:
    """The values a cron field selects within lo..hi; None when it does not parse."""
    values: set = set()
    for part in field.split(","):
        base, sep, step_text = part.partition("/")
        if sep and not step_text.isdecimal():
            return None
        step = int(step_text) if sep else 1
        if step < 1:
            return None
        if base == "*":
            start, end = lo, hi
        else:
            first, dash, last = base.partition("-")
            if not first.isdecimal() or (dash and not last.isdecimal()):
                return None
            start = int(first)
            # `5/15` runs from 5 to the field's maximum, as cron reads it.
            end = int(last) if dash else (hi if sep else start)
        if start < lo or end > hi or start > end:
            return None
        values.update(range(start, end + 1, step))
    return values


def fires_per_day(entry: dict) -> Optional[int]:
    """Fires on a day the entry runs at all (minute values x hour values);
    None when `cron` is absent or is not a parseable 5-field expression."""
    cron = entry.get("cron")
    if not isinstance(cron, str):
        return None
    fields = cron.split()
    if len(fields) != 5:
        return None
    minutes = _field_values(fields[0], 0, 59)
    hours = _field_values(fields[1], 0, 23)
    if not minutes or not hours:
        return None
    return len(minutes) * len(hours)


def gated_by_default(entry: dict) -> bool:
    """Sub-daily entries yield to owner work; a daily-or-rarer one would lose its day."""
    # No cadence to read (dynamic loop, malformed cron): gate, since gating only delays.
    fires = fires_per_day(entry)
    return fires is None or fires > 1


def is_gated(entry: dict) -> bool:
    """Whether the registered prompt gets the owner-task preamble."""
    if is_main_loop(entry):
        return False
    prompt = base_prompt(entry)
    if prompt is None or _GATE_CALL.search(prompt) is not None:
        return False
    gate = entry.get("gate")
    return gate if isinstance(gate, bool) else gated_by_default(entry)


def gate_reason(entry: dict) -> str:
    for key in ("name", "prompt_skill"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return "".join(c if c.isalnum() or c in "-_." else "-" for c in value.strip())
    return "cron"


def preamble(reason: str) -> str:
    return (f"First run `bash {GATE_SCRIPT} --check {reason}`. If it prints `deferred`, "
            "stop here: owner tasks are queued and this job retries on its next fire. "
            "If it prints `clear`, carry on: ")


def registered_prompt(entry: dict) -> Optional[str]:
    """The text to pass as CronCreate's prompt (or the /loop body). None when
    the entry has neither prompt nor prompt_skill (a monitor entry)."""
    prompt = base_prompt(entry)
    if prompt is None or not is_gated(entry):
        return prompt
    if entry.get("prompt_skill"):
        return preamble(gate_reason(entry)) + f"invoke the skill {prompt}"
    return preamble(gate_reason(entry)) + prompt


def registration_view(entries: list, owner_id: str = CORE) -> list:
    """entries_for_owner(...) with `registered_prompt` filled in on each entry."""
    out = []
    for entry in entries_for_owner(entries, owner_id):
        e = dict(entry)
        e["registered_prompt"] = registered_prompt(entry)
        out.append(e)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("crons_json", help="path to the host's crons.json")
    ap.add_argument("--owner", default=CORE,
                    help="session registering: 'core' (default) or a worker's bare id")
    args = ap.parse_args(argv)
    try:
        cfg = json.loads(Path(args.crons_json).read_text())
    except FileNotFoundError:
        cfg = []
    if not isinstance(cfg, list):
        print(f"{args.crons_json}: expected a JSON list", file=sys.stderr)
        return 2
    print(json.dumps(registration_view(cfg, args.owner)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
