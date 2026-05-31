#!/usr/bin/env python3
"""Agent Registry client / CLI.

Used by the Claude Code SessionStart hook to register an instance and keep it
heartbeating, and usable by hand to inspect the registry.

Commands:
  register   --name --cwd --pid [--meta JSON] [--autostart]   -> prints id
  heartbeat  --id ID
  deregister --id ID
  list                                                        -> table
  watch      --name --cwd --pid [--interval N] [--autostart]
             register, then heartbeat until the watched pid exits, then
             deregister. This is the one-shot command the hook backgrounds.

The service is located via the discovery file written by registry-service.py
(<workspace>/state/agent-registry.json). With --autostart, the client launches
the service detached if it is not already running.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.join(SCRIPT_DIR, "registry-service.py")
# Shared workspace resolver — handles SessionStart-hook env-miss by falling
# back to grep of repo .env. See _workspace_resolve.py for the rationale.
sys.path.insert(0, SCRIPT_DIR)
from _workspace_resolve import resolve_workspace  # noqa: E402

WORKSPACE = resolve_workspace()
DISCOVERY_PATH = os.path.join(WORKSPACE, "state", "agent-registry.json")


def read_discovery():
    try:
        with open(DISCOVERY_PATH) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def http(method, url, payload=None, timeout=4):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def service_url(autostart=False):
    """Return a base URL for a healthy service, starting it if asked."""
    disc = read_discovery()
    if disc and _healthy(disc["url"]):
        return disc["url"]
    if not autostart:
        return None
    return _start_service()


def _healthy(base):
    try:
        return http("GET", base + "/health", timeout=2).get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _start_service():
    log_dir = os.path.join(WORKSPACE, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log = open(os.path.join(log_dir, "agent-registry.log"), "a")
    subprocess.Popen(
        [sys.executable, SERVICE],
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # survives the Claude session that spawned it
    )
    for _ in range(50):  # wait up to ~5s for the service to come up
        time.sleep(0.1)
        disc = read_discovery()
        if disc and _healthy(disc["url"]):
            return disc["url"]
    return None


def pid_alive(pid):
    """True if pid is running. A pid of 0/None means 'nothing to watch'."""
    if not pid or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False


def cmd_register(args, base):
    meta = {}
    if args.meta:
        try:
            meta = json.loads(args.meta)
        except json.JSONDecodeError:
            print("warning: --meta is not valid JSON, ignoring", file=sys.stderr)
    result = http("POST", base + "/register", {
        "name": args.name,
        "cwd": args.cwd or os.getcwd(),
        "pid": args.pid,
        "host": os.uname().nodename,
        "meta": meta,
    })
    return result.get("id")


def cmd_watch(args, base):
    agent_id = cmd_register(args, base)
    if not agent_id:
        print("watch: registration failed", file=sys.stderr)
        return 1
    print(agent_id, flush=True)
    interval = max(5, args.interval)
    try:
        while pid_alive(args.pid):
            time.sleep(interval)
            if not pid_alive(args.pid):
                break
            try:
                http("POST", base + "/heartbeat", {"id": agent_id})
            except (urllib.error.URLError, OSError):
                pass  # transient — keep trying
    finally:
        try:
            http("POST", base + "/deregister", {"id": agent_id})
        except (urllib.error.URLError, OSError):
            pass
    return 0


def cmd_list(base):
    data = http("GET", base + "/agents")
    agents = data.get("agents", [])
    if not agents:
        print("(no agents registered)")
        return
    print(f"{'STATUS':<8} {'NAME':<16} {'PID':<8} {'HB AGE':<8} CWD")
    for a in agents:
        print(
            f"{a['status']:<8} {a['name']:<16} {str(a['pid']):<8} "
            f"{str(a['heartbeat_age'])+'s':<8} {a.get('cwd') or ''}"
        )


def main():
    p = argparse.ArgumentParser(description="Agent Registry client")
    sub = p.add_subparsers(dest="command", required=True)

    for name in ("register", "watch"):
        sp = sub.add_parser(name)
        sp.add_argument("--name", default="claude-code")
        sp.add_argument("--cwd", default=None)
        sp.add_argument("--pid", type=int, default=0)
        sp.add_argument("--meta", default=None)
        sp.add_argument("--autostart", action="store_true")
        if name == "watch":
            sp.add_argument("--interval", type=int, default=30)

    sp = sub.add_parser("heartbeat")
    sp.add_argument("--id", required=True)
    sp = sub.add_parser("deregister")
    sp.add_argument("--id", required=True)
    sub.add_parser("list")

    args = p.parse_args()

    # SIGTERM -> clean exit so cmd_watch's finally deregisters the agent when
    # the Claude session's process group is torn down.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    autostart = getattr(args, "autostart", False)
    base = service_url(autostart=autostart)
    if base is None:
        print("agent-registry service not reachable", file=sys.stderr)
        return 2

    if args.command == "register":
        agent_id = cmd_register(args, base)
        if not agent_id:
            return 1
        print(agent_id)
        return 0
    if args.command == "watch":
        return cmd_watch(args, base)
    if args.command == "heartbeat":
        http("POST", base + "/heartbeat", {"id": args.id})
        return 0
    if args.command == "deregister":
        http("POST", base + "/deregister", {"id": args.id})
        return 0
    if args.command == "list":
        cmd_list(base)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
