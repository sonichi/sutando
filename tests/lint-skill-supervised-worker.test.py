#!/usr/bin/env python3
"""scripts/lint-skill.py's validation of a skill's `supervised_worker` block.

Lives under tests/ rather than beside the linter because that is the tree the
coverage gate instruments: `scripts/lint-skill.test.py` is run by its own CI
step, which is not measured, so validation tested only there reads as untested.

What it pins: sparrowd starts a skill's daemon from this declaration and names
no skill itself, so a malformed block is a daemon that silently never runs. The
linter is the only thing that sees it before the supervisor does.
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("lint_skill", REPO / "scripts" / "lint-skill.py")
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)

FAILS = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILS.append(label)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lint-sw-"))
    n = [0]

    def lint_worker(worker, config=None, script_body="# a worker\n"):
        """A minimal valid skill carrying this supervised_worker block."""
        n[0] += 1
        d = tmp / f"sw{n[0]}"
        d.mkdir(parents=True, exist_ok=True)
        if script_body is not None:
            (d / "loop.py").write_text(script_body, encoding="utf-8")
        (d / "manifest.json").write_text(json.dumps({
            "name": f"sw{n[0]}", "version": "1.0.0", "owner": "m", "stability": "stable",
            "config": {"SW_PYTHON": ""} if config is None else config,
            "supervised_worker": worker,
        }), encoding="utf-8")
        return lint._lint_manifest(d)

    good = {"name": "sw-loop", "script": "loop.py",
            "interpreter": {"config": "SW_PYTHON", "needs": "nothing"}}

    e, w = lint_worker(good)
    check("a well-formed declaration is accepted", e == [] and w == [], f"{e} {w}")

    e, _ = lint_worker("a string")
    check("a declaration that is not an object is an error",
          any("must be an object" in x for x in e), str(e))

    e, _ = lint_worker({**good, "name": "../../etc/passwd"})
    check("a name that is a path is an error", any("plain name" in x for x in e), str(e))

    e, _ = lint_worker({**good, "name": ""})
    check("an empty name is an error", any("plain name" in x for x in e), str(e))

    e, _ = lint_worker({k: v for k, v in good.items() if k != "script"})
    check("a missing script is an error", any("script is required" in x for x in e), str(e))

    e, _ = lint_worker({**good, "script": "../../src/sparrowd.py"})
    check("a script outside the skill is an error", any("inside the skill" in x for x in e), str(e))

    e, _ = lint_worker({**good, "script": "/usr/bin/python3"})
    check("an absolute script path is an error", any("inside the skill" in x for x in e), str(e))

    e, _ = lint_worker(good, script_body=None)
    check("a script that does not exist is an error",
          any("does not exist" in x for x in e), str(e))

    e, _ = lint_worker({k: v for k, v in good.items() if k != "interpreter"})
    check("a missing interpreter.config is an error",
          any("interpreter.config is required" in x for x in e), str(e))

    e, _ = lint_worker({**good, "interpreter": {"needs": "nothing"}})
    check("an interpreter block without config is an error",
          any("interpreter.config is required" in x for x in e), str(e))

    _, w = lint_worker(good, config={})
    check("an interpreter key absent from the config block warns",
          any("not declared in the config block" in x for x in w), str(w))

    _, w = lint_worker({**good, "flavour": "x"})
    check("an unknown field in the block warns",
          any("unknown supervised_worker field" in x for x in w), str(w))

    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all supervised_worker lint checks ok'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
