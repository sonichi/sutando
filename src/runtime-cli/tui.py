#!/usr/bin/env python3
"""sutando tui — a deliberately DUMB reference client + architecture probe.

The point (owner spec, taxonomy part 9): a client that discovers, inspects,
starts and talks to a Sutando instance knowing ONLY the boundary

    Registry -> Instance Manifest -> Launcher -> Sutando Protocol

and nothing about tmux, CLAUDE_CONFIG_DIR, task-file layout or recovery
internals. If this client ever needs that knowledge, the Server abstraction is
incomplete. So this module speaks exclusively: the registry (list/start) and
JSON-RPC over each instance's OWN endpoint socket.

The five states are kept SEPARATE per the spec — existence, server, core,
health, desired — because conflating them is the bug the manifest exists to
prevent (a stale socket/PID must never read as Running).

`instance_view()` and `render_view()` are pure over their inputs (view does
one socket probe; render is string-only) so the probe logic is unit- and
E2E-tested without a terminal. `main()` is a thin key-driven loop on top.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.with_name("runtime-api")))
import instance_registry  # noqa: E402


def _send(s: socket.socket, method: str, params: dict) -> dict:
    frame = json.dumps({"jsonrpc": "2.0", "id": f"tui-{uuid.uuid4().hex[:8]}",
                        "method": method, "params": params},
                       ensure_ascii=False) + "\n"
    s.sendall(frame.encode("utf-8"))
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    resp = json.loads(buf.decode("utf-8"))
    if "error" in resp:
        raise RuntimeError(resp["error"].get("message"))
    return resp["result"]


def _rpc_at(sock_path: str, method: str, params: dict, timeout: float = 10.0,
            expect: tuple | None = None) -> dict:
    """One JSON-RPC call to a SPECIFIC instance socket (not the shared
    default) — the client always addresses the instance it selected.

    `expect` is an (agent_id, instance_id) tuple the answering peer must
    match. It is checked over THIS connection, immediately before the call, so
    the check and the action cannot straddle a socket replacement: an earlier
    verification is a cache, and a cache is what routed private work to a
    sibling runtime that rebound the path in between."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        if expect is not None:
            info = _send(s, "sutando.info", {})
            got = (info.get("agentId"), info.get("instanceId") or "default")
            if got != (expect[0], expect[1] or "default"):
                raise RuntimeError(
                    f"socket identity changed: expected {expect[0]}/"
                    f"{expect[1]}, got {got[0]}/{got[1]} — refusing to route")
        return _send(s, method, params)
    finally:
        s.close()


def _socket_reachable(path: str, timeout: float = 1.0) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def instance_view(manifest: dict) -> dict:
    """Compose the client-facing view of one instance from its manifest + a
    live protocol probe. The manifest 'status' is only last-known intent — it
    is NEVER trusted as running; the socket probe is the running signal."""
    agent_id = (manifest.get("identity") or {}).get("agent_id") or "?"
    inst = manifest.get("instance_id") or "default"
    endpoint = (manifest.get("endpoint") or {}).get("path")
    view = {
        "agentId": agent_id,
        "instanceId": inst,
        "existence": "registered",              # the manifest exists
        "server": "unreachable",
        "core": "unknown",
        "health": "unknown",
        "desiredState": manifest.get("desired_state") or manifest.get("status"),
        "identityVerified": None,
        "endpoint": endpoint,
    }
    if not endpoint or not _socket_reachable(endpoint):
        view["server"] = "stopped"
        return view
    view["server"] = "running"
    # Identity: the socket answering must be THIS instance — both axes. The
    # same Stand's sibling instance answers agentId-true but instanceId-false.
    try:
        info = _rpc_at(endpoint, "sutando.info", {})
        view["identityVerified"] = (
            info.get("agentId") == agent_id
            and (info.get("instanceId") or "default") == inst)
    except (OSError, RuntimeError, ValueError):
        view["identityVerified"] = False
    # Core + health, kept distinct from server reachability.
    try:
        h = _rpc_at(endpoint, "runtime.health", {})
        st = h.get("state")
        view["core"] = "running" if st in ("online", "degraded") else "stopped"
        view["health"] = {"online": "healthy", "degraded": "degraded",
                          "offline": "unresponsive"}.get(st, "unknown")
        if h.get("currentActivity"):
            view["activity"] = h["currentActivity"]
    except (OSError, RuntimeError, ValueError):
        pass
    return view


