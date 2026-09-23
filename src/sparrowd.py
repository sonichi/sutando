#!/usr/bin/env python3
"""sparrowd launcher — the adapter edge that names concrete workers.

The package shell (ag2_sparrow.sparrowd) is deliberately blind to what it
supervises; THIS file owns the worker list and resolved paths, so the core
never imports or locates a repo-specific loop.
"""
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
REPO = _SRC.parent
for _p in (str(_SRC), str(REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from workspace_default import resolve_workspace  # noqa: E402
from ag2_sparrow.sparrowd import WorkerSpec, run  # noqa: E402

import re  # noqa: E402

# A worker name reaches a state-dir path and a log line; keep it a plain name.
_WORKER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _skill_worker_specs() -> "tuple[list, list[str]]":
    """Every installed skill that declares a `supervised_worker`, found by
    scanning manifests. Returns (specs, reasons-for-the-ones-skipped).

    No skill is named here. A skill is optional and self-contained, so the
    core cannot know which ones exist; it reads the declaration each one
    publishes (`skills/MANIFEST.md`) and supervises what it finds.

    A declared interpreter is required and never guessed. These loops are
    free to need packages the core's own python does not have, and started
    under the wrong one a worker crash-loops under the supervisor, which reads
    as a broken daemon rather than a missing config. Unset is a skipped worker
    with a reason.
    """
    import json

    specs, skipped = [], []
    for manifest in sorted((REPO / "skills").glob("*/manifest.json")):
        skill_dir = manifest.parent
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            skipped.append(f"{skill_dir.name}: manifest unreadable ({exc})")
            continue
        decl = data.get("supervised_worker")
        if not isinstance(decl, dict):
            continue
        name, rel = decl.get("name"), decl.get("script")
        if not isinstance(name, str) or not _WORKER_NAME.match(name):
            skipped.append(f"{skill_dir.name}: supervised_worker.name is missing or not a name")
            continue
        if not isinstance(rel, str) or not rel:
            skipped.append(f"{name}: supervised_worker.script is missing")
            continue
        # A manifest is attacker-adjacent (skills/trusted-capabilities installs
        # third-party skills), so the script must resolve inside its own skill.
        script = (skill_dir / rel).resolve()
        if not script.is_relative_to(skill_dir.resolve()) or not script.is_file():
            skipped.append(f"{name}: script is not a file inside {skill_dir.name}/")
            continue
        interp = decl.get("interpreter")
        if not isinstance(interp, dict) or not isinstance(interp.get("config"), str):
            skipped.append(f"{name}: supervised_worker.interpreter.config is missing")
            continue
        key = interp["config"]
        py = os.environ.get(key) or ""
        if not py:
            cfg = data.get("config")
            py = (cfg.get(key) or "") if isinstance(cfg, dict) else ""
        if not py:
            needs = interp.get("needs")
            specs_needs = f" (it needs {needs})" if isinstance(needs, str) and needs else ""
            skipped.append(f"{name}: no interpreter configured: set {key} in "
                           f"skills/{skill_dir.name}/manifest.json{specs_needs}")
            continue
        if not Path(py).is_file():
            skipped.append(f"{name}: configured interpreter does not exist: {py}")
            continue
        specs.append(WorkerSpec(name=name, argv=[py, str(script)], cwd=str(REPO)))
    return specs, skipped


def worker_specs() -> list:
    specs = [
        WorkerSpec(
            name="remote-gateway-bridge",
            argv=[sys.executable, str(REPO / "src" / "remote-gateway-bridge.py")],
            cwd=str(REPO),
        ),
    ]
    skill_specs, skipped = _skill_worker_specs()
    specs.extend(skill_specs)
    for why in skipped:
        print(f"sparrowd: not supervised — {why}", file=sys.stderr)
    return specs


def external_supervisor(marker: str) -> "str | None":
    """A live process already running the worker script means another
    supervisor (e.g. an app bundle's keepalive) owns it — dual supervision
    degrades to an eviction/reap loop, so sparrowd must refuse, not race."""
    import os
    import subprocess
    out = subprocess.run(["pgrep", "-f", marker],
                         capture_output=True, text=True)
    pids = [p for p in out.stdout.split()
            if p.isdigit() and int(p) != os.getpid()]
    if not pids:
        return None
    lines = []
    for pid in pids:
        ps = subprocess.run(["ps", "-o", "ppid=,command=", "-p", pid],
                            capture_output=True, text=True).stdout.strip()
        lines.append(f"pid {pid} ({ps or 'gone'})")
    return "; ".join(lines)


def main() -> int:
    for spec in worker_specs():
        owned = external_supervisor(Path(spec.argv[-1]).name)
        if owned:
            print(f"sparrowd: refusing to start — {spec.name} already "
                  f"supervised outside sparrowd: {owned}. Stop that "
                  f"supervisor (e.g. the app's gateway-keepalive) first.",
                  file=sys.stderr)
            return 2
    state_dir = resolve_workspace() / "state" / "sparrowd"
    return run(worker_specs(), state_dir)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
