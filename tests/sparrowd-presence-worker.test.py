#!/usr/bin/env python3
"""sparrowd's decision about the room-collab presence daemon.

The daemon imports pycrdt and websockets, which the core's own interpreter is
not required to have. So the question this file pins is not "does it start" but
"does it REFUSE cleanly" — a worker started under the wrong python crash-loops
under the supervisor, which is worse than an absent one.
"""
import importlib.util
import os
import sys
import tempfile
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


def main() -> int:
    mod = load()
    keep = os.environ.pop("ROOM_COLLAB_PYTHON", None)
    try:
        print("── unconfigured ──")
        spec, why = mod._presence_daemon_spec()
        check("no interpreter configured means NO worker", spec is None)
        check("...and the reason names what to set", "ROOM_COLLAB_PYTHON" in (why or ""), str(why))
        names = [w.name for w in mod.worker_specs()]
        check("the core's own worker is unaffected", "remote-gateway-bridge" in names)
        check("...and the presence worker is absent", "room-collab-presence" not in names)

        print("── configured but not a real file ──")
        os.environ["ROOM_COLLAB_PYTHON"] = "/nonexistent/python3"
        spec, why = mod._presence_daemon_spec()
        check("a configured interpreter that does not exist is refused", spec is None)
        check("...saying so, rather than falling back to sys.executable",
              "does not exist" in (why or ""), str(why))

        print("── configured ──")
        os.environ["ROOM_COLLAB_PYTHON"] = sys.executable
        spec, why = mod._presence_daemon_spec()
        check("a real interpreter produces the worker", spec is not None and why is None)
        if spec is not None:
            check("...named for what it supervises", spec.name == "room-collab-presence")
            check("...running THAT interpreter, not the core's by accident",
                  spec.argv[0] == sys.executable)
            check("...on the daemon script", spec.argv[1].endswith("presence_daemon.py"))
            check("it joins the core's worker rather than replacing it",
                  [w.name for w in mod.worker_specs()]
                  == ["remote-gateway-bridge", "room-collab-presence"])
        print("── the manifest is the configured source, not only the env ──")
        # An operator sets this in the skill's manifest; reading it only from
        # the env would make the documented place the one that does not work.
        os.environ.pop("ROOM_COLLAB_PYTHON", None)
        man = REPO / "skills" / "room-collab" / "manifest.json"
        keep_manifest = man.read_text(encoding="utf-8")
        try:
            import json as _json
            cfg = _json.loads(keep_manifest)
            cfg.setdefault("config", {})["ROOM_COLLAB_PYTHON"] = sys.executable
            man.write_text(_json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            spec, why = mod._presence_daemon_spec()
            check("an interpreter named in the manifest is honoured",
                  spec is not None and why is None, str(why))
            cfg["config"]["ROOM_COLLAB_PYTHON"] = "{"          # not valid JSON below
            man.write_text("{ not json", encoding="utf-8")
            spec, why = mod._presence_daemon_spec()
            check("an unreadable manifest refuses rather than raising", spec is None)
        finally:
            man.write_text(keep_manifest, encoding="utf-8")

        print("── the skill absent entirely ──")
        # The core must boot without room-collab installed at all.
        script = REPO / "skills" / "room-collab" / "scripts" / "presence_daemon.py"
        moved = script.with_suffix(".py.hidden-for-test")
        script.rename(moved)
        try:
            spec, why = mod._presence_daemon_spec()
            check("no skill means no worker", spec is None)
            check("...and the reason says so", "not installed" in (why or ""), str(why))
        finally:
            moved.rename(script)

    finally:
        os.environ.pop("ROOM_COLLAB_PYTHON", None)
        if keep is not None:
            os.environ["ROOM_COLLAB_PYTHON"] = keep

    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all sparrowd-presence checks ok'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
