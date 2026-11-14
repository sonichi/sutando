#!/usr/bin/env python3
"""Re-export of `src/quota_availability.py`, the one authority for whether
Claude quota telemetry is usable.

The policy moved to `src/` because a third reader of the same record -- the
delivery gate -- arrived, and the repo keeps shared adapter policy in one
dependency-light core module rather than copies. This file keeps the skill's
import path working; it decides nothing itself.

This file shares its basename with the authority, and the skill's scripts put
this directory first on `sys.path`, so a plain import here would find this
shim. The authority is reused when it is already loaded, else loaded by path.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "src"
_AUTHORITY_PATH = (_SRC / "quota_availability.py").resolve()
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _load_authority():
    loaded = sys.modules.get("quota_availability")
    if loaded is not None and Path(getattr(loaded, "__file__", "") or "").resolve() == _AUTHORITY_PATH:
        return loaded
    spec = importlib.util.spec_from_file_location("_sutando_quota_availability", _AUTHORITY_PATH)
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: dataclasses resolve a class's annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_authority = _load_authority()

PROXY_PORT = _authority.PROXY_PORT
PROXY_SCHEME = _authority.PROXY_SCHEME
PROXY_HOSTS = _authority.PROXY_HOSTS
points_at_credential_proxy = _authority.points_at_credential_proxy
resolve_available = _authority.resolve_available
availability_decision = _authority.availability_decision

__all__ = ["PROXY_PORT", "PROXY_SCHEME", "PROXY_HOSTS", "points_at_credential_proxy",
           "resolve_available", "availability_decision"]
