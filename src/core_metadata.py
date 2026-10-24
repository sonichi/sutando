"""Record the core and configured model at launch for agent profile cards.

This is launch configuration; the gateway prefers a later accepted switch
(`state/model-switch.json`). Missing configuration is left unknown, never guessed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import write_status  # noqa: E402


def configured_model(runtime: str) -> Optional[str]:
    override = os.environ.get("SUTANDO_CORE_MODEL", "").strip()
    if override:
        return override
    try:
        env_name = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME"}.get(runtime)
        if not env_name:
            return None
        config_dir = os.environ.get(env_name)
        if not config_dir:
            from sutando_config import find_core_config_dir
            config_dir = (find_core_config_dir(type_=runtime) or {}).get("value")
        if not config_dir:
            return None
        home = Path(config_dir)
        if runtime == "claude":
            config = json.loads((home / "settings.json").read_text())
        elif runtime == "codex":
            import tomllib
            config = tomllib.loads((home / "config.toml").read_text())
        else:
            return None
        model = config.get("model")
        return model.strip() if isinstance(model, str) and model.strip() else None
    except (OSError, ValueError, AttributeError, ImportError):
        return None


def record(workspace: Path, runtime: str, session: str) -> Path:
    return write_status("core-runtime.json", {
        "runtime": runtime,
        "model": configured_model(runtime),
        "session": session,
        "started_at": int(time.time()),
    }, workspace)


if __name__ == "__main__":
    record(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
