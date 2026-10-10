#!/usr/bin/env python3
"""Shared macOS Keychain resolution for Claude Code CLI credentials.

Mirrors credential-proxy.ts's `scopedKeychainService`: a per-CLAUDE_CONFIG_DIR
Keychain item name (`Claude Code-credentials-<sha256(config_dir)[:8]>`), with a
fallback to the vanilla shared item (`Claude Code-credentials`) for installs
that predate per-config-dir scoping. Centralized so health-check.py's
quota-account-identity probe and auth_preflight.py's boot-auth probe never
drift from each other (CLAUDE.md "Shared adapter policy is core").

The scoped name is a pure function of the config_dir STRING, not the machine:
two hosts sharing a CLAUDE_CONFIG_DIR path produce the identical service name.
Harmless while each machine's Keychain stays local; would only collide if a
future mechanism merged Keychain items across machines.
"""
from __future__ import annotations

import hashlib
import subprocess
from typing import Optional

VANILLA_SERVICE = "Claude Code-credentials"


def scoped_keychain_service(config_dir: Optional[str]) -> Optional[str]:
    """The per-config-dir Keychain item name, or None for an empty config_dir."""
    dir_ = (config_dir or "").strip()
    if not dir_:
        return None
    digest = hashlib.sha256(dir_.encode()).hexdigest()[:8]
    return f"{VANILLA_SERVICE}-{digest}"


def keychain_service_exists(service: str) -> bool:  # pragma: no cover - external I/O (security CLI)
    """Existence check only — the secret value is never requested. Non-macOS
    (no `security` binary) -> False."""
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", service],
            capture_output=True, timeout=5,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def resolved_credential_service(config_dir: Optional[str]) -> Optional[str]:
    """First EXISTING item of [scoped(config_dir), vanilla] — the proxy's own order."""
    for service in (scoped_keychain_service(config_dir), VANILLA_SERVICE):
        if service and keychain_service_exists(service):
            return service
    return None
