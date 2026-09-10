#!/usr/bin/env python3
"""sparrowd launcher — the adapter edge that names concrete workers.

The package shell (ag2_sparrow.sparrowd) is deliberately blind to what it
supervises; THIS file owns the worker list and resolved paths, so the core
never imports or locates a repo-specific loop.
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
REPO = _SRC.parent
for _p in (str(_SRC), str(REPO / "packages" / "ag2-sparrow")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from workspace_default import resolve_workspace  # noqa: E402
from channel_env_resolve import resolve_channel_env  # noqa: E402
from ag2_sparrow.sparrowd import WorkerSpec, run  # noqa: E402

# The unsuffixed lane. Naming it would rename its whole id namespace
# (`task-<inst>~...`) and its status file, orphaning work already in flight.
PRIMARY_CHANNEL = "ag2space"


def _channels_dir():
    return resolve_workspace() / ".claude-sutando" / "channels"


def relay_channels(channels_dir=None) -> list:
    """Channel dirs that resolve to a RELAY env, sorted, primary first.

    Selection is delegated to `channel_env_resolve`, which picks by CONTENT —
    a usable REMOTE_TASK_TOKEN — so discord/slack/telegram dirs are excluded
    without this file naming them.
    """
    base = channels_dir if channels_dir is not None else _channels_dir()
    base = Path(base)
    if not base.is_dir():
        return []
    found = []
    for d in sorted(base.iterdir()):
        if d.is_dir() and resolve_channel_env(base, d.name) is not None:
            found.append(d.name)
    found.sort(key=lambda n: (n != PRIMARY_CHANNEL, n))
    return found


def instance_for(channel: str) -> str:
    """GATEWAY_INSTANCE for a channel dir — the name ALREADY in use, not the dir.

    Live lanes are `dev` and `local` for `dev-ag2space` and `local-ag2space`;
    inventing `dev-ag2space` would move that lane's `task-<inst>~...` namespace
    and status file, orphaning work in flight.
    """
    if channel == PRIMARY_CHANNEL:
        return ""
    suffix = f"-{PRIMARY_CHANNEL}"
    return channel[: -len(suffix)] if channel.endswith(suffix) else channel


def worker_specs(channels_dir=None) -> list:
    """One gateway bridge per relay channel.

    A channel with no bridge is a channel that silently receives nothing, which
    is what made `local-ag2space` inert: the dir and its token existed, but
    nothing ran against them.
    """
    bridge = str(REPO / "src" / "remote-gateway-bridge.py")
    specs = []
    for name in relay_channels(channels_dir):
        instance = instance_for(name)
        specs.append(WorkerSpec(
            name=f"gateway-{name}",
            argv=[sys.executable, bridge],
            cwd=str(REPO),
            env={"REMOTE_TASK_CHANNEL_DIR": name, "GATEWAY_INSTANCE": instance},
        ))
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
