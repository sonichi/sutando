#!/usr/bin/env python3
"""The status line recorder's config and its install into a Claude config dir.

Config (skills/MANIFEST.md precedence, env > manifest `config` > state file):
- QUOTA_STATUSLINE_ENABLED: install the recorder (default on); off removes what it installed.
- QUOTA_STATUSLINE_PASSTHROUGH: a status line command to pipe the input to and print.
  Unset, the foreign status line the installer replaced, kept beside that config dir's
  settings as `quota-statusline-passthrough.json`, is the one used.
- CREDENTIAL_PROXY_ENABLED: off is the owner's explicit "keep the proxy off" (default on).
"""
from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO = SKILL_DIR.parents[1]
sys.path.insert(0, str(REPO / "src"))

MANIFEST = SKILL_DIR / "manifest.json"
RECORDER = SKILL_DIR / "scripts" / "statusline-record.py"
STOPFAILURE = SKILL_DIR / "scripts" / "stopfailure-record.py"
STATUS_MARK = "/quota-tracker/scripts/statusline-record.py"
STOP_MARK = "/quota-tracker/scripts/stopfailure-record.py"
PASSTHROUGH_FILE = "quota-statusline-passthrough.json"
REFRESH_SECONDS = 60
OFF = {"0", "false", "no", "off"}


def manifest_config(key: str, manifest: Path = MANIFEST) -> Any:
    try:
        cfg = json.loads(manifest.read_text(encoding="utf-8")).get("config") or {}
    except (OSError, ValueError, AttributeError):
        return None
    return cfg.get(key) if isinstance(cfg, dict) else None


def config(key: str, manifest: Path = MANIFEST) -> Any:
    env = os.environ.get(key)
    if env not in (None, ""):
        return env
    return manifest_config(key, manifest)


def enabled(manifest: Path = MANIFEST) -> bool:
    value = config("QUOTA_STATUSLINE_ENABLED", manifest)
    if value is None or value == "":
        return True
    return str(value).strip().lower() not in OFF


def proxy_allowed(manifest: Path = MANIFEST) -> bool:
    value = config("CREDENTIAL_PROXY_ENABLED", manifest)
    if value is None or value == "":
        return True
    return str(value).strip().lower() not in OFF


def workspace() -> Path:
    from workspace_default import resolve_workspace
    return Path(resolve_workspace(migrate=False))


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def passthrough_command(config_dir: Optional[str], manifest: Path = MANIFEST) -> Optional[str]:
    cmd = config("QUOTA_STATUSLINE_PASSTHROUGH", manifest)
    if not (isinstance(cmd, str) and cmd.strip()) and config_dir:
        saved = _read_json(Path(config_dir) / PASSTHROUGH_FILE) or {}
        cmd = saved.get("command")
    if not isinstance(cmd, str) or not cmd.strip() or STATUS_MARK in cmd:
        return None
    return cmd


def hook_command(script: Path) -> str:
    """The same fail-open, interpreter-at-event-time shape skill hooks are registered with."""
    from skill_hooks import python_runner_prefix
    q = shlex.quote(str(script))
    return f"[ -f {q} ] || exit 0; {python_runner_prefix(REPO)}exec \"$_py\" {q}"


def _is_ours(status_line: Any) -> bool:
    return isinstance(status_line, dict) and STATUS_MARK in str(status_line.get("command") or "")


def _strip_stop_hooks(settings: dict) -> bool:
    groups = (settings.get("hooks") or {}).get("StopFailure")
    if not isinstance(groups, list):
        return False
    changed = False
    kept_groups = []
    for g in groups:
        hooks = g.get("hooks") if isinstance(g, dict) else None
        if isinstance(hooks, list):
            kept = [h for h in hooks if not (isinstance(h, dict) and STOP_MARK in str(h.get("command") or ""))]
            if len(kept) != len(hooks):
                changed = True
                if not kept:
                    continue
                g = {**g, "hooks": kept}
        kept_groups.append(g)
    if changed:
        if kept_groups:
            settings["hooks"]["StopFailure"] = kept_groups
        else:
            del settings["hooks"]["StopFailure"]
            if not settings["hooks"]:
                del settings["hooks"]
    return changed


def install(config_dir: str, on: bool) -> str:
    """Bring `<config_dir>/settings.json` to the wanted state; returns what was done."""
    settings_path = Path(config_dir) / "settings.json"
    settings = _read_json(settings_path)
    if settings is None:
        return "skipped: settings.json unreadable"
    before = json.dumps(settings, sort_keys=True)
    saved_path = Path(config_dir) / PASSTHROUGH_FILE
    current = settings.get("statusLine")
    if on:
        if current is not None and not _is_ours(current):
            _write_json(saved_path, current)
        settings["statusLine"] = {"type": "command", "command": hook_command(RECORDER),
                                  "refreshInterval": REFRESH_SECONDS}
        cmd = hook_command(STOPFAILURE)
        present = any(isinstance(h, dict) and h.get("command") == cmd
                      for g in ((settings.get("hooks") or {}).get("StopFailure") or [])
                      if isinstance(g, dict) for h in (g.get("hooks") or []))
        if not present:
            _strip_stop_hooks(settings)
            settings.setdefault("hooks", {}).setdefault("StopFailure", []).append(
                {"matcher": "rate_limit", "hooks": [{"type": "command", "command": cmd}]})
    else:
        restore = None
        if _is_ours(current):
            restore = _read_json(saved_path)
            if restore is None:
                return "skipped: saved status line unreadable, kept"
            if restore:
                settings["statusLine"] = restore
            else:
                del settings["statusLine"]
        _strip_stop_hooks(settings)
        if json.dumps(settings, sort_keys=True) == before:
            return "unchanged"
        _write_json(settings_path, settings)
        # The backup is the only copy of the original until the restore is read back.
        if restore and (_read_json(settings_path) or {}).get("statusLine") != restore:
            return "skipped: restore not verified, saved status line kept"
        if restore is not None:
            try:
                saved_path.unlink()
            except OSError:
                pass
        return "removed"
    if json.dumps(settings, sort_keys=True) == before:
        return "unchanged"
    _write_json(settings_path, settings)
    return "installed"
