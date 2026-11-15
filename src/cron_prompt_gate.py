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
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cron_ownership import CORE, entries_for_owner  # noqa: E402

GATE_SCRIPT = "scripts/cron-gate.sh"
# Absolute, so the preamble runs from any cwd (a worker's need not be the repo).
GATE_PATH = Path(__file__).resolve().parents[1] / GATE_SCRIPT
# An invocation of the gate (relative, absolute or quoted), not a mention of it in prose.
_GATE_CALL = re.compile(r"(^|[\s`])bash (?:'[^']*/|\"[^\"]*/|\S*/)?"
                        + re.escape(GATE_SCRIPT) + r"['\"]?\s")
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


def gate_command(reason: str, workspace: Optional[str] = None) -> str:
    cmd = f"bash {shlex.quote(str(GATE_PATH))} --check {reason}"
    if workspace:
        cmd += f" --workspace {shlex.quote(workspace)}"
    return cmd


def preamble(reason: str, workspace: Optional[str] = None) -> str:
    return (f"First run `{gate_command(reason, workspace)}`. If it prints `deferred`, "
            "stop here: owner tasks are queued and this job retries on its next fire. "
            "If it prints anything other than `deferred` or `clear`, or fails, stop too. "
            "If it prints `clear`, carry on: ")


def registered_prompt(entry: dict, workspace: Optional[str] = None) -> Optional[str]:
    """The text to pass as CronCreate's prompt (or the /loop body). None when
    the entry has neither prompt nor prompt_skill (a monitor entry). `workspace`
    names the queue the gate checks (a worker's assigned one); None = the configured one."""
    prompt = base_prompt(entry)
    if prompt is None or not is_gated(entry):
        return prompt
    if entry.get("prompt_skill"):
        return preamble(gate_reason(entry), workspace) + f"invoke the skill {prompt}"
    return preamble(gate_reason(entry), workspace) + prompt


def registration_view(entries: list, owner_id: str = CORE,
                      workspace: Optional[str] = None) -> list:
    """entries_for_owner(...) with `registered_prompt` filled in on each entry."""
    out = []
    for entry in entries_for_owner(entries, owner_id):
        e = dict(entry)
        e["registered_prompt"] = registered_prompt(entry, workspace)
        out.append(e)
    return out


class ListingError(ValueError):
    """The CronList receipt or the registry was not observed or is not usable."""


def _text(v) -> bool:
    return isinstance(v, (str, int)) and not isinstance(v, bool) and str(v).strip() != ""


def load_jobs(path) -> list:
    """Jobs as [{id, cron, prompt}] from the file the session wrote after calling CronList."""
    if not path:
        raise ListingError("CronList receipt path is empty")
    try:
        jobs = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ListingError(f"CronList receipt {path} not readable: {exc}")
    good = isinstance(jobs, list) and all(
        isinstance(j, dict) and _text(j.get("id")) and _text(j.get("cron"))
        and isinstance(j.get("prompt"), str) and j["prompt"].strip() != "" for j in jobs)
    if not good:
        raise ListingError(f"CronList receipt {path} is not a list of jobs with non-blank id, cron and prompt")
    return jobs


def entry_hash(entry: dict, prompt: str) -> str:
    """What a registered job was built from: its schedule and exact prompt."""
    cron = " ".join(str(entry.get("cron")).split())
    return hashlib.sha256(f"{cron}\0{prompt}".encode()).hexdigest()[:16]


def load_registry(path) -> dict:
    """{name: {id, hash, pending}} this worker recorded; an absent file means nothing recorded yet.

    `pending` lists superseded job ids not yet confirmed deleted."""
    if not path:
        raise ListingError("registry path is empty")
    try:
        raw = Path(path).read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ListingError(f"registry {path} not readable: {exc}")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ListingError(f"registry {path} is corrupt: {exc}")
    ents = data.get("entries") if isinstance(data, dict) else None
    ok = isinstance(ents, dict)
    if ok:
        for v in ents.values():
            pend = v.get("pending", []) if isinstance(v, dict) else None
            ok = ok and isinstance(v, dict) and _text(v.get("id")) and _text(v.get("hash")) \
                and isinstance(pend, list) and all(_text(i) for i in pend)
    if not ok:
        raise ListingError(f"registry {path} is corrupt: unexpected shape")
    for v in ents.values():
        v["pending"] = [str(i) for i in v.get("pending", [])]
    return ents


def _save(path, ents: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps({"version": 1, "entries": ents}))
    os.replace(tmp, target)


