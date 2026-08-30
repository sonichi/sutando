#!/usr/bin/env python3
"""Sutando Instance Manifest registry — persistent "this agent exists here"
records, M1 of the manifest spec (taxonomy part 4/5): Agent existence ≠ agent
process existence.

One JSON per instance under a well-known per-user dir:
  darwin  ~/Library/Application Support/sutando/instances/<agent-id>.json
  linux   $XDG_DATA_HOME|~/.local/share/sutando/instances/<agent-id>.json
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
import re
import sys
import time
from pathlib import Path

SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"[^A-Za-z0-9._@:-]+")


def registry_dir() -> Path:
    env = os.environ.get("SUTANDO_INSTANCE_REGISTRY")
    if env:
        return Path(env)
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support"
                / "sutando" / "instances")
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "sutando" / "instances"


def _manifest_path(agent_id: str) -> Path:
    return registry_dir() / f"{_SAFE_ID.sub('_', agent_id)}.json"


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
    p = _manifest_path(agent_id)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    installed_at = now
    try:
        installed_at = json.loads(p.read_text()).get("installed_at") or now
    except (OSError, ValueError):
        pass
    manifest = {
        "schema_version": SCHEMA_VERSION,
        **({"instance_id": instance} if instance else {}),
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


def mark_stopped(agent_id: str) -> None:
    """Clean-shutdown transition. Deliberately a no-op if the manifest is
    missing — shutdown must never fail on registry state."""
    p = _manifest_path(agent_id)
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


# ── desired state (M2) ──────────────────────────────────────────────────────
# Separate file so the stable manifest never churns on intent changes. Written
# ONLY on explicit lifecycle intent (user start/stop/pause) — never by crash
# or system shutdown, which is exactly what makes crash-vs-stopped decidable.

DESIRED_STATES = ("running", "stopped", "paused")


def _desired_path(agent_id: str) -> Path:
    return registry_dir() / f"{_SAFE_ID.sub('_', agent_id)}.desired.json"


def write_desired_state(agent_id: str, state: str, *, reason: str | None = None,
                        restore: dict | None = None) -> Path:
    if state not in DESIRED_STATES:
        raise ValueError(f"desired state must be one of {DESIRED_STATES}")
    d = registry_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    p = _desired_path(agent_id)
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


def read_desired_state(agent_id: str) -> dict | None:
    try:
        return json.loads(_desired_path(agent_id).read_text())
    except (OSError, ValueError):
        return None


# ── start (lifecycle-lite, M2.5) ────────────────────────────────────────────
# Client-side by necessity: a STOPPED instance has no daemon to ask, so the
# discovery flow is registry -> manifest -> probe socket -> launcher. Starting
# a stopped instance is NOT the self-lifecycle case (nothing running can kill
# its own caller); stop/restart-of-self remain a separate, gated design.

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
    endpoint = (manifest.get("endpoint") or {}).get("path")
    if not endpoint or not _socket_alive(endpoint):
        return {"attachable": False, "stage": "server", "endpoint": endpoint}
    info = _rpc_probe(endpoint, "sutando.info")
    if not info or info.get("agentId") != agent_id:
        return {"attachable": False, "stage": "identity", "endpoint": endpoint}
    health = _rpc_probe(endpoint, "runtime.health") or {}
    if health.get("state") not in ("online", "degraded"):
        return {"attachable": False, "stage": "core", "endpoint": endpoint}
    return {"attachable": True, "endpoint": endpoint}



def resolve_agent_id(id_or_instance: str) -> dict:
    """Resolve a user-supplied identifier to a registered agent_id. An exact
    agent_id (its manifest file exists) wins; otherwise a UNIQUE instance_id
    match resolves — `sutando list` displays instance_id, so the id a user
    reads off the list must work in attach/start. Ambiguity is an error
    naming the candidates, never a guess."""
    if _manifest_path(id_or_instance).exists():
        return {"ok": True, "agent_id": id_or_instance}
    hits = sorted({(m.get("identity") or {}).get("agent_id")
                   for m in list_instances()
                   if m.get("instance_id") == id_or_instance
                   and (m.get("identity") or {}).get("agent_id")})
    if len(hits) == 1:
        return {"ok": True, "agent_id": hits[0]}
    if hits:
        return {"ok": False, "error": (
            f"ambiguous instance_id {id_or_instance!r} matches: "
            + ", ".join(hits))}
    return {"ok": False, "error": f"not_registered: no manifest for {id_or_instance!r}"}


def start_instance(agent_id: str, wait_s: float = 30.0, _ready=attachable) -> dict:
    """Start a registered instance via its manifest launcher and wait until it
    is ATTACHABLE (not merely socket-present). Idempotent, serialized by a
    per-instance start lock so two concurrent starts spawn ONE launcher. The
    launcher runs as a structured executable+args (never a shell string), in
    its declared working directory, with instance-identifying env injected
    FROM THE MANIFEST — not inherited from the calling shell. `_ready` is the
    readiness probe (injectable for tests)."""
    import fcntl
    import subprocess
    r = resolve_agent_id(agent_id)
    if not r.get("ok"):
        return r
    agent_id = r["agent_id"]
    p = _manifest_path(agent_id)
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

        env = {**os.environ, "SUTANDO_INSTANCE_ID": agent_id}
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
                write_desired_state(agent_id, "running", reason="start verb")
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


# ── attach (v1: connect to the native tmux Claude Code TUI) ──────────────────
# The attach IS the session interface (owner v1): no session.* API needed —
# the Core keeps running its native Claude Code TUI inside tmux, and the client
# just re-attaches. The argv is resolved FROM THE MANIFEST, never hand-built,
# so the client stays dumb about where tmux lives.

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


def attach(agent_id: str) -> dict:
    r = resolve_agent_id(agent_id)
    if not r.get("ok"):
        return r
    p = _manifest_path(r["agent_id"])
    try:
        m = json.loads(p.read_text())
    except (OSError, ValueError):
        return {"ok": False, "error": f"not_registered: no manifest for {agent_id!r}"}
    return attach_command(m)
