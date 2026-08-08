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
        **({"runtime": {"backend": backend}} if backend else {}),
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


def start_instance(agent_id: str, wait_s: float = 10.0) -> dict:
    """Start a registered instance via its manifest launcher. Idempotent on a
    live instance (already_running). Launches ONLY the structured executable
    recorded in the 0600 manifest — never a shell string."""
    import subprocess
    p = _manifest_path(agent_id)
    try:
        m = json.loads(p.read_text())
    except (OSError, ValueError):
        return {"ok": False, "error": f"not_registered: no manifest for {agent_id!r}"}
    endpoint = (m.get("endpoint") or {}).get("path")
    if endpoint and _socket_alive(endpoint):
        return {"ok": True, "state": "already_running", "endpoint": endpoint}
    launcher = m.get("launcher") or {}
    exe, args = launcher.get("executable"), launcher.get("args") or []
    if launcher.get("type") != "command" or not exe:
        return {"ok": False, "error": "manifest has no usable command launcher"}
    if not os.access(exe, os.X_OK):
        return {"ok": False, "error": f"launcher executable missing or not executable: {exe}"}
    proc = subprocess.Popen([exe, *args], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if endpoint and _socket_alive(endpoint):
            write_desired_state(agent_id, "running", reason="start verb")
            return {"ok": True, "state": "started", "pid": proc.pid,
                    "endpoint": endpoint}
        if proc.poll() is not None:
            return {"ok": False,
                    "error": f"launcher exited rc={proc.returncode} before the "
                             "endpoint came up"}
        time.sleep(0.2)
    return {"ok": False, "error": f"endpoint not ready within {wait_s}s "
                                  "(launcher still running)", "pid": proc.pid}
