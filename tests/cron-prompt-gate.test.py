#!/usr/bin/env python3
"""Unit tests for src/cron_prompt_gate.py — the registered-prompt builder that
puts the owner-task gate in front of every session cron by default."""
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "cron_prompt_gate", REPO / "src" / "cron_prompt_gate.py")
cpg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cpg)

FAILURES = []


def check(name, cond, extra=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"FAIL  {name}  {extra}")
        FAILURES.append(name)


def main() -> int:
    loop = {"name": "main-loop", "cron": "*/15 * * * *", "prompt_skill": "proactive-loop"}
    briefing = {"name": "morning-briefing", "cron": "57 6 * * *",
                "prompt": "Morning briefing. Run python3 src/morning-briefing.py"}
    skill_cron = {"name": "people-pass", "cron": "0 3 * * *", "prompt_skill": "people-analysis"}
    opted_out = {"name": "heartbeat", "cron": "*/5 * * * *", "prompt": "ping", "gate": False}
    hand_wrapped = {"name": "sync-workspace", "cron": "*/30 * * * *",
                    "prompt": "Run: bash scripts/cron-gate.sh sync-workspace bash scripts/sync-workspace.sh"}
    monitor = {"name": "content-loop", "monitor": {"command": "x", "description": "d", "match": "m"}}
    worker_entry = {"name": "event-mining-hourly", "cron": "7 * * * *", "prompt": "mine",
                    "owner": "d1fc9b10050b490d894947f96627447f"}
    loop_by_prompt = {"name": "custom-loop", "cron": "*/5 * * * *", "prompt": "Run /proactive-loop now"}

    # ── the wrap ─────────────────────────────────────────────────────────
    rp = cpg.registered_prompt(briefing)
    check("a prompt entry is gated", cpg.is_gated(briefing))
    check("gated prompt opens with the --check preamble",
          rp.startswith("First run `bash scripts/cron-gate.sh --check morning-briefing`"), rp)
    check("gated prompt names deferred and clear",
          "`deferred`" in rp and "`clear`" in rp, rp)
    check("gated prompt keeps the original text verbatim at the end",
          rp.endswith(briefing["prompt"]), rp)

    rp_skill = cpg.registered_prompt(skill_cron)
    check("a prompt_skill entry is gated", cpg.is_gated(skill_cron))
    check("gated prompt_skill keeps the /skill invocation",
          rp_skill.endswith("invoke the skill /people-analysis"), rp_skill)
    check("gated prompt_skill reason is the entry name",
          "--check people-pass`" in rp_skill, rp_skill)

    # ── exemptions ───────────────────────────────────────────────────────
    check("main-loop is never gated", not cpg.is_gated(loop))
    check("main-loop registers as the bare slash command",
          cpg.registered_prompt(loop) == "/proactive-loop", cpg.registered_prompt(loop))
    check("a prompt_skill of proactive-loop is the loop whatever its name",
          not cpg.is_gated({"name": "x", "prompt_skill": "proactive-loop"}))
    check("a prompt invoking /proactive-loop is the loop", not cpg.is_gated(loop_by_prompt))
    check("a prompt invoking /proactive-loop registers verbatim",
          cpg.registered_prompt(loop_by_prompt) == loop_by_prompt["prompt"])

    check("gate: false opts out", not cpg.is_gated(opted_out))
    check("gate: false registers the prompt verbatim",
          cpg.registered_prompt(opted_out) == "ping")
    check("gate: true is the default, spelled out",
          cpg.is_gated(dict(briefing, gate=True)))

    # ── no double wrap ───────────────────────────────────────────────────
    check("a prompt already calling cron-gate.sh is left alone", not cpg.is_gated(hand_wrapped))
    check("hand-wrapped prompt registers verbatim",
          cpg.registered_prompt(hand_wrapped) == hand_wrapped["prompt"])
    twice = cpg.registered_prompt({"name": "morning-briefing", "prompt": rp})
    check("re-applying to an already gated prompt is a no-op", twice == rp, twice)
    check("the preamble appears once", rp.count("bash scripts/cron-gate.sh") == 1)
    prose = {"name": "sync-workspace", "prompt": "Run x — no cron-gate.sh wrapper is needed here."}
    check("a prose mention of cron-gate.sh is not a wrap", cpg.is_gated(prose))

    # ── entries with nothing to register ─────────────────────────────────
    check("a monitor entry has no registered prompt", cpg.registered_prompt(monitor) is None)
    check("a monitor entry is not gated", not cpg.is_gated(monitor))

    # ── reason is a safe shell token ─────────────────────────────────────
    check("a name with spaces/quotes becomes a plain token",
          cpg.gate_reason({"name": "my cron \"x\""}) == "my-cron--x-")
    check("a nameless prompt_skill entry uses the skill as reason",
          cpg.gate_reason({"prompt_skill": "dream"}) == "dream")
    check("no name at all falls back to cron", cpg.gate_reason({}) == "cron")

    # ── registration view honours ownership ──────────────────────────────
    entries = [loop, briefing, worker_entry, monitor]
    core_view = cpg.registration_view(entries, cpg.CORE)
    check("core view skips worker-owned entries",
          [e["name"] for e in core_view] == ["main-loop", "morning-briefing", "content-loop"],
          [e["name"] for e in core_view])
    check("core view carries registered_prompt per entry",
          core_view[0]["registered_prompt"] == "/proactive-loop"
          and core_view[1]["registered_prompt"] == rp
          and core_view[2]["registered_prompt"] is None)
    check("view does not mutate the source entries", "registered_prompt" not in briefing)
    worker_view = cpg.registration_view(entries, "d1fc9b10050b490d894947f96627447f")
    check("a worker's own entry is gated too",
          len(worker_view) == 1 and worker_view[0]["registered_prompt"].endswith("mine")
          and "--check event-mining-hourly`" in worker_view[0]["registered_prompt"])

    # ── CLI ──────────────────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        cf = Path(td) / "crons.json"
        cf.write_text(json.dumps(entries))
        out = subprocess.run([sys.executable, str(REPO / "src" / "cron_prompt_gate.py"), str(cf)],
                             capture_output=True, text=True, check=True).stdout
        got = json.loads(out)
        check("CLI prints the core view", [e["name"] for e in got]
              == ["main-loop", "morning-briefing", "content-loop"], got)
        out = subprocess.run([sys.executable, str(REPO / "src" / "cron_prompt_gate.py"), str(cf),
                              "--owner", "d1fc9b10050b490d894947f96627447f"],
                             capture_output=True, text=True, check=True).stdout
        check("CLI --owner prints a worker's view",
              [e["name"] for e in json.loads(out)] == ["event-mining-hourly"])
        out = subprocess.run([sys.executable, str(REPO / "src" / "cron_prompt_gate.py"),
                              str(Path(td) / "missing.json")],
                             capture_output=True, text=True, check=True).stdout
        check("CLI on a missing file prints an empty list", json.loads(out) == [])

    # ── the shipped example never double-wraps ───────────────────────────
    example = json.loads((REPO / "skills" / "schedule-crons" / "crons.example.json").read_text())
    for e in cpg.registration_view(example, cpg.CORE):
        rp = e["registered_prompt"] or ""
        check(f"example entry {e['name']} carries at most one gate",
              rp.count("bash scripts/cron-gate.sh") <= 1, rp)
    check("example main-loop stays ungated",
          next(e for e in cpg.registration_view(example, cpg.CORE) if e["name"] == "main-loop")
          ["registered_prompt"] == "/proactive-loop")

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
