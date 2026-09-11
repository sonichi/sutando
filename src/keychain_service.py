#!/usr/bin/env python3
"""keychain_service.py — shared macOS Keychain resolution for Claude Code CLI credentials.

Mirrors credential-proxy.ts's `scopedKeychainService`: a per-CLAUDE_CONFIG_DIR
Keychain item name (`Claude Code-credentials-<sha256(config_dir)[:8]>`), with a
fallback to the vanilla shared item (`Claude Code-credentials`) for installs
that predate per-config-dir scoping.

Centralized because more than one adapter needs this exact resolution
(src/health-check.py's quota-account-identity probe, src/auth_preflight.py's
boot-auth probe) — a copy that drifts between them is the defect (see
CLAUDE.md "Shared adapter policy is core"). Found via a real, install-breaking
instance of that drift: auth_preflight.py checked only the vanilla name,
so a scoped-keychain install (the common case) always read as logged-out
even while genuinely authenticated, and `bash src/restart.sh` permanently
aborted startup on a healthy host (2026-09-11).

Property worth stating rather than assuming (review, #4196, 2026-09-11): the
scoped name is a pure function of the config_dir STRING, not of the machine —
two different hosts with the same CLAUDE_CONFIG_DIR path produce the identical
service name (confirmed live: two independent hosts both produced
`Claude Code-credentials-b0888206` from the same path string). Harmless while
each machine's Keychain stays local, as it does today; it would become a
genuine collision (two different secrets, one name) only if some future
mechanism merged keychain items across machines. Neither observed nor
implemented anywhere in this codebase as of this fix — noted so it is a known
property if that ever changes, not a surprise.
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
