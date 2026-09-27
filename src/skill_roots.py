"""The ordered set of directories that hold skills, shared by every skill loader.

Order: the engine's own `skills/`, `<workspace>/skills/`, `$SUTANDO_MEMORY_DIR/skills/`
(legacy `$SUTANDO_PRIVATE_DIR`), each `$SUTANDO_EXTERNAL_PLUGIN_DIRS` entry's `skills/`
(os.pathsep-separated), then every sibling checkout's `skills/` (siblings of the engine
root, sorted). Only existing directories are returned, each once. Collision policy
stays with the caller. Shell callers: `scripts/sutando-config.sh skill-roots`.
Stdlib only.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


def _memory_dir(env: Mapping[str, str]) -> str:
    return env.get("SUTANDO_MEMORY_DIR") or env.get("SUTANDO_PRIVATE_DIR") or ""


def skill_roots(repo, workspace, env: Mapping[str, str] | None = None) -> list[Path]:
    env = os.environ if env is None else env
    repo = Path(repo)
    candidates = [repo / "skills", Path(workspace) / "skills"]
    mem = _memory_dir(env).strip()
    if mem:
        candidates.append(Path(mem).expanduser() / "skills")
    for d in env.get("SUTANDO_EXTERNAL_PLUGIN_DIRS", "").split(os.pathsep):
        if d.strip():
            candidates.append(Path(d.strip()).expanduser() / "skills")
    try:
        siblings = sorted(p for p in repo.parent.iterdir() if p.name != repo.name)
    except OSError:
        siblings = []
    candidates += [s / "skills" for s in siblings]

    roots, seen = [], set()
    for c in candidates:
        if not c.is_dir():
            continue
        key = c.resolve()
        if key not in seen:
            seen.add(key)
            roots.append(c)
    return roots