def record(path, entry: dict, prompt: str, job_id) -> None:
    """Record the new job id for an entry; the id it supersedes becomes pending deletion."""
    if not _text(job_id):
        raise ListingError("job id to record is blank")
    ents = load_registry(path)
    old = ents.get(entry["name"], {})
    pending = list(old.get("pending", []))
    if old.get("id") and old["id"] != str(job_id) and old["id"] not in pending:
        pending.append(old["id"])
    ents[entry["name"]] = {"id": str(job_id), "hash": entry_hash(entry, prompt),
                           "pending": [i for i in pending if i != str(job_id)]}
    _save(path, ents)


def confirm_deleted(path, name: str, job_id) -> None:
    """Drop a pending id once the session has seen it gone from CronList."""
    ents = load_registry(path)
    if name not in ents:
        raise ListingError(f"no registry record for {name!r}")
    ents[name]["pending"] = [i for i in ents[name]["pending"] if i != str(job_id)]
    _save(path, ents)


def plan_cron_actions(view: list, jobs: list, registry: dict) -> list:
    """Per entry: create / keep / replace, plus the old ids to delete and the settled ones to clear.

    Identity is the job id the worker recorded, matched against the listed ids, which
    CronList never truncates; the dispatched prompt is exactly the configured one.
    `delete_ids` are superseded jobs still listed (retried every startup until gone);
    `clear_ids` are pending ids already absent, for --confirm-deleted."""
    names = [e.get("name") for e in view]
    dupes = sorted({n for n in names if isinstance(n, str) and names.count(n) > 1})
    if dupes:
        raise ListingError(f"duplicate pinned names {dupes}")
    listed = {str(j["id"]) for j in jobs}
    plan = []
    for e in view:
        prompt, name = e.get("registered_prompt"), e.get("name")
        if not prompt or not isinstance(name, str) or not name.strip():
            continue
        rec = registry.get(name)
        pending = rec["pending"] if rec else []
        delete = [i for i in pending if i in listed]
        if rec and rec["id"] in listed:
            action = "keep" if rec["hash"] == entry_hash(e, prompt) else "replace"
            if action == "replace":
                delete = [rec["id"]] + delete
        else:
            action = "create"
        plan.append({"name": name, "action": action, "cron": e.get("cron"),
                     "registered_prompt": prompt, "delete_ids": delete,
                     "clear_ids": [i for i in pending if i not in listed]})
    return plan


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("crons_json", help="path to the host's crons.json")
    ap.add_argument("--owner", default=CORE,
                    help="session registering: 'core' (default) or a worker's bare id")
    ap.add_argument("--workspace", default=None,
                    help="workspace whose queue the gate checks (a worker's assigned one)")
    ap.add_argument("--registered", default=None, metavar="FILE",
                    help="JSON file of the session's CronList jobs [{id, cron, prompt}]; prints a "
                         "create/keep/replace plan and fails closed when it is unreadable")
    ap.add_argument("--registry", default=None, metavar="FILE",
                    help="this worker's record of the job id behind each registered entry")
    ap.add_argument("--record", nargs=2, metavar=("NAME", "JOB_ID"), default=None,
                    help="record the job id CronCreate returned for NAME, then exit")
    ap.add_argument("--confirm-deleted", nargs=2, metavar=("NAME", "JOB_ID"), default=None,
                    help="clear JOB_ID from NAME's pending deletions after CronList shows it gone")
    args = ap.parse_args(argv)
    try:
        cfg = json.loads(Path(args.crons_json).read_text())
    except FileNotFoundError:
        cfg = []
    if not isinstance(cfg, list):
        print(f"{args.crons_json}: expected a JSON list", file=sys.stderr)
        return 2
    view = registration_view(cfg, args.owner, args.workspace)
    unnamed = [e for e in view if not isinstance(e.get("name"), str) or not e["name"].strip()]
    if unnamed:
        print(f"refusing {len(unnamed)} pinned entr(y/ies) with no name", file=sys.stderr)
    if args.record is not None or args.confirm_deleted is not None or args.registered is not None:
        try:
            if args.record is not None:
                name, job_id = args.record
                entry = next((e for e in view if e.get("name") == name), None)
                if entry is None or not entry.get("registered_prompt"):
                    raise ListingError(f"no pinned entry named {name!r} to record")
                record(args.registry, entry, entry["registered_prompt"], job_id)
                return 0
            if args.confirm_deleted is not None:
                confirm_deleted(args.registry, *args.confirm_deleted)
                return 0
            view = plan_cron_actions(view, load_jobs(args.registered), load_registry(args.registry))
        except ListingError as exc:
            print(f"{exc}; register nothing", file=sys.stderr)
            return 3
    print(json.dumps(view))
    return 0


if __name__ == "__main__":
    sys.exit(main())
