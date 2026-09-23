#!/usr/bin/env python3
"""sparrowd's decision about a worker a SKILL declares.

Two things are pinned. First, the refusals: these loops may need packages the
core's own interpreter does not have, so the question is not "does it start"
but "does it REFUSE cleanly" — started under the wrong python a worker
crash-loops under the supervisor, which reads as a broken daemon rather than a
missing setting.

Second, and the reason this file is not named after one skill: the discovery
must name NO skill. A skill is optional and self-contained, so the core cannot
know which ones exist. The synthetic skill below is never referenced from
src/, and it must be supervised anyway.
"""
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FAILS = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILS.append(label)


def load():
    spec = importlib.util.spec_from_file_location("sparrowd_under_test", REPO / "src" / "sparrowd.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def find(specs, name):
    return next((s for s in specs if s.name == name), None)


def main() -> int:
    mod = load()
    check("core names no concrete skill",
          "room-collab" not in (REPO / "src" / "sparrowd.py").read_text(encoding="utf-8"))

    skill = REPO / "skills" / "zz-synthetic-worker-skill"
    keep = os.environ.pop("SYNTHETIC_WORKER_PYTHON", None)
    try:
        (skill / "scripts").mkdir(parents=True, exist_ok=True)
        (skill / "scripts" / "loop.py").write_text("# a worker\n", encoding="utf-8")
        manifest = skill / "manifest.json"

        def declare(**over):
            decl = {"name": "synthetic-worker", "script": "scripts/loop.py",
                    "interpreter": {"config": "SYNTHETIC_WORKER_PYTHON", "needs": "nothing"}}
            decl.update(over.pop("worker", {}))
            body = {"config": over.pop("config", {}), "supervised_worker": decl}
            body.update(over)
            manifest.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")

        print("── unconfigured ──")
        declare()
        specs, skipped = mod._skill_worker_specs()
        check("no interpreter configured means NO worker", find(specs, "synthetic-worker") is None)
        check("...and the reason names the setting to add",
              any("SYNTHETIC_WORKER_PYTHON" in s for s in skipped), str(skipped))
        check("...and what the interpreter needs", any("needs nothing" in s for s in skipped),
              str(skipped))
        names = [w.name for w in mod.worker_specs()]
        check("the core's own worker is unaffected", "remote-gateway-bridge" in names)

        print("── configured but not a real file ──")
        os.environ["SYNTHETIC_WORKER_PYTHON"] = "/nonexistent/python3"
        specs, skipped = mod._skill_worker_specs()
        check("an interpreter that does not exist is refused", find(specs, "synthetic-worker") is None)
        check("...saying so, rather than falling back to sys.executable",
              any("does not exist" in s for s in skipped), str(skipped))

        print("── configured ──")
        os.environ["SYNTHETIC_WORKER_PYTHON"] = sys.executable
        specs, skipped = mod._skill_worker_specs()
        spec = find(specs, "synthetic-worker")
        check("a real interpreter produces the worker", spec is not None)
        if spec is not None:
            check("...running THAT interpreter, not the core's by accident",
                  spec.argv[0] == sys.executable)
            check("...on the declared script", spec.argv[1].endswith("scripts/loop.py"))
            check("it joins the core's worker rather than replacing it",
                  [w.name for w in mod.worker_specs()][0] == "remote-gateway-bridge")

        print("── the manifest is a configured source, not only the env ──")
        os.environ.pop("SYNTHETIC_WORKER_PYTHON", None)
        declare(config={"SYNTHETIC_WORKER_PYTHON": sys.executable})
        specs, _ = mod._skill_worker_specs()
        check("an interpreter named in the manifest is honoured",
              find(specs, "synthetic-worker") is not None)

        print("── a manifest is attacker-adjacent ──")
        os.environ["SYNTHETIC_WORKER_PYTHON"] = sys.executable
        declare(worker={"script": "../../src/sparrowd.py"})
        specs, skipped = mod._skill_worker_specs()
        check("a script outside the skill is refused",
              find(specs, "synthetic-worker") is None and any("inside" in s for s in skipped),
              str(skipped))
        declare(worker={"name": "../../etc/passwd"})
        specs, skipped = mod._skill_worker_specs()
        check("a worker name that is a path is refused",
              not any(s.name.startswith("..") for s in specs), str(skipped))
        declare(worker={"script": None})
        specs, skipped = mod._skill_worker_specs()
        check("a declaration with no script is refused",
              find(specs, "synthetic-worker") is None
              and any("script is missing" in s for s in skipped), str(skipped))
        declare(worker={"interpreter": "not-an-object"})
        specs, skipped = mod._skill_worker_specs()
        check("a declaration with no interpreter.config is refused",
              find(specs, "synthetic-worker") is None
              and any("interpreter.config is missing" in s for s in skipped), str(skipped))
        manifest.write_text("{ not json", encoding="utf-8")
        specs, skipped = mod._skill_worker_specs()
        check("an unreadable manifest is skipped, not raised",
              any("unreadable" in s for s in skipped), str(skipped))

        print("── a skill that declares nothing ──")
        declare_none = {"config": {}}
        manifest.write_text(json.dumps(declare_none) + "\n", encoding="utf-8")
        specs, skipped = mod._skill_worker_specs()
        check("is neither supervised nor complained about",
              find(specs, "synthetic-worker") is None
              and not any("synthetic" in s for s in skipped), str(skipped))
    finally:
        shutil.rmtree(skill, ignore_errors=True)
        os.environ.pop("SYNTHETIC_WORKER_PYTHON", None)
        if keep is not None:
            os.environ["SYNTHETIC_WORKER_PYTHON"] = keep

    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all sparrowd skill-worker checks ok'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
