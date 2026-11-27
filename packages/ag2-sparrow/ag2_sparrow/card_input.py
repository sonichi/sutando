"""A card's typed input (a sign-in code, say) handed from the bridge to its agent outside the task file.

The task file is durable (archived, and read by the model when no card claims the task), so a value typed
into a card never goes in it. The bridge keeps the value in a one-time file of its own state dir, 0600 in a
0700 folder, and writes only that file's random reference in the task's `card_click`. The agent's card
`take`s it: read once and removed. A value nobody takes is removed after TTL_S. Stdlib only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

DIR = "card-input"
TTL_S = 900.0
REF_RE = re.compile(r"[0-9a-f]{32}")


def _folder(state_dir) -> Path:
    d = Path(state_dir) / DIR
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def put(state_dir, values: dict, now=None) -> str:
    """Keep `values` for one `take`; its reference."""
    d = _folder(state_dir)
    sweep(state_dir, now)
    ref = secrets.token_hex(16)
    fd = os.open(str(d / (ref + ".json")), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"at": time.time() if now is None else now, "values": values}, f)
    return ref


def take(state_dir, ref, now=None) -> dict:
    """The values kept under `ref`, removed as they are read; {} for none, a stale one or a bad reference."""
    if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
        return {}
    path = Path(state_dir) / DIR / (ref + ".json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    fresh = isinstance(data, dict) and (time.time() if now is None else now) - float(data.get("at") or 0) <= TTL_S
    values = data.get("values") if fresh else None
    return dict(values) if isinstance(values, dict) else {}


def sweep(state_dir, now=None) -> int:
    """Remove the values nobody took within TTL_S; how many."""
    d = Path(state_dir) / DIR
    gone, cut = 0, (time.time() if now is None else now) - TTL_S
    for p in d.glob("*.json") if d.is_dir() else ():
        try:
            if p.stat().st_mtime < cut:
                p.unlink()
                gone += 1
        except OSError:
            continue
    return gone
