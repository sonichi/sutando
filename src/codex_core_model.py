#!/usr/bin/env python3
"""The Codex core's own model choice: core-only, never the shared Codex home's.

A Codex core and its pool workers share one CODEX_HOME (one login), so the core's pick must not go
into `<CODEX_HOME>/config.toml`: every worker reads that file at start and would follow. The pick is
kept in the core's switch record instead (`model-switch.json` in the state folder its caller names), with
`runtime: "codex"` (a record without it is a Claude core's and is never read here). Readers:

- `src/agent/codex/cli/start-cli.sh` launches the core with `-m <model>` from it when
  SUTANDO_CORE_MODEL is not set for that launch (an explicit launch value wins);
- the core's app-server seat (`src/agent/codex/app-server/seat.py`, no `--worker`) launches on
  `working`, the last model Codex took, and passes a pending pick as `model` on a `turn/start`. It
  is `accepted` once Codex runs that turn and `rejected` when Codex refuses that model; any other
  failure leaves it pending for the next turn.

Pool workers read none of this: their model is their own.

    python3 codex_core_model.py check <model>                 # exit 0 a Codex model name, 2 not
    python3 codex_core_model.py current --state-dir DIR       # the recorded model, or nothing
    python3 codex_core_model.py launch --state-dir DIR        # "<rev> <pick> <working>" ('-' none), read once
    python3 codex_core_model.py save --state-dir DIR --model M --applied WHEN [--accepted]
    python3 codex_core_model.py choices [--codex-home DIR]    # id=Title;... from Codex's model cache

Stdlib only.
"""
from __future__ import annotations

import argparse
import contextlib
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
LOCK = ".model-switch.lock"  # switch-model.sh holds the same lock while it saves


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


def _rev(rec: dict) -> int:
    rev = rec.get("rev")
    return rev if isinstance(rev, int) and not isinstance(rev, bool) and rev > 0 else 0


def working(rec: dict) -> str:
    """The last model Codex took: `confirmed` (what a completed turn last ran on, whatever pick is
    pending), else the record's own once accepted, else the one it carried."""
    c = rec.get("confirmed")
    if isinstance(c, str) and valid(c):
        return c
    if rec.get("status", "accepted" if rec.get("accepted") else "") == "accepted" and valid(rec.get("model") or ""):
        return rec["model"]
    w = rec.get("working")
    return w if isinstance(w, str) and valid(w) else ""


def current(state_dir) -> str:
    """The model the Codex core is to use, '' for none: the last pick, or, once Codex refused it,
    the last model Codex took."""
    rec = read_record(state_dir)
    if rec.get("status") == "rejected":
        return working(rec)
    return rec.get("model") or ""


def revision(state_dir) -> int:
    """The record's revision, 0 for none; every save raises it by one."""
    return _rev(read_record(state_dir))


def launch(state_dir) -> tuple:
    """(rev, model, working) read in one go: the pick a TUI launch takes, and the last model Codex
    took, which a seat launches on while a pick is still pending."""
    rec = read_record(state_dir)
    model = working(rec) if rec.get("status") == "rejected" else (rec.get("model") or "")
    verified = model if rec.get("status") in (None, "accepted") else working(rec)
    return _rev(rec), model, verified


def pending(state_dir) -> tuple:
    """(model, rev): the pick Codex has not yet taken or refused, '' when there is none."""
    rec = read_record(state_dir)
    return (rec.get("model") or "" if rec.get("status") == "pending" else ""), _rev(rec)


@contextlib.contextmanager
def _locked(state_dir):
    import fcntl
    os.makedirs(state_dir, exist_ok=True)
    with open(Path(state_dir) / LOCK, "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield


def _write(state_dir, rec: dict) -> None:
    os.makedirs(state_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(state_dir), prefix=".model-switch.staged.")
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f, indent=2)
        f.write("\n")
    os.replace(tmp, record_path(state_dir))


def confirm(state_dir, model: str, rev: int) -> bool:
    """Record that Codex completed a turn on `model` (the pick of revision `rev`): `confirmed` and
    `confirmed_rev`, kept apart from the pending pick, which this never settles. An older revision
    never replaces a newer confirmation."""
    if not valid(model):
        return False
    with _locked(state_dir):
        rec = read_record(state_dir)
        if not rec or int(rec.get("confirmed_rev") or 0) > rev:
            return False
        rec["confirmed"], rec["confirmed_rev"] = model, rev
        _write(state_dir, rec)
        return True


def resolve(state_dir, rev: int, accepted: bool, reason: str = "") -> Optional[dict]:
    """Settle the pending pick `rev`: accepted (it becomes `working`) or rejected (the core stays on
    `working`). None when the record has moved on or is not pending; a newer pick is never touched."""
    with _locked(state_dir):
        rec = read_record(state_dir)
        if not rec or _rev(rec) != rev or rec.get("status") != "pending":
            return None
        rec["status"], rec["accepted"] = ("accepted" if accepted else "rejected"), bool(accepted)
        if accepted:
            rec["working"] = rec["confirmed"] = rec["model"]
            rec["confirmed_rev"] = max(rev, int(rec.get("confirmed_rev") or 0))
        else:
            rec["rejected_reason"] = " ".join(str(reason).split())[:240]
        _write(state_dir, rec)
        return rec


def save(state_dir, model: str, *, applied: str, accepted: bool, now: Optional[float] = None) -> dict:
    """Write the record atomically, one revision past the last. Without `accepted` the pick is
    `pending` until Codex takes it; `previous` is the model in use before it and `working` the last
    model Codex took. Raises ValueError for a name that is not a Codex model, OSError when it cannot
    be written."""
    if not valid(model):
        raise ValueError(f"{model!r} is not a Codex model name")
    if applied not in APPLIED:
        raise ValueError(f"applied must be one of {APPLIED}")
    old = read_record(state_dir)
    prev = current(state_dir) or None
    rec = {"model": model, "previous": prev, "previous_source": "last-record" if prev else "none",
           "runtime": RUNTIME, "accepted": bool(accepted), "status": "accepted" if accepted else "pending",
           "working": model if accepted else (working(old) or None), "rev": _rev(old) + 1,
           "applied": applied, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "by": BY}
    for key in ("confirmed", "confirmed_rev"):  # a turn's confirmation outlives later picks
        if old.get(key):
            rec[key] = old[key]
    if accepted:
        rec["confirmed"], rec["confirmed_rev"] = model, rec["rev"]
    _write(state_dir, rec)
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
    la = sub.add_parser("launch")
    la.add_argument("--state-dir", required=True)
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
    if a.cmd == "launch":
        rev, model, verified = launch(a.state_dir)
        print(f"{rev} {model or '-'} {verified or '-'}")
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
