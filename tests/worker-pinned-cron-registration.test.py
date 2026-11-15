#!/usr/bin/env python3
"""A worker's pinned crons register once even when the Mod's watcher answered `skip`."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cpg = load("cron_prompt_gate", "src/cron_prompt_gate.py")
own = load("cron_ownership", "src/cron_ownership.py")
FAILURES = []


def check(name, cond, extra=""):
    print(("  ok  " if cond else "FAIL  ") + name + ("" if cond else f"  {extra}"))
    if not cond:
        FAILURES.append(name)


def cli(crons, owner, listing=None):
    with tempfile.TemporaryDirectory() as d:
        cf = Path(d) / "crons.json"
        cf.write_text(json.dumps(crons))
        args = [str(cf), "--owner", owner]
        if listing is not None:
            lf = Path(d) / "listing.txt"
            lf.write_text(listing)
            args += ["--registered", str(lf)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cpg.main(args)
    return rc, json.loads(out.getvalue())


def main() -> int:
    W = "d1fc9b10050b490d894947f96627447f"
    pinned = {"name": "event-mining-hourly", "cron": "7 * * * *", "prompt": "mine events", "owner": W}
    other = {"name": "other", "cron": "57 6 * * *", "prompt": "briefing"}
    crons = [pinned, other]

    # (a) Mod watcher up, one pinned entry, nothing registered -> registered once
    rc, view = cli(crons, W, listing="")
    check("(a) pinned entry is returned when CronList is empty",
          rc == 0 and [e["name"] for e in view] == ["event-mining-hourly"], view)
    prompt = view[0]["registered_prompt"]

    # (b) repeat startup: the session's CronList now shows it -> nothing more to register
    rc, view2 = cli(crons, W, listing=f"1  7 * * * *  {prompt}\n")
    check("(b) repeat run with the job listed registers nothing", rc == 0 and view2 == [], view2)
    check("(b) whitespace in the listing does not defeat the match",
          cli(crons, W, listing=" ".join(prompt.split()).replace(" ", "\n  "))[1] == [])
    check("(b) a different job in the listing does not hide it",
          len(cli(crons, W, listing="1  0 9 * * *  something else")[1]) == 1)
    check("without --registered the view is unchanged", len(cli(crons, W)[1]) == 1)

    # (c) the core view still excludes the worker-owned entry
    rc, core = cli(crons, own.CORE, listing="")
    check("(c) core view excludes the worker-pinned entry",
          [e["name"] for e in core] == ["other"], core)

    check("an entry with no registered prompt (monitor) is never dropped",
          cpg.not_yet_registered([{"name": "m", "registered_prompt": None}], "x")
          == [{"name": "m", "registered_prompt": None}])

    text = (REPO / "skills/startup/SKILL.md").read_text()
    step = text[text.index("2.5. "):text.index("3. Report what actually happened")]
    check("SKILL step 2.5 runs on start or skip, not unknown",
          "On `start` or `skip`" in step and "never `unknown`" in step)
    check("SKILL step 2.5 filters with CronList via --registered",
          "--registered" in step and "CronList" in step)

    print("FAILED" if FAILURES else "all passed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
