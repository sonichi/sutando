#!/usr/bin/env python3
"""sparrowd launcher — the adapter edge that names concrete workers.

The package shell (ag2_sparrow.sparrowd) is deliberately blind to what it
supervises; THIS file owns the worker list and resolved paths, so the core
never imports or locates a repo-specific loop.

The agent bridges a claude-local install declares under `<workspace>/agents`
are rescanned every `--rescan-s`: a declaration written later is picked up, a
removed one has its worker stopped, an edited one has it restarted. `--agent-bridges` supervises only those, for
a host whose gateway bridge another supervisor already runs (the launchd
wrapper starts this mode beside the bridge).
"""
import argparse
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


AGENT_BRIDGE = _SRC / "sparrow_agent_bridge.py"


def _agent_bridge_specs(workspace=None, vault_keys=None, held=None) -> "tuple[list, list[str]]":
    """One worker per agent bridge a claude-local install declared under <workspace>/agents; each skipped
    declaration is one reason, never run. A vault manifest or agents directory that cannot be read is
    `sparrow_agent_bridge.ScanUnavailable`: the caller keeps what it runs, nothing is read as absent. The
    worker names whose declaration file alone could not be read are appended to `held`."""
    import sparrow_agent_bridge as agent_bridge
    if vault_keys is None:
        from vault_intercept import list_vault_keys
        try:
            vault_keys = list_vault_keys()
        except Exception as exc:
            raise agent_bridge.ScanUnavailable(f"vault manifest unreadable ({type(exc).__name__}: {exc})") from exc
    found, skipped = agent_bridge.declarations(workspace or resolve_workspace(), vault_keys, held)
    specs = []
    for path, decl in found:     # names derive from distinct segments: never two alike
        specs.append(WorkerSpec(name=decl["name"], argv=[sys.executable, str(AGENT_BRIDGE), str(path)],
                                cwd=str(REPO), revision=decl["digest"]))
    return specs, skipped


def _ere_literal(text: str) -> str:
    return re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", text)


def supervision_marker(spec) -> str:
    """The pgrep pattern for a worker's process: an agent bridge by the wrapper and its own declaration
    path ending the command line (an editor or a backup of the file is not a bridge), any other worker by
    its script's name."""
    if len(spec.argv) > 2 and Path(spec.argv[1]).name == AGENT_BRIDGE.name:
        return f"(^|[/ ]){_ere_literal(AGENT_BRIDGE.name)} {_ere_literal(spec.argv[-1])}$"
    return Path(spec.argv[-1]).name


def _static_specs() -> "tuple[list, list[str]]":
    """The workers read once, at start: the host's gateway bridge and the skill loops."""
    specs = [
        WorkerSpec(
            name="remote-gateway-bridge",
            argv=[sys.executable, str(REPO / "src" / "remote-gateway-bridge.py")],
            cwd=str(REPO),
        ),
    ]
    skill_specs, skipped = _skill_worker_specs()
    specs.extend(skill_specs)
    return specs, skipped


def worker_specs() -> list:
    import sparrow_agent_bridge as agent_bridge
    specs, skipped = _static_specs()
    try:
        agent_specs, agent_skipped = _agent_bridge_specs()
    except agent_bridge.ScanUnavailable as e:
        agent_specs, agent_skipped = [], [f"agent bridges not scanned: {e}"]
    specs.extend(agent_specs)
    skipped.extend(agent_skipped)
    for why in skipped:
        print(f"sparrowd: not supervised — {why}", file=sys.stderr)
    return specs


class AgentBridgeScan:
    """The agent-bridge workers as of each pass. A reason is printed when it first
    appears, not every pass; a declaration another process already runs (its
    marker live outside this daemon) is skipped the same way, so a declaration
    never runs twice. A name this scan already returned is never re-checked:
    its live process is this daemon's own worker. A declaration file that alone
    cannot be read keeps its worker as it was. A scan that cannot be made
    raises `ScanUnavailable` through to the supervisor, which keeps its set."""

    def __init__(self, workspace=None):
        self._workspace = workspace
        self._reported: set = set()
        self._supervised: dict = {}

    def __call__(self) -> list:
        held: list = []
        specs, skipped = _agent_bridge_specs(self._workspace, held=held)
        kept = [self._supervised[name] for name in held if name in self._supervised]
        for spec in specs:
            owned = None if spec.name in self._supervised else external_supervisor(supervision_marker(spec))
            if owned:
                skipped.append(f"{spec.argv[-1]}: already supervised outside sparrowd: {owned}")
            else:
                kept.append(spec)
        for why in skipped:
            if why not in self._reported:
                print(f"sparrowd: not supervised — {why}", file=sys.stderr)
        self._reported = set(skipped)
        self._supervised = {spec.name: spec for spec in kept}
        return kept


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


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="supervise Sparrow's resident loops")
    ap.add_argument("--agent-bridges", action="store_true",
                    help="supervise only the agent bridges declared under <workspace>/agents (the host's "
                         "gateway bridge and the skill loops run elsewhere)")
    ap.add_argument("--rescan-s", type=float, default=30.0, metavar="S",
                    help="seconds between scans of <workspace>/agents for added or removed declarations")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    static, skipped = ([], []) if args.agent_bridges else _static_specs()
    for why in skipped:
        print(f"sparrowd: not supervised — {why}", file=sys.stderr)
    for spec in static:
        owned = external_supervisor(supervision_marker(spec))
        if owned:
            print(f"sparrowd: refusing to start — {spec.name} already "
                  f"supervised outside sparrowd: {owned}. Stop that "
                  f"supervisor (e.g. the app's gateway-keepalive) first.",
                  file=sys.stderr)
            return 2
    import sparrow_agent_bridge as agent_bridge
    scan = AgentBridgeScan()
    try:
        agents = scan()
    except agent_bridge.ScanUnavailable as e:  # picked up on the first pass that can be made
        print(f"sparrowd: agent bridges not scanned yet — {e}", file=sys.stderr)
        agents = []
    state_dir = resolve_workspace() / "state" / "sparrowd"
    return run(static + agents, state_dir, rescan=lambda: static + scan(), rescan_s=args.rescan_s,
               wait_for_lock=args.agent_bridges)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
