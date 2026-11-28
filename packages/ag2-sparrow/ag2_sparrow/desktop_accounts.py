"""The accounts the desktop app saved on this computer for one local agent, as its own account menu lists them.

The app keeps them under `<app data>/accounts/<store>/<16 hex of the agent id>/` (agent_account.rs `Scope::subdir`):
Claude Code's as an `index.json` of key -> label, Codex's as one `<key>.json` per account. Only labels are read
here (an address); nothing else in those files is looked at, kept or logged. Stdlib only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from . import account_switch
from .bounded_file_read import read_regular

KEY_RE = re.compile(r"[0-9a-f]{16}")
CODEX_STORE = ("accounts", "codex-local")
CODEX_FILE_MAX = 256 << 10


def saved_key(text: str) -> str:
    """The desktop's store key of an agent id or an account's address: 16 hex of its SHA-256."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def agent_slug(agent: str) -> str:
    """The desktop's folder name for `agent` under `<app data>/sparrow/agents` (sparrow_local.rs agent_slug)."""
    readable = "".join(c if (c.isascii() and c.isalnum()) or c in "._-" else "_" for c in agent.lstrip("@"))[:40]
    return "%s-%s" % (readable, hashlib.sha256(agent.encode("utf-8")).hexdigest()[:12])


def email_only(text: str) -> str:
    """A saved label without its " (plan)" (the desktop's email_only)."""
    t = text.strip()
    i = t.rfind(" (")
    return t[:i].rstrip() if i >= 0 and t.endswith(")") and not any(c in t[i + 2:-1] for c in "()") else t


def address(label) -> str:
    """A saved label as a whole address, '' for anything else (a masked copy, a non-string)."""
    email = email_only(label) if isinstance(label, str) else ""
    if not email or "…" in email or "...@" in email or account_switch.shown(email) != email:
        return ""
    return email


def app_data_dirs(agent: str, login_dir: str, leaf: str, home=None, environ=None) -> list:
    """Where the desktop app keeps its data, most likely first: the one the agent's login dir sits in, when
    the app made that dir (`<app data>/sparrow/agents/<slug>/<leaf>`), then the app's folders for this user
    as Tauri lays them out (macOS: `space.ag2.desktop`, then the older `space.ag2.app`)."""
    env = os.environ if environ is None else environ
    home = Path(home) if home is not None else Path.home()
    found = []
    if agent and login_dir:
        p = Path(login_dir)
        if p.name == leaf and p.parent.name == agent_slug(agent) and p.parent.parent.name == "agents" \
                and p.parent.parent.parent.name == "sparrow":
            found.append(p.parents[3])
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        found += [base / "space.ag2.desktop", base / "space.ag2.app"]
    elif sys.platform.startswith("win"):
        if env.get("APPDATA"):
            found.append(Path(env["APPDATA"]) / "space.ag2.app")
    else:
        found.append(Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "space.ag2.app")
    return list(dict.fromkeys(found))


def codex_saved(agent: str, codex_home: str, dirs=None) -> dict:
    """{key: address} of the Codex accounts the desktop saved for `agent`, from the first app data dir that
    has its folder; {} for none. Each file's label is all that is taken from it."""
    if not agent:
        return {}
    for d in (app_data_dirs(agent, codex_home, "codex-home") if dirs is None else dirs):
        folder = Path(d).joinpath(*CODEX_STORE) / saved_key(agent)
        try:
            names = sorted(os.listdir(str(folder)))
        except OSError:
            continue
        out = {}
        for name in names:
            key = name[:-5] if name.endswith(".json") else ""
            if not KEY_RE.fullmatch(key):
                continue
            text, _ = read_regular(str(folder / name), CODEX_FILE_MAX, follow=False)
            try:
                data = json.loads(text) if text else None
            except ValueError:
                data = None
            email = address(data.get("label")) if isinstance(data, dict) else ""
            if email:
                out[key] = email
        return out
    return {}
