"""A skill's config value, read CLI > env > the skill's manifest `config` block (skills/MANIFEST.md).

Dependency-light, so a caller can ask whether an optional feature is on before importing any of
its code. The caller names the manifest; this module names no skill. `enabled()` never raises:
anything it cannot read is off.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping, Optional

TRUE = ("1", "true", "yes", "on")


def manifest_config(manifest: Path) -> dict:
    """The manifest's `config` block, or {} when it cannot be read."""
    try:
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cfg = data.get("config") if isinstance(data, dict) else None
    return cfg if isinstance(cfg, dict) else {}


def config_value(key: str, manifest: Optional[Path] = None, cli=None,
                 environ: Optional[Mapping[str, str]] = None, manifest_cfg: Optional[Mapping] = None) -> str:
    """CLI > env > manifest config; an empty value counts as unset."""
    if cli is not None and str(cli).strip():
        return str(cli).strip()
    env = os.environ if environ is None else environ
    if str(env.get(key) or "").strip():
        return str(env[key]).strip()
    cfg = (manifest_config(manifest) if manifest is not None else {}) if manifest_cfg is None else manifest_cfg
    return str(cfg.get(key) or "").strip()


def enabled(key: str, manifest: Path, environ: Optional[Mapping[str, str]] = None) -> bool:
    """True only when the key reads as true; any fault reads as off."""
    try:
        return config_value(key, manifest, environ=environ).lower() in TRUE
    except Exception:  # noqa: BLE001 - an unreadable switch is an off switch
        return False
