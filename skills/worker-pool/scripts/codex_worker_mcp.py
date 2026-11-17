#!/usr/bin/env python3
"""The `s-space` MCP server a Codex worker seat runs, as `codex -c` overrides, one argument per line.

Codex hands a stdio MCP server only the variables its entry names, so a seat started from the desktop's
registration alone never sees the worker it is (`SUTANDO_WORKER_ID`) or the credential resolver
(`AG2_MCP_WORKER_CREDENTIAL`), and its proxy would mint as the core's agent. This prints that
registration with both names added to `env_vars`, so the seat's proxy mints as the worker's own agent,
or refuses. The registration is read from the Codex home's `config.toml`, else from the Claude config
dir's `.claude.json` (the desktop registers it only for the core's runtime). Neither holds a secret, and
nothing is printed when neither registers one. Usage:

    codex_worker_mcp.py --codex-home DIR [--claude-config-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11: the launcher may resolve the system 3.9
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        import toml_fallback as tomllib

SERVER_KEY = "s-space"
# connections.SESSION_WORKER_ENV and connections.MCP_WORKER_CREDENTIAL_ENV; a test pins them equal.
PASSTHROUGH = ("SUTANDO_WORKER_ID", "AG2_MCP_WORKER_CREDENTIAL")


def _stdio(entry) -> bool:
    return (isinstance(entry, dict) and isinstance(entry.get("command"), str) and entry["command"] != ""
            and isinstance(entry.get("args", []), list))


def registration(codex_home, claude_config_dir=None):
    """The desktop's `s-space` stdio entry, as a Codex table, or None when no config registers one."""
    try:
        cfg = tomllib.loads((Path(codex_home) / "config.toml").read_text())
        entry = (cfg.get("mcp_servers") or {}).get(SERVER_KEY)
        if _stdio(entry):
            return dict(entry)
    except (OSError, ValueError, AttributeError):
        pass
    if not claude_config_dir:
        return None
    try:
        cfg = json.loads((Path(claude_config_dir) / ".claude.json").read_text())
        entry = (cfg.get("mcpServers") or {}).get(SERVER_KEY)
    except (OSError, ValueError, AttributeError):
        return None
    if not _stdio(entry):
        return None
    out = {"command": entry["command"], "args": [str(a) for a in entry.get("args", [])]}
    env = entry.get("env")
    if isinstance(env, dict):
        out["env"] = {str(k): str(v) for k, v in env.items()}
    return out


def _toml(value) -> str:
    # json.dumps quotes a TOML basic string the same way; DEL is the one character TOML also requires escaped.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_toml(str(k))} = {_toml(v)}" for k, v in value.items()) + "}"
    raise ValueError(f"no TOML form for {type(value).__name__}")


def overrides(entry) -> list:
    """`-c` arguments that make the seat's `s-space` server `entry` plus the worker passthrough."""
    if entry is None:
        return []
    seat = dict(entry)
    named = [v for v in seat.get("env_vars") or [] if isinstance(v, (str, dict))]
    have = {v if isinstance(v, str) else v.get("name") for v in named}
    seat["env_vars"] = named + [n for n in PASSTHROUGH if n not in have]
    value = _toml(seat)
    tomllib.loads(f"x = {value}")  # never hand Codex a value it could not parse
    return ["-c", f"mcp_servers.{SERVER_KEY}={value}"]


def home_registers(codex_home) -> bool:
    """Whether the Codex home's own config names an `s-space` server in any form (stdio, url, ...)."""
    try:
        cfg = tomllib.loads((Path(codex_home) / "config.toml").read_text())
    except (OSError, ValueError):
        return False
    return isinstance(cfg.get("mcp_servers"), dict) and SERVER_KEY in cfg["mcp_servers"]


def seat_overrides(codex_home, claude_config_dir=None) -> list:
    """The seat's `-c` arguments: the passthrough entry, else the home's own entry switched off, since
    one the worker variables cannot reach would act as the core's agent."""
    entry = registration(codex_home, claude_config_dir)
    if entry is None and home_registers(codex_home):
        return ["-c", f"mcp_servers.{SERVER_KEY}.enabled=false"]
    return overrides(entry)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--codex-home", required=True)
    p.add_argument("--claude-config-dir", default="")
    a = p.parse_args(argv)
    for arg in seat_overrides(a.codex_home, a.claude_config_dir or None):
        print(arg)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess by the launcher tests
    sys.exit(main())
