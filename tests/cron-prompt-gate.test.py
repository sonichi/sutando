#!/usr/bin/env python3
"""Unit tests for src/cron_prompt_gate.py — the registered-prompt builder that
puts the owner-task gate in front of sub-daily session crons by default."""
import contextlib
import importlib.util
import io
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


def run_main(*args):
    """main() in-process, so the coverage gate measures the CLI's own lines."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cpg.main(list(args))
    return rc, out.getvalue(), err.getvalue()


def main() -> int:
    loop = {"name": "main-loop", "cron": "*/15 * * * *", "prompt_skill": "proactive-loop"}
    briefing = {"name": "morning-briefing", "cron": "57 6 * * *",
                "prompt": "Morning briefing. Run python3 src/morning-briefing.py"}
    sweep = {"name": "inbox-sweep", "cron": "13,43 * * * *", "prompt": "Sweep the inbox and file what is new"}
    skill_cron = {"name": "people-pass", "cron": "0 */3 * * *", "prompt_skill": "people-analysis"}
    daily_skill = {"name": "dream", "cron": "37 3 * * *", "prompt_skill": "obsidian-dream"}
    opted_out = {"name": "heartbeat", "cron": "*/5 * * * *", "prompt": "ping", "gate": False}
    hand_wrapped = {"name": "sync-workspace", "cron": "*/30 * * * *",
                    "prompt": "Run: bash scripts/cron-gate.sh sync-workspace bash scripts/sync-workspace.sh"}
    monitor = {"name": "content-loop", "monitor": {"command": "x", "description": "d", "match": "m"}}
    worker_entry = {"name": "event-mining-hourly", "cron": "7 * * * *", "prompt": "mine",
                    "owner": "d1fc9b10050b490d894947f96627447f"}
    loop_by_prompt = {"name": "custom-loop", "cron": "*/5 * * * *", "prompt": "Run /proactive-loop now"}

    # ── the wrap ─────────────────────────────────────────────────────────
    rp = cpg.registered_prompt(sweep)
    check("a sub-daily prompt entry is gated", cpg.is_gated(sweep))
    check("gated prompt opens with the --check preamble",
          rp.startswith("First run `bash scripts/cron-gate.sh --check inbox-sweep`"), rp)
    check("gated prompt names deferred and clear",
          "`deferred`" in rp and "`clear`" in rp, rp)
    check("gated prompt keeps the original text verbatim at the end",
          rp.endswith(sweep["prompt"]), rp)

    rp_skill = cpg.registered_prompt(skill_cron)
    check("a sub-daily prompt_skill entry is gated", cpg.is_gated(skill_cron))
    check("gated prompt_skill keeps the /skill invocation",
          rp_skill.endswith("invoke the skill /people-analysis"), rp_skill)
    check("gated prompt_skill reason is the entry name",
          "--check people-pass`" in rp_skill, rp_skill)

    # ── the default follows the cron cadence ─────────────────────────────
    table = [
        # sub-daily: more than one (minute, hour) pair
        ("*/30 * * * *", True), ("* * * * *", True), ("0 * * * *", True),
        ("13,43 * * * *", True), ("0 9,17 * * *", True), ("0 9-17 * * 1-5", True),
        ("0-5 6 * * *", True), ("0 */6 * * *", True), ("5/15 3 * * *", True),
        ("*/60 * * * *", True),          # one minute value, every hour: hourly
        ("0,0 8,9 * * *", True),
        # daily or rarer: one minute, one hour, any day fields
        ("57 6 * * *", False), ("0 9 * * 1-5", False), ("0 9 * * 1", False),
        ("30 7 1 * *", False), ("0 0 25 12 *", False), ("0 */24 * * *", False),
        ("*/60 */24 * * *", False), ("0-0 5 * * *", False), ("7,7 9-9 * * *", False),
        ("  57   6 * * *  ", False),
        # unparseable: gated, since gating only delays
        ("@daily", True), ("", True), ("* * * *", True), ("* * *", True),
        ("61 * * * *", True), ("a b c d e", True), ("0 0 * * * *", True),
        ("x 6 * * *", True), ("0 y * * *", True), ("60 6 * * *", True), ("0 24 * * *", True),
        ("*/0 * * * *", True), ("*/x * * * *", True), ("5-1 6 * * *", True),
        ("0-x 6 * * *", True), ("0,,5 6 * * *", True), ("-5 6 * * *", True),
    ]
    for expr, want in table:
        e = {"name": "t", "cron": expr, "prompt": "do the thing"}
        check(f"cron {expr!r} is {'gated' if want else 'not gated'} by default",
              cpg.gated_by_default(e) is want and cpg.is_gated(e) is want,
              (cpg.fires_per_day(e), cpg.is_gated(e)))
    check("fires_per_day multiplies minute and hour values",
          cpg.fires_per_day({"cron": "13,43 9-17 * * *"}) == 18
          and cpg.fires_per_day({"cron": "57 6 * * *"}) == 1)
    check("fires_per_day is None for an unparseable or absent cron",
          cpg.fires_per_day({"cron": "@daily"}) is None and cpg.fires_per_day({}) is None)
    for bad in (None, 5, ["57 6 * * *"]):
        check(f"a non-string cron ({bad!r}) is gated, not a crash",
              cpg.is_gated({"name": "t", "cron": bad, "prompt": "p"}))
    check("an entry with no cron (a dynamic loop) stays gated",
          cpg.is_gated({"name": "inbox-score", "prompt_skill": "inbox-score", "loop": "dynamic"}))

    check("a daily prompt entry is not gated", not cpg.is_gated(briefing))
    check("a daily prompt registers verbatim",
          cpg.registered_prompt(briefing) == briefing["prompt"])
    check("a daily prompt_skill entry registers as the bare slash command",
          not cpg.is_gated(daily_skill) and cpg.registered_prompt(daily_skill) == "/obsidian-dream")

    # ── gate overrides the cadence default either way ────────────────────
    forced = dict(briefing, gate=True)
    check("gate: true forces a daily to be gated", cpg.is_gated(forced))
    check("a forced daily carries the preamble and its prompt",
          cpg.registered_prompt(forced).startswith(
              "First run `bash scripts/cron-gate.sh --check morning-briefing`")
          and cpg.registered_prompt(forced).endswith(briefing["prompt"]))
    check("gate: false forces a sub-daily to be ungated",
          not cpg.is_gated(dict(sweep, gate=False))
          and cpg.registered_prompt(dict(sweep, gate=False)) == sweep["prompt"])
    check("gate: true on a sub-daily changes nothing", cpg.registered_prompt(dict(sweep, gate=True)) == rp)
    check("a non-bool gate value falls back to the cadence default",
          cpg.is_gated(dict(sweep, gate="false")) and not cpg.is_gated(dict(briefing, gate="true")))
    check("gate: true never gates the main loop", not cpg.is_gated(dict(loop, gate=True)))
    check("gate: true never double-wraps", not cpg.is_gated(dict(hand_wrapped, gate=True)))
    watchdog = {"name": "core-watchdog", "cron": "*/10 * * * *", "gate": False,
                "prompt": "Run python3 src/health-check.py and report anything red"}
    check("a watchdog with gate: false registers verbatim",
          cpg.registered_prompt(watchdog) == watchdog["prompt"])

    # ── exemptions ───────────────────────────────────────────────────────
    check("main-loop is never gated", not cpg.is_gated(loop))
    check("main-loop registers as the bare slash command",
          cpg.registered_prompt(loop) == "/proactive-loop", cpg.registered_prompt(loop))
    check("a prompt_skill of proactive-loop is the loop whatever its name",
          not cpg.is_gated({"name": "x", "prompt_skill": "proactive-loop"}))
    loop_by_name = {"name": "main-loop", "cron": "*/15 * * * *", "prompt": "Run the loop pass"}
    check("an entry named main-loop is the loop whatever its prompt",
          cpg.is_main_loop(loop_by_name) and not cpg.is_gated(loop_by_name))
    check("an entry named main-loop registers verbatim",
          cpg.registered_prompt(loop_by_name) == "Run the loop pass")
    check("a prompt invoking /proactive-loop is the loop", not cpg.is_gated(loop_by_prompt))
    check("a prompt invoking /proactive-loop registers verbatim",
          cpg.registered_prompt(loop_by_prompt) == loop_by_prompt["prompt"])
    loop_script = {"name": "loop-digest", "cron": "0 * * * *",
                   "prompt": "Run python3 skills/proactive-loop/scripts/quota-tier.py and report"}
    check("a prompt naming a proactive-loop script path is not the loop",
          not cpg.is_main_loop(loop_script) and cpg.is_gated(loop_script))
    check("a longer slash command sharing the prefix is not the loop",
          not cpg.is_main_loop({"name": "x", "prompt": "/proactive-loop-report"}))
    check("the bare slash command with an interval is the loop",
          cpg.is_main_loop({"name": "x", "prompt": "/proactive-loop 10m"}))

    check("gate: false opts out", not cpg.is_gated(opted_out))
    check("gate: false registers the prompt verbatim",
          cpg.registered_prompt(opted_out) == "ping")

    # ── no double wrap ───────────────────────────────────────────────────
    check("a prompt already calling cron-gate.sh is left alone", not cpg.is_gated(hand_wrapped))
    check("hand-wrapped prompt registers verbatim",
          cpg.registered_prompt(hand_wrapped) == hand_wrapped["prompt"])
    twice = cpg.registered_prompt({"name": "inbox-sweep", "cron": sweep["cron"], "prompt": rp})
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
    entries = [loop, briefing, sweep, worker_entry, monitor]
    core_view = cpg.registration_view(entries, cpg.CORE)
    check("core view skips worker-owned entries",
          [e["name"] for e in core_view]
          == ["main-loop", "morning-briefing", "inbox-sweep", "content-loop"],
          [e["name"] for e in core_view])
    check("core view carries registered_prompt per entry",
          core_view[0]["registered_prompt"] == "/proactive-loop"
          and core_view[1]["registered_prompt"] == briefing["prompt"]
          and core_view[2]["registered_prompt"] == rp
          and core_view[3]["registered_prompt"] is None)
    check("view does not mutate the source entries", "registered_prompt" not in briefing)
    worker_view = cpg.registration_view(entries, "d1fc9b10050b490d894947f96627447f")
    check("a worker's own sub-daily entry is gated too",
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
              == ["main-loop", "morning-briefing", "inbox-sweep", "content-loop"], got)
        check("CLI output gates the sub-daily entry and leaves the daily verbatim",
              got[1]["registered_prompt"] == briefing["prompt"] and got[2]["registered_prompt"] == rp)
        out = subprocess.run([sys.executable, str(REPO / "src" / "cron_prompt_gate.py"), str(cf),
                              "--owner", "d1fc9b10050b490d894947f96627447f"],
                             capture_output=True, text=True, check=True).stdout
        check("CLI --owner prints a worker's view",
              [e["name"] for e in json.loads(out)] == ["event-mining-hourly"])
        out = subprocess.run([sys.executable, str(REPO / "src" / "cron_prompt_gate.py"),
                              str(Path(td) / "missing.json")],
                             capture_output=True, text=True, check=True).stdout
        check("CLI on a missing file prints an empty list", json.loads(out) == [])

        rc, out, err = run_main(str(cf))
        check("main returns 0 and prints the core view",
              rc == 0 and err == "" and json.loads(out) == got, (rc, out, err))
        rc, out, _ = run_main(str(cf), "--owner", "d1fc9b10050b490d894947f96627447f")
        check("main --owner prints a worker's view, gate included",
              rc == 0 and [e["registered_prompt"] for e in json.loads(out)]
              == [cpg.registered_prompt(worker_entry)], (rc, out))
        rc, out, err = run_main(str(Path(td) / "missing.json"))
        check("main on a missing file returns 0 with an empty list",
              (rc, json.loads(out), err) == (0, [], ""), (rc, out, err))
        not_a_list = Path(td) / "object.json"
        not_a_list.write_text(json.dumps({"crons": entries}))
        rc, out, err = run_main(str(not_a_list))
        check("main rejects a crons.json that is not a list with exit 2",
              rc == 2 and out == "" and "expected a JSON list" in err, (rc, out, err))

    # ── the shipped example behaves as documented ────────────────────────
    example = json.loads((REPO / "skills" / "schedule-crons" / "crons.example.json").read_text())
    view = {e["name"]: e for e in cpg.registration_view(example, cpg.CORE)}
    for name, e in view.items():
        rp_e = e["registered_prompt"] or ""
        check(f"example entry {name} carries at most one gate",
              rp_e.count("bash scripts/cron-gate.sh") <= 1, rp_e)
    check("example main-loop stays ungated", view["main-loop"]["registered_prompt"] == "/proactive-loop")
    check("example sync-workspace (*/30) is gated at registration",
          view["sync-workspace"]["registered_prompt"].startswith(
              "First run `bash scripts/cron-gate.sh --check sync-workspace`"))
    for daily in ("morning-briefing", "learned-skills-scan", "example-digest"):
        check(f"example daily {daily} registers verbatim",
              view[daily]["registered_prompt"] == view[daily]["prompt"])
    check("every example entry is accounted for",
          set(view) == {"main-loop", "sync-workspace", "morning-briefing",
                        "learned-skills-scan", "example-digest"}, sorted(view))
    for name, e in view.items():
        if "health" in name or "watchdog" in name:
            check(f"example {name} opts out of the gate", e.get("gate") is False)

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
