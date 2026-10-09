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


def workspace_skills(mod):
    """A skill in <workspace>/skills/ is supervised like a shipped one; a shipped
    skill of the same name wins, as in skills/install.sh."""
    import tempfile
    print("── a workspace skill ──")
    saved = {k: os.environ.get(k) for k in ("SUTANDO_TEST_MODE", "SUTANDO_WORKSPACE", "WS_FIXTURE_PYTHON")}
    with tempfile.TemporaryDirectory() as tmp:
        ws, elsewhere = Path(tmp) / "workspace", Path(tmp) / "other-checkout" / "skills"
        def put(dir_name, worker):
            skill = elsewhere / dir_name
            (skill / "scripts").mkdir(parents=True, exist_ok=True)
            (skill / "scripts" / "loop.py").write_text("# a worker\n", encoding="utf-8")
            (skill / "manifest.json").write_text(json.dumps({"enabled": True, "supervised_worker": {
                "name": worker, "script": "scripts/loop.py",
                "interpreter": {"config": "WS_FIXTURE_PYTHON"}}}), encoding="utf-8")
            (ws / "skills").mkdir(parents=True, exist_ok=True)
            (ws / "skills" / dir_name).symlink_to(skill)
        put("zz-ws-fixture-skill", "ws-fixture-worker")
        shipped = next(p.parent.name for p in sorted((REPO / "skills").glob("*/manifest.json")))
        put(shipped, "ws-shadow-worker")
        os.environ.update(SUTANDO_TEST_MODE="1", SUTANDO_WORKSPACE=str(ws), WS_FIXTURE_PYTHON=sys.executable)
        try:
            specs, skipped = mod._skill_worker_specs()
            spec = find(specs, "ws-fixture-worker")
            check("a workspace skill's declared worker is supervised", spec is not None, str(skipped))
            if spec is not None:
                check("...running the script inside that skill (through its symlink)",
                      Path(spec.argv[1]) == (elsewhere / "zz-ws-fixture-skill" / "scripts" / "loop.py").resolve())
            check("a workspace skill named like a shipped skill is not supervised",
                  find(specs, "ws-shadow-worker") is None)
            os.environ.pop("WS_FIXTURE_PYTHON")
            _, skipped = mod._skill_worker_specs()
            check("an unconfigured workspace worker names its own manifest",
                  any("ws-fixture-worker" in s and str(ws) in s for s in skipped), str(skipped))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def outside_the_engine(mod):
    """A skill in a sibling checkout's skills/ or an external plugin dir is supervised too;
    the workspace comes first, and a shipped skill still wins."""
    import tempfile
    print("── sibling-checkout and external-dir skills ──")
    keys = ("SUTANDO_EXTERNAL_PLUGIN_DIRS", "SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR", "WS_FIXTURE_PYTHON")
    saved, saved_repo = {k: os.environ.get(k) for k in keys}, mod.REPO
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        engine, ws = tmp / "engine" / "sutando", tmp / "workspace"
        sibling, external = tmp / "engine" / "neighbor" / "skills", tmp / "plugins" / "extra"
        def put(base, dir_name, worker):
            skill = base / dir_name
            (skill / "scripts").mkdir(parents=True, exist_ok=True)
            (skill / "scripts" / "loop.py").write_text("# a worker\n", encoding="utf-8")
            (skill / "manifest.json").write_text(json.dumps({"enabled": True, "supervised_worker": {
                "name": worker, "script": "scripts/loop.py",
                "interpreter": {"config": "WS_FIXTURE_PYTHON"}}}), encoding="utf-8")
        (engine / "skills" / "shipped-fixture").mkdir(parents=True)
        (engine / "skills" / "shipped-fixture" / "SKILL.md").write_text("# shipped\n", encoding="utf-8")
        (ws / "skills").mkdir(parents=True)
        put(sibling, "sibling-fixture", "sibling-worker")
        put(sibling, "shipped-fixture", "sibling-shadow-worker")
        put(ws / "skills", "both-places", "ws-first-worker")
        put(sibling, "both-places", "sibling-second-worker")
        put(external / "skills", "external-fixture", "external-worker")
        mod.REPO = engine
        os.environ.update(SUTANDO_EXTERNAL_PLUGIN_DIRS=str(external), WS_FIXTURE_PYTHON=sys.executable)
        for k in ("SUTANDO_MEMORY_DIR", "SUTANDO_PRIVATE_DIR"):
            os.environ.pop(k, None)
        try:
            order = [m.parent.name for m in mod._skill_manifests(workspace=ws)]
            check("roots are scanned workspace, external dir, then sibling checkout",
                  order == ["both-places", "external-fixture", "sibling-fixture"], str(order))
            real = mod._skill_manifests
            mod._skill_manifests = lambda: real(workspace=ws)
            try:
                specs, skipped = mod._skill_worker_specs()
            finally:
                mod._skill_manifests = real
            names = {s.name for s in specs}
            check("a sibling checkout's skill worker is supervised", "sibling-worker" in names, str(skipped))
            check("an external plugin dir's skill worker is supervised", "external-worker" in names, str(skipped))
            spec = find(specs, "sibling-worker")
            check("...running the script inside the sibling skill", spec is not None and
                  Path(spec.argv[1]) == (sibling / "sibling-fixture" / "scripts" / "loop.py").resolve())
            check("a shipped skill folder shadows a sibling's", "sibling-shadow-worker" not in names)
            check("the workspace copy wins over a sibling's", "ws-first-worker" in names
                  and "sibling-second-worker" not in names, str(names))
        finally:
            mod.REPO = saved_repo
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def guarded_scan(mod):
    """An unreadable root is skipped, a leftover folder claims no name, and a worker whose
    manifest is not enabled is not started (the voice loader's gate)."""
    import tempfile
    print("── guards ──")
    saved_repo, saved_py = mod.REPO, os.environ.get("WS_FIXTURE_PYTHON")
    saved_ext = os.environ.get("SUTANDO_EXTERNAL_PLUGIN_DIRS")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        engine, ws, ext, locked = tmp / "engine" / "sutando", tmp / "workspace", tmp / "ext", tmp / "locked"
        (engine / "skills").mkdir(parents=True)
        def put(base, dir_name, worker, enabled=True):
            skill = base / dir_name
            (skill / "scripts").mkdir(parents=True, exist_ok=True)
            (skill / "scripts" / "loop.py").write_text("# a worker\n", encoding="utf-8")
            body = {"supervised_worker": {"name": worker, "script": "scripts/loop.py",
                                          "interpreter": {"config": "WS_FIXTURE_PYTHON"}}}
            if enabled is not None:
                body["enabled"] = enabled
            (skill / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
        (ws / "skills" / "leftover" / "__pycache__").mkdir(parents=True)
        put(ext / "skills", "leftover", "leftover-worker")
        put(ext / "skills", "off-skill", "off-worker", enabled=False)
        put(ext / "skills", "unset-skill", "unset-worker", enabled=None)
        put(locked / "skills", "hidden", "hidden-worker")
        mod.REPO = engine
        os.environ.update(SUTANDO_EXTERNAL_PLUGIN_DIRS=os.pathsep.join([str(locked), str(ext)]),
                          WS_FIXTURE_PYTHON=sys.executable)
        (locked / "skills").chmod(0)
        try:
            try:
                found, raised = [m.parent.name for m in mod._skill_manifests(workspace=ws)], None
            except OSError as exc:
                found, raised = [], exc
            check("an unreadable skill root is skipped, not fatal", raised is None, str(raised))
            check("a leftover folder without a skill does not claim the name, and later roots still count",
                  "leftover" in found, str(found))
            real = mod._skill_manifests
            mod._skill_manifests = lambda: real(workspace=ws)
            try:
                specs, skipped = mod._skill_worker_specs()
            finally:
                mod._skill_manifests = real
            names = {s.name for s in specs}
            check("a worker whose manifest says enabled: false is not started", "off-worker" not in names)
            check("...nor one whose manifest has no enabled", "unset-worker" not in names)
            check("...and the reason says so", any("not enabled" in r for r in skipped), str(skipped))
            check("an enabled worker is still started", "leftover-worker" in names, str(skipped))
        finally:
            (locked / "skills").chmod(0o755)
            mod.REPO = saved_repo
            for k, v in (("SUTANDO_EXTERNAL_PLUGIN_DIRS", saved_ext), ("WS_FIXTURE_PYTHON", saved_py)):
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def locked_skill_folder(mod):
    """One unreadable skill folder inside a readable root is skipped; its siblings still load."""
    import tempfile
    print("── an unreadable skill folder ──")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("  skip: running as root, chmod 000 does not restrict")
        return
    saved_repo, saved_ext = mod.REPO, os.environ.get("SUTANDO_EXTERNAL_PLUGIN_DIRS")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        engine, ws = tmp / "engine" / "sutando", tmp / "workspace"
        (engine / "skills").mkdir(parents=True)
        for name in ("good", "lockedskill"):
            (ws / "skills" / name).mkdir(parents=True)
            (ws / "skills" / name / "manifest.json").write_text("{}", encoding="utf-8")
        locked = ws / "skills" / "lockedskill"
        mod.REPO = engine
        os.environ.pop("SUTANDO_EXTERNAL_PLUGIN_DIRS", None)
        locked.chmod(0)
        try:
            try:
                (locked / "manifest.json").is_file()
                probe_raises = False
            except OSError:
                probe_raises = True
            if not probe_raises and os.access(locked, os.R_OK | os.X_OK):
                print("  skip: chmod 000 does not restrict on this platform")
                return
            print(f"  (this python {'raises' if probe_raises else 'does not raise'} on a locked folder's probe)")
            try:
                found, raised = [m.parent.name for m in mod._skill_manifests(workspace=ws)], None
            except OSError as exc:
                found, raised = [], exc
            check("an unreadable skill folder is skipped, not fatal", raised is None, repr(raised))
            check("...and the readable sibling skill still loads", found == ["good"], str(found))
        finally:
            locked.chmod(0o755)
            mod.REPO = saved_repo
            if saved_ext is not None:
                os.environ["SUTANDO_EXTERNAL_PLUGIN_DIRS"] = saved_ext


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
            body = {"enabled": True, "config": over.pop("config", {}), "supervised_worker": decl}
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

    workspace_skills(mod)
    outside_the_engine(mod)
    guarded_scan(mod)
    locked_skill_folder(mod)

    print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all sparrowd skill-worker checks ok'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
