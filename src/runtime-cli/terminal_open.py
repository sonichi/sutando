#!/usr/bin/env python3
"""Terminal adapter for `sutando open` — spawn `sutando attach <id>` in a new
terminal tab/window so the Sutando control TUI can stay in the current tab
(owner v1). Adapters are per-terminal; an unknown terminal falls back to
printing the exact command for the user to run themselves.

Pure helpers (build_open_plan / applescript_for) are string-only so the
adapter choice is unit-tested without spawning anything.
"""
from __future__ import annotations

import os
import shutil
import subprocess


def detect_terminal() -> str:
    """Best-effort current-terminal identity from the environment."""
    if os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
        return "apple_terminal"
    if os.environ.get("TERM_PROGRAM") == "iTerm.app":
        return "iterm2"
    if os.environ.get("WEZTERM_PANE") is not None:
        return "wezterm"
    if os.environ.get("KITTY_WINDOW_ID") is not None:
        return "kitty"
    if os.environ.get("TERM_PROGRAM") == "ghostty":
        return "ghostty"
    return "unknown"


def applescript_for(command: str, window: bool = False) -> str:
    """AppleScript that runs `command` in a new Apple Terminal tab (default)
    or window. `do script` with no target opens a new window; targeting the
    front window opens a tab."""
    if window:
        return (f'tell application "Terminal"\n  activate\n'
                f'  do script "{command}"\nend tell')
    return (f'tell application "Terminal"\n  activate\n'
            f'  do script "{command}" in front window\nend tell')


def build_open_plan(agent_id: str, terminal: str, window: bool = False,
                    instance: str | None = None) -> dict:
    """Decide how to open. Returns {"method": ..., ...} — never spawns."""
    command = f"sutando attach {agent_id}"
    if instance and instance != "default":
        command += f" --instance {instance}"
    if terminal == "apple_terminal":
        return {"method": "applescript",
                "script": applescript_for(command, window=window),
                "command": command}
    if terminal == "wezterm" and shutil.which("wezterm"):
        return {"method": "exec",
                "argv": ["wezterm", "cli", "spawn", "--", "sh", "-c", command],
                "command": command}
    if terminal == "kitty" and shutil.which("kitty"):
        return {"method": "exec",
                "argv": ["kitty", "@", "launch", "--type",
                         "window" if window else "tab", "sh", "-c", command],
                "command": command}
    return {"method": "manual", "command": command}


def open_instance(agent_id: str, window: bool = False,
                  instance: str | None = None) -> dict:  # pragma: no cover — spawns a real terminal tab; build_open_plan/applescript_for are the tested pure core
    plan = build_open_plan(agent_id, detect_terminal(), window=window,
                           instance=instance)
    if plan["method"] == "applescript":
        subprocess.run(["osascript", "-e", plan["script"]], check=False)
        return {"ok": True, "opened": "new_window" if window else "new_tab",
                "command": plan["command"]}
    if plan["method"] == "exec":
        subprocess.run(plan["argv"], check=False)
        return {"ok": True, "opened": "new_window" if window else "new_tab",
                "command": plan["command"]}
    return {"ok": False, "opened": "manual",
            "hint": f"Run in another terminal:\n    {plan['command']}",
            "command": plan["command"]}
