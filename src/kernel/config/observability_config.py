"""Narrow config slice for the observability + metering spine (Python twin).

Resolves ONLY the ``observability`` / ``metering`` / ``tenant`` blocks -- NOT the
full Spine-C config. Returns a plain ``dict`` mirroring the JSON structure
(camelCase keys, e.g. ``batchMax``) so it stays byte-parallel with the TS twin
``observability-config.ts`` and consistent with how obs/meter pass dicts.

Resolution order (each layer overlays the previous, field by field):
  1. shipped defaults -- repo ``config/sutando.config.defaults.json``
     (falls back to OBSERVABILITY_DEFAULTS if missing/unparseable)
  2. environment knobs -- SUTANDO_TENANT_ID / SUTANDO_TENANT_MODE /
     SUTANDO_METERING_ENABLED / SUTANDO_METERING_ENDPOINT
  3. workspace override -- ``<workspace>/config/observability.json`` (machine-local; wins)
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

# Flat src/ module; importable for the same reason the `kernel` package is.
from workspace_default import resolve_workspace

__all__ = ["OBSERVABILITY_DEFAULTS", "load_observability_config"]

# In-code defaults -- kept equal to config/sutando.config.defaults.json's three
# blocks (a test asserts this so they cannot drift). Fallback when the file
# can't be read.
OBSERVABILITY_DEFAULTS: dict[str, Any] = {
    "observability": {"sinks": [{"type": "jsonl-file"}], "sampling": {"trace": 1.0}},
    "metering": {"enabled": False, "endpoint": None, "batchMax": 100},
    "tenant": {"id": None, "mode": "byok"},
}

_TRUEISH = {"1", "true", "yes", "on"}


def _defaults_file_path() -> Path:
    # src/kernel/config/observability_config.py -> repo root is three dirs up.
    return Path(__file__).resolve().parents[3] / "config" / "sutando.config.defaults.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (ValueError, OSError) as e:
        print(f"[observability-config] failed to parse {path}, ignoring: {e}", file=sys.stderr)
        return None


def _overlay(base: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """Overlay the three blocks of ``raw`` onto ``base``, key by key. Keys absent
    from ``raw`` keep their ``base`` value. The sinks array is taken verbatim."""
    obs = raw.get("observability") or {}
    meter = raw.get("metering") or {}
    tenant = raw.get("tenant") or {}
    sinks = obs.get("sinks")
    sampling = obs.get("sampling") or {}
    mode = tenant.get("mode")
    return {
        "observability": {
            "sinks": sinks if isinstance(sinks, list) else base["observability"]["sinks"],
            "sampling": {
                "trace": sampling.get("trace", base["observability"]["sampling"]["trace"]),
            },
        },
        "metering": {
            "enabled": meter.get("enabled", base["metering"]["enabled"]),
            "endpoint": meter.get("endpoint", base["metering"]["endpoint"]),
            "batchMax": meter.get("batchMax", base["metering"]["batchMax"]),
        },
        "tenant": {
            "id": tenant.get("id", base["tenant"]["id"]),
            "mode": mode if mode in ("byok", "managed") else base["tenant"]["mode"],
        },
    }


def load_observability_config(workspace: Path | None = None) -> dict[str, Any]:
    # 1. shipped defaults (file overlays the in-code const)
    cfg = copy.deepcopy(OBSERVABILITY_DEFAULTS)
    file_raw = _read_json(_defaults_file_path())
    if file_raw:
        cfg = _overlay(cfg, file_raw)

    # 2. environment knobs
    tenant_id = os.environ.get("SUTANDO_TENANT_ID", "").strip()
    if tenant_id:
        cfg["tenant"]["id"] = tenant_id
    tenant_mode = os.environ.get("SUTANDO_TENANT_MODE", "").strip()
    if tenant_mode:
        cfg["tenant"]["mode"] = "managed" if tenant_mode == "managed" else "byok"
    met_enabled = os.environ.get("SUTANDO_METERING_ENABLED", "").strip()
    if met_enabled:
        cfg["metering"]["enabled"] = met_enabled.lower() in _TRUEISH
    met_endpoint = os.environ.get("SUTANDO_METERING_ENDPOINT", "").strip()
    if met_endpoint:
        cfg["metering"]["endpoint"] = met_endpoint

    # 3. workspace override (machine-local; wins, per the documented order)
    ws = workspace if workspace is not None else resolve_workspace(migrate=False)
    override_raw = _read_json(Path(ws) / "config" / "observability.json")
    if override_raw:
        cfg = _overlay(cfg, override_raw)

    return cfg
