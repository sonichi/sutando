#!/usr/bin/env python3
"""The Codex core's own model choice: core-only, never the shared Codex home's.

A Codex core and its pool workers share one CODEX_HOME (one login), so the core's pick must not go
into `<CODEX_HOME>/config.toml`: every worker reads that file at start and would follow. The pick is
kept in the core's switch record instead, `<workspace>/state/model-switch.json`, with
`runtime: "codex"` (a record without it is a Claude core's and is never read here). Readers:

- `src/agent/codex/cli/start-cli.sh` launches the core with `-m <model>` from it when
  SUTANDO_CORE_MODEL is not set for that launch (an explicit launch value wins);
- the core's app-server seat (`src/agent/codex/app-server/seat.py`, no `--worker`) passes it as
  `model` on the next `turn/start` once the record is newer than the seat's own start.

Pool workers read none of this: their model is their own.

    python3 codex_core_model.py check <model>                 # exit 0 a Codex model name, 2 not
    python3 codex_core_model.py current --state-dir DIR       # the recorded model, or nothing
    python3 codex_core_model.py save --state-dir DIR --model M --applied WHEN [--accepted]
    python3 codex_core_model.py choices [--codex-home DIR]    # id=Title;... from Codex's model cache

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

RUNTIME = "codex"
RECORD = "model-switch.json"
BY = "skills/model-switch/scripts/switch-model.sh"
# A Codex model slug: letters, digits, dot, dash, underscore; never a flag, never a Claude name.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_CLAUDE = re.compile(r"(default|opus|sonnet|haiku|fable|claude-.*)(\[1m\])?", re.I)
APPLIED = ("next-turn", "next-start")


def valid(name: str) -> bool:
    """A name the Codex core may be switched to. Claude's aliases and ids are not Codex models."""
    return bool(isinstance(name, str) and _NAME.fullmatch(name) and not _CLAUDE.fullmatch(name))


def record_path(state_dir) -> Path:
    return Path(state_dir) / RECORD


def read_record(state_dir) -> dict:
    """The core's Codex switch record, {} when there is none, it is unreadable, or it is a Claude core's."""
    try:
        data = json.loads(record_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("runtime") != RUNTIME or not valid(data.get("model") or ""):
        return {}
    return data


def current(state_dir) -> str:
    """The model the Codex core was last switched to, '' for none."""
    return read_record(state_dir).get("model") or ""


def newer_than(state_dir, since: float) -> str:
    """The recorded model when the record was written after `since` (epoch seconds), else ''."""
    path = record_path(state_dir)
    try:
        if path.stat().st_mtime <= since:
            return ""
    except OSError:
        return ""
    return current(state_dir)


def save(state_dir, model: str, *, applied: str, accepted: bool, now: Optional[float] = None) -> dict:
    """Write the record atomically; `previous` is the last Codex record's model. Raises ValueError for
    a name that is not a Codex model, OSError when it cannot be written."""
    if not valid(model):
        raise ValueError(f"{model!r} is not a Codex model name")
    if applied not in APPLIED:
        raise ValueError(f"applied must be one of {APPLIED}")
    prev = current(state_dir) or None
    rec = {"model": model, "previous": prev, "previous_source": "last-record" if prev else "none",
           "runtime": RUNTIME, "accepted": bool(accepted), "applied": applied,
           "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "by": BY}
    os.makedirs(state_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(state_dir), prefix=".model-switch.staged.")
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f, indent=2)
        f.write("\n")
    os.replace(tmp, record_path(state_dir))
    return rec


def home(repo) -> Path:
    """The Codex core's home without creating it: the configured `core_config_dirs` entry of type
    codex (src/codex_home.py), else $CODEX_HOME, else ~/.codex, as Codex itself reads."""
    value = ""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import codex_home
        value = codex_home.configured(repo)[1]
    except Exception:  # noqa: BLE001 - an unreadable config only means the default home
        value = ""
    return Path(os.path.expanduser(value or os.environ.get("CODEX_HOME") or "~/.codex"))


def choices(codex_home) -> list:
    """[(slug, title)] of the models Codex lists (its own `models_cache.json`), by its priority; []
    when the cache is absent or unreadable. Hidden models are left out."""
    try:
        data = json.loads((Path(codex_home) / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    models = data.get("models") if isinstance(data, dict) else None
    rows = []
    for m in models if isinstance(models, list) else []:
        if not isinstance(m, dict) or m.get("visibility") not in (None, "list"):
            continue
        slug = m.get("slug")
        if not valid(slug or ""):
            continue
        title = " ".join(str(m.get("display_name") or slug).split())
        prio = m.get("priority") if isinstance(m.get("priority"), (int, float)) else 1e9
        rows.append((prio, slug, title))
    rows.sort(key=lambda r: r[0])
    seen, out = set(), []
    for _, slug, title in rows:
        if slug not in seen:
            seen.add(slug)
            out.append((slug, title))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="codex_core_model.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("model")
    cur = sub.add_parser("current")
    cur.add_argument("--state-dir", required=True)
    s = sub.add_parser("save")
    s.add_argument("--state-dir", required=True)
    s.add_argument("--model", required=True)
    s.add_argument("--applied", required=True, choices=APPLIED)
    s.add_argument("--accepted", action="store_true")
    ch = sub.add_parser("choices")
    ch.add_argument("--codex-home", default="", help="default: the Codex core's home")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        return 0 if valid(a.model) else 2
    if a.cmd == "current":
        model = current(a.state_dir)
        if model:
            print(model)
        return 0
    if a.cmd == "choices":
        where = a.codex_home or home(Path(__file__).resolve().parents[1])  # lint-workspace-resolution: allow-repo-root
        print(";".join(f"{slug}={title}" for slug, title in choices(where)))
        return 0
    try:
        rec = save(a.state_dir, a.model, applied=a.applied, accepted=a.accepted)
    except ValueError as e:
        print(f"FAILED {e}")
        return 2
    except OSError as e:
        print(f"FAILED {e}")
        return 1
    print(f"OK {rec['previous'] or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
