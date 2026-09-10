#!/usr/bin/env python3
"""Shared plumbing for the import-claude-context scripts.

Deliberately small: the data-dir resolution, the two JSON side files
(`state.json` per session, `status.json` counts only) and a few helpers every
script needs. No transcript parsing lives here — that is index.py (LLM-free
metadata) and session-recap's extract.py (the dialog stream).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
REPO = SKILL_DIR.parents[1]  # skills/import-claude-context/scripts -> repo root

# The engine modules this skill reuses (context_resume, secret_scanner,
# util_paths) live in src/; scripts import them after this insert.
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, str(REPO / "src"))

from util_paths import write_private_text  # noqa: E402

DATA_SUBDIR = ("data", "claude-import")
INDEX_FILE = "index.json"
INDEX_MD = "claude-import-index.md"
STATE_FILE = "state.json"
STATUS_FILE = "status.json"
DUMPS_DIR = "dumps"
SUMMARIES_DIR = "summaries"
PROJECTS_DIR = "projects"
ENTITIES_FILE = "entities.json"

_SINCE_RE = re.compile(r"^(\d+)([hdw])$")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def workspace_root(explicit=None) -> Path:
    """The Sutando workspace: `--workspace` when given, else the config helper
    (`bash scripts/sutando-config.sh workspace`), exactly as session-recap does."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    out = subprocess.run(
        ["bash", str(REPO / "scripts" / "sutando-config.sh"), "workspace"],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout.strip()
    if not out:
        raise SystemExit("import-claude-context: could not resolve the workspace")
    return Path(out)


def data_dir(workspace=None, explicit=None) -> Path:
    """`<workspace>/data/claude-import/` — per-host, never vault-synced."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    return workspace_root(workspace).joinpath(*DATA_SUBDIR)


def refuse_inside(root: Path, out_dir: Path) -> None:
    """The source tree is read-only: no output may land inside it."""
    real_root = os.path.realpath(str(root))
    real_out = os.path.realpath(str(out_dir))
    try:
        common = os.path.commonpath([real_root, real_out])
    except ValueError:  # different drives (Windows) — cannot be inside
        return
    if common == real_root:
        raise SystemExit(
            f"import-claude-context: refusing an out-dir inside the source root "
            f"({out_dir} is under {root}); the transcripts tree is read-only")


def ensure_private_dir(path: Path) -> Path:
    """mkdir -p with owner-only permissions (0700), also on a pre-existing dir."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def load_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def dump_json(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_json(path: Path, obj, private: bool = False) -> None:
    text = dump_json(obj)
    if private:
        write_private_text(path, text)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


def session_key(slug: str, uuid: str) -> str:
    return f"{slug}/{uuid}"


def load_state(out_dir: Path) -> dict:
    state = load_json(out_dir / STATE_FILE, {})
    state.setdefault("sessions", {})
    state.setdefault("projects", {})
    return state


def save_state(out_dir: Path, state: dict) -> None:
    state["updated_at"] = now_iso()
    write_json(out_dir / STATE_FILE, state)


def write_status(out_dir: Path, phase: str, **counts) -> dict:
    """status.json carries the phase and COUNTS only — never titles, paths or text."""
    for k, v in counts.items():
        if not isinstance(v, (int, bool)) and v is not None:
            raise ValueError(f"status.json takes counts only; {k}={v!r}")
    status = {"phase": phase, "updated_at": now_iso()}
    status.update(counts)
    write_json(out_dir / STATUS_FILE, status)
    return status


def parse_since(spec) -> "float | None":
    """`30d` / `12h` / `2w` (relative to now) or `YYYY-MM-DD` -> epoch seconds."""
    if not spec:
        return None
    m = _SINCE_RE.match(spec.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        mult = {"h": 3600, "d": 86400, "w": 7 * 86400}[unit]
        return time.time() - n * mult
    try:
        return datetime.strptime(spec.strip(), "%Y-%m-%d").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        raise SystemExit(f"import-claude-context: bad --since {spec!r} (use 30d, 12h, 2w or YYYY-MM-DD)")


def matches_project(slug: str, wanted) -> bool:
    """`--projects a,b`: exact slug, or a case-insensitive substring of the
    opaque slug (`gtm` matches `-Users-x-Projects-gtm`)."""
    if not wanted:
        return True
    low = slug.lower()
    return any(w == slug or w.lower() in low for w in wanted if w)


def absorb_dash_values(argv, flags) -> list:
    """Claude Code slugs start with "-" (`-Users-o-Projects-x`), which argparse
    reads as an option. Rewrite `--flag -Users-x` into `--flag=-Users-x` for the
    given value flags so `--projects`/`--forget` take real slugs as typed."""
    if argv is None:
        argv = sys.argv[1:]
    out = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if tok in flags and nxt is not None and nxt.startswith("-") and not nxt.startswith("--"):
            out.append(f"{tok}={nxt}")
            i += 2
            continue
        out.append(tok)
        i += 1
    return out


def split_csv(value) -> list:
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]