def render_view(view: dict) -> str:
    mark = {True: "verified", False: "MISMATCH", None: "-"}[view.get("identityVerified")]
    lines = [
        "Sutando Instance",
        f"  ID:         {view['agentId']}",
        f"  Instance:   {view.get('instanceId') or 'default'}",
        f"  Existence:  {view['existence']}",
        f"  Server:     {view['server']}",
        f"  Core:       {view['core']}",
        f"  Health:     {view['health']}",
        f"  Desired:    {view.get('desiredState') or '-'}",
        f"  Identity:   {mark}",
    ]
    if view.get("activity"):
        lines.append(f"  Activity:   {view['activity']}")
    lines.append(f"  Endpoint:   {view.get('endpoint') or '-'}")
    return "\n".join(lines)


def _views() -> list:
    return [{**instance_view(m), "_manifest": m}
            for m in instance_registry.list_instances()
            if "identity" in m]


_KEYS = ("[l] list/refresh  [s] start <id>  [c] connect <id>  "
         "[a] attach <id>  [o] open <id>  [t] task <id> <text>  "
         "[h] requests <id>  [q] quit")


def main(argv=None) -> int:  # pragma: no cover — interactive key loop; instance_view/render_view are the tested pure core
    print("sutando tui — dumb reference client (Registry -> Manifest -> "
          "Launcher -> Protocol)\n")
    while True:
        views = _views()
        print("Sutando Instances")
        for v in views:
            dot = "●" if v["server"] == "running" else "○"
            sel = f"{v['agentId']}/{v['instanceId']}"
            print(f"  {dot} {sel:<28} {v['server']:<10} "
                  f"core={v['core']} health={v['health']}")
        if not views:
            print("  (none registered)")
        print("\n" + _KEYS)
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            continue
        cmd, *rest = raw.split()
        if cmd == "q":
            return 0
        if cmd == "l":
            continue
        # Instances are addressed agentId/instanceId; a bare agentId is only
        # accepted while it names exactly ONE instance (never a silent pick).
        by_id = {f"{v['agentId']}/{v['instanceId']}": v for v in views}
        for v in views:
            by_id[v["agentId"]] = (
                None if sum(1 for x in views
                            if x["agentId"] == v["agentId"]) > 1 else v)
        if cmd in ("s", "c", "a", "o", "t", "h") and rest:
            v = by_id.get(rest[0])
            if v is None:
                print(f"  no such instance (or ambiguous agent id — use "
                      f"agentId/instanceId): {rest[0]}\n")
                continue
            # The view's flag is a fast reject only; routed calls re-verify on
            # their own connection, so replacing the socket after this cannot win.
            expect = (v["agentId"], v["instanceId"])
            if cmd in ("a", "o", "t", "h") and v.get("identityVerified") is not True:
                # the socket answered as a DIFFERENT instance (or never
                # answered): routing work/attach there leaks to the wrong core
                print("  refusing: socket identity is not verified as "
                      f"{rest[0]} (identityVerified="
                      f"{v.get('identityVerified')})\n")
                continue
            try:
                if cmd == "s":
                    print(" ", instance_registry.start_instance(
                        v["agentId"], instance=v["instanceId"]), "\n")
                elif cmd == "c":
                    print(render_view(instance_view(v["_manifest"])), "\n")
                elif cmd == "a":
                    # hand the whole terminal to the native TUI (v1 attach)
                    ac = instance_registry.attach(v["agentId"],
                                                  instance=v["instanceId"])
                    if not ac.get("ok"):
                        print(f"  {ac.get('error')}\n")
                        continue
                    os.execvp(ac["argv"][0], ac["argv"])
                elif cmd == "o":
                    import terminal_open
                    print(" ", terminal_open.open_instance(
                        v["agentId"], instance=v["instanceId"]), "\n")
                elif cmd == "t":
                    ep = v.get("endpoint")
                    if not ep or v["server"] != "running":
                        print("  instance not running — start it first\n")
                        continue
                    out = _rpc_at(ep, "task.submit", {"task": " ".join(rest[1:])},
                                  expect=expect)
                    print(" ", out, "\n")
                elif cmd == "h":
                    ep = v.get("endpoint")
                    if not ep or v["server"] != "running":
                        print("  instance not running — start it first\n")
                        continue
                    print(" ", _rpc_at(ep, "request.list", {}, expect=expect), "\n")
            except (OSError, RuntimeError) as e:
                print(f"  error: {e}\n")
        else:
            print("  usage:", _KEYS, "\n")


if __name__ == "__main__":
    raise SystemExit(main())
