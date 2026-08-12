#!/usr/bin/env python3
"""Resolve the proactive loop's autonomous self-development policy.

Precedence follows the skill-config convention:

    environment override > manifest.json config default

The shipped manifest defaults to enabled. An invalid value fails closed so a
misconfigured product deployment never silently enables autonomous code work.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Mapping, Optional

ENV_NAME = "SUTANDO_SELF_DEVELOPMENT_ENABLED"
TRUE_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})
FALSE_VALUES = frozenset({"0", "false", "no", "off", "disabled"})
MANIFEST_PATH = Path(__file__).resolve().parents[1] / "manifest.json"


def _manifest_default(manifest_path: Path = MANIFEST_PATH) -> Optional[str]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        config = manifest.get("config", {})
        if not isinstance(config, dict):
            return None
        value = config.get(ENV_NAME)
    except (OSError, ValueError, TypeError):
        return None
    return str(value) if value is not None else None


def self_development_enabled(
    environ: Optional[Mapping[str, str]] = None,
    manifest_path: Path = MANIFEST_PATH,
) -> bool:
    """Return whether autonomous improvement work is allowed.

    Missing configuration uses the manifest default. Missing/broken manifest
    data and unrecognized values fail closed.
    """

    env = os.environ if environ is None else environ
    raw = env.get(ENV_NAME)
    if raw is None:
        raw = _manifest_default(manifest_path)
    normalized = (raw or "").strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    return False


def main() -> int:
    enabled = self_development_enabled()
    print("enabled" if enabled else "disabled")
    raw = os.environ.get(ENV_NAME)
    if raw is not None and raw.strip().lower() not in TRUE_VALUES | FALSE_VALUES:
        print(
            f"{ENV_NAME}={raw!r} is invalid; self-development is disabled",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
