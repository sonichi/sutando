#!/usr/bin/env python3
"""Sutando Instance Manifest registry — persistent "this agent exists here"
records, M1 of the manifest spec (taxonomy part 4/5): Agent existence ≠ agent
process existence.

One JSON per instance under a well-known per-user dir, keyed by the composite
(agent_id, instance_id) via the shared injective encoding in `instance_key.py`
— `<agent>.json` for the default instance (pre-M2 name),
`<agent>+<instance>.json` otherwise:
  darwin  ~/Library/Application Support/sutando/instances/
  linux   $XDG_DATA_HOME|~/.local/share/sutando/instances/
  any     $SUTANDO_INSTANCE_REGISTRY overrides

The manifest is small, stable, human-readable, versioned, and NEVER carries
tokens/keys/memory. The Server is its single writer: atomic write at boot
(status running), mark_stopped on clean shutdown. A crash leaves status
"running" behind on purpose — manifest-says-running + dead socket is the
stale_or_crashed signal; only unregister deletes the file. Discovery reads
(list/load) must work with no daemon running — that is the point.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from instance_key import instance_key

SCHEMA_VERSION = 1


def registry_dir() -> Path:
    # DELIBERATELY outside the workspace: discovery must work BEFORE any
    # workspace is known — each manifest names its own. Don't "fix" this back.
    env = os.environ.get("SUTANDO_INSTANCE_REGISTRY")
    if env:
        return Path(env)
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support"
                / "sutando" / "instances")
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "sutando" / "instances"


def _key(agent_id: str, instance: str | None) -> str:
    """Composite durable key: actor identity says WHO executes; instance
    identity says WHICH installation. Neither substitutes for the other, and
    the encoding is injective so two tuples can never share a manifest."""
    return instance_key(agent_id, instance)


def _manifest_path(agent_id: str, instance: str | None = None) -> Path:
    return registry_dir() / f"{_key(agent_id, instance)}.json"


def write_manifest(agent_id: str, *, workspace: str | None = None,
                   owner: str | None = None, endpoint: str | None = None,
                   backend: str | None = None, host_label: str | None = None,
                   launcher: dict | None = None, instance: str | None = None,
                   tmux_socket: str | None = None, session: str | None = None,
                   config_dir: str | None = None,
                   status: str = "running") -> Path:
    """Atomic write of the instance manifest (0700 dir / 0600 file). Preserves
    installed_at across rewrites so registration age survives restarts."""
    d = registry_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    p = _manifest_path(agent_id, instance)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    installed_at = now
    try:
        installed_at = json.loads(p.read_text()).get("installed_at") or now
    except (OSError, ValueError):
        pass
    manifest = {
        "schema_version": SCHEMA_VERSION,
        # always present: attachability/routing verify BOTH identity axes
        "instance_id": instance or "default",
        "identity": {"agent_id": agent_id,
                     **({"owner": owner} if owner else {}),
                     **({"host_label": host_label} if host_label else {})},
        **({"workspace": workspace} if workspace else {}),
        **({"endpoint": {"type": "unix", "path": endpoint}} if endpoint else {}),
        **({"runtime": {**({"backend": backend} if backend else {}),
                        **({"tmux_socket": tmux_socket} if tmux_socket else {}),
                        **({"session": session} if session else {})}}
           if (backend or tmux_socket or session) else {}),
        **({"claude": {"config_dir": config_dir}} if config_dir else {}),
        **({"launcher": launcher} if launcher else {}),
        "status": status,
        "installed_at": installed_at,
        "updated_at": now,
    }
    tmp = d / f".{p.name}.tmp"
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return p


def mark_stopped(agent_id: str, instance: str | None = None) -> None:
    """Clean-shutdown transition. Deliberately a no-op if the manifest is
    missing — shutdown must never fail on registry state."""
    p = _manifest_path(agent_id, instance)
    try:
        m = json.loads(p.read_text())
    except (OSError, ValueError):
        return
    m["status"] = "stopped"
    m["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = p.parent / f".{p.name}.tmp"
    tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def list_instances() -> list:
    """All registered instances, running or not. Unreadable files are listed
    as unreadable rather than hidden — a corrupt manifest is still evidence
    an instance was registered."""
    d = registry_dir()
    if not d.is_dir():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        if f.name.endswith(".desired.json"):
            continue
        try:
            m = json.loads(f.read_text())
            m["_file"] = str(f)
        except (OSError, ValueError):
            out.append({"_file": str(f), "error": "unreadable manifest"})
            continue
        desired = None
        try:
            desired = json.loads(
                f.with_name(f.name[:-5] + ".desired.json").read_text())
        except (OSError, ValueError):
            pass
        if desired:
            m["desired_state"] = desired.get("desired_state")
        out.append(m)
    return out


# ── desired state: written ONLY on explicit lifecycle intent, never by ──────
# crash/shutdown — that is what makes crash-vs-stopped decidable.

DESIRED_STATES = ("running", "stopped", "paused")


def _desired_path(agent_id: str, instance: str | None = None) -> Path:
    return registry_dir() / f"{_key(agent_id, instance)}.desired.json"


def write_desired_state(agent_id: str, state: str, *, reason: str | None = None,
                        restore: dict | None = None,
                        instance: str | None = None) -> Path:
    if state not in DESIRED_STATES:
        raise ValueError(f"desired state must be one of {DESIRED_STATES}")
    d = registry_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    p = _desired_path(agent_id, instance)
    payload = {
        "desired_state": state,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **({"reason": reason} if reason else {}),
        **({"restore": restore} if restore else {}),
    }
    tmp = d / f".{p.name}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return p


def read_desired_state(agent_id: str, instance: str | None = None) -> dict | None:
    try:
        return json.loads(_desired_path(agent_id, instance).read_text())
    except (OSError, ValueError):
        return None


# ── start: client-side by necessity (a STOPPED instance has no daemon) — ────
# registry -> manifest -> probe socket -> launcher. Self-stop stays separate.

def _socket_alive(path: str, timeout: float = 1.0) -> bool:
    import socket as _socket
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _rpc_probe(sock_path: str, method: str, timeout: float = 2.0):
    """One argless JSON-RPC call to a socket; None on any failure."""
    import socket as _socket
    frame = ('{"jsonrpc":"2.0","id":"probe","method":"%s","params":{}}\n'
             % method).encode()
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall(frame)
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return (json.loads(buf.decode()) or {}).get("result")
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def attachable(manifest: dict) -> dict:
    """The owner's readiness definition: an instance is attachable only when
    the runtime socket is reachable AND sutando.info reports the SAME instance
    identity AND the core is live (runtime.health online/degraded). A bare
    socket file — or a socket answering with the wrong identity — is NOT
    attachable (that is exactly the 'start says ok, attach fails' trap)."""
    agent_id = (manifest.get("identity") or {}).get("agent_id")
    inst = manifest.get("instance_id") or "default"
    endpoint = (manifest.get("endpoint") or {}).get("path")
    if not endpoint or not _socket_alive(endpoint):
        return {"attachable": False, "stage": "server", "endpoint": endpoint}
    info = _rpc_probe(endpoint, "sutando.info")
    # BOTH axes: a sibling instance of the same Stand answers with the right
    # agentId but the wrong instanceId — routing there leaks to the wrong core.
    if (not info or info.get("agentId") != agent_id
            or (info.get("instanceId") or "default") != inst):
        return {"attachable": False, "stage": "identity", "endpoint": endpoint}
    health = _rpc_probe(endpoint, "runtime.health") or {}
    if health.get("state") not in ("online", "degraded"):
        return {"attachable": False, "stage": "core", "endpoint": endpoint}
    return {"attachable": True, "endpoint": endpoint}


def start_instance(agent_id: str, wait_s: float = 30.0, _ready=attachable,
                   instance: str | None = None) -> dict:
    """Start a registered instance via its manifest launcher and wait until it
    is ATTACHABLE (not merely socket-present). Idempotent, serialized by a
    per-instance start lock so two concurrent starts spawn ONE launcher. The
    launcher runs as a structured executable+args (never a shell string), in
    its declared working directory, with instance-identifying env injected
    FROM THE MANIFEST — not inherited from the calling shell. `_ready` is the
    readiness probe (injectable for tests)."""
    import fcntl
    import subprocess
    p = _manifest_path(agent_id, instance)
    try:
        m = json.loads(p.read_text())
    except (OSError, ValueError):
        return {"ok": False, "error": f"not_registered: no manifest for {agent_id!r}"}
    endpoint = (m.get("endpoint") or {}).get("path")
    if _ready(m).get("attachable"):
        return {"ok": True, "state": "already_running", "endpoint": endpoint}

    launcher = m.get("launcher") or {}
    exe = launcher.get("executable")
    if launcher.get("type") not in ("process", "command") or not exe:
        return {"ok": False, "error": "manifest has no usable structured launcher"}
    if not os.access(exe, os.X_OK):
        return {"ok": False, "error": f"launcher not executable: {exe}"}

    # Per-instance start lock: two concurrent starts must invoke ONE launcher.
    lock_fd = None
    if endpoint:
        run_dir = Path(endpoint).parent
        run_dir.mkdir(parents=True, exist_ok=True)
        lock_fd = open(run_dir / "start.lock", "w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_fd.close()
            return {"ok": False, "error": "another start is in progress "
                                          "(start lock held)"}
    try:
        # Re-check under the lock — a racing start may have finished.
        if _ready(m).get("attachable"):
            return {"ok": True, "state": "already_running", "endpoint": endpoint}

        env = {**os.environ,
               "SUTANDO_INSTANCE_ID": m.get("instance_id") or instance
                                      or "default",
               # the child must serve the TARGET identity — the caller's
               # ambient SUTANDO_AGENT_ID takes precedence in server.py
               "SUTANDO_AGENT_ID":
                   (m.get("identity") or {}).get("agent_id") or agent_id}
        rt = m.get("runtime") or {}
        for var, val in (("SUTANDO_RUNTIME_SOCKET", endpoint),
                         ("SUTANDO_TMUX_SOCKET", rt.get("tmux_socket")),
                         ("SUTANDO_TMUX_SESSION", rt.get("session")),
                         ("CLAUDE_CONFIG_DIR",
                          (m.get("claude") or {}).get("config_dir")),
                         ("SUTANDO_INSTANCE_DIR", m.get("instance_dir"))):
            if val:
                env[var] = val

        cwd = launcher.get("working_directory") or None
        log_fh = subprocess.DEVNULL
        if endpoint:
            logs = Path(endpoint).parent / "logs"
            try:
                logs.mkdir(parents=True, exist_ok=True)
                log_fh = open(logs / "startup.log", "a")
            except OSError:
                log_fh = subprocess.DEVNULL

        proc = subprocess.Popen(
            [exe, *(launcher.get("args") or [])], cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT,
            start_new_session=True)

        deadline = time.time() + wait_s
        last = {"stage": "server"}
        while time.time() < deadline:
            r = _ready(m)
            if r.get("attachable"):
                write_desired_state(agent_id, "running", reason="start verb",
                                    instance=m.get("instance_id") or instance)
                return {"ok": True, "state": "started", "pid": proc.pid,
                        "endpoint": endpoint}
            last = r
            if proc.poll() is not None:
                return {"ok": False, "state": "launcher_exited",
                        "error": f"launcher exited rc={proc.returncode} before "
                                 "the instance became attachable"}
            time.sleep(0.3)
        stage = last.get("stage", "server")
        msg = {"server": "Runtime API socket did not become reachable",
               "identity": "socket answered with the WRONG instance identity",
               "core": "Server became ready but Core did not become attachable"}
        return {"ok": False, "state": "timeout", "stage": stage, "pid": proc.pid,
                "error": f"{msg.get(stage, 'not attachable')} within {wait_s}s"
                         + (f" — Log: {Path(endpoint).parent / 'logs' / 'startup.log'}"
                            if endpoint else "")}
    finally:
        if lock_fd is not None:
            lock_fd.close()


# ── attach (v1: the native tmux TUI IS the session interface): argv from ─────
# THE MANIFEST, never hand-built — the client stays dumb about tmux paths.

def attach_command(manifest: dict) -> dict:
    """Resolve the tmux attach argv for an instance from its manifest.
    Returns {"ok": True, "argv": [...]} or {"ok": False, "error": ...}."""
    rt = manifest.get("runtime") or {}
    if rt.get("backend") not in (None, "tmux"):
        return {"ok": False, "error": f"backend {rt.get('backend')!r} is not "
                                      "tmux — attach is a tmux-only v1 verb"}
    sock = rt.get("tmux_socket")
    session = rt.get("session")
    if not sock or not session:
        return {"ok": False, "error": "manifest has no runtime.tmux_socket + "
                                      "runtime.session to attach to"}
    return {"ok": True,
            "argv": ["tmux", "-S", sock, "attach-session", "-t", session]}


def attach(agent_id: str, instance: str | None = None) -> dict:
    p = _manifest_path(agent_id, instance)
    try:
        m = json.loads(p.read_text())
    except (OSError, ValueError):
        return {"ok": False, "error": f"not_registered: no manifest for {agent_id!r}"}
    return attach_command(m)
