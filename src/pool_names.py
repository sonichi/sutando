#!/usr/bin/env python3
"""Pool worker naming — the one owner of `worker-N` and every seam built on it.

A pool worker is `worker-<seat>` (seat 1..N). `core-<seat>` is the pre-rename
spelling: readers accept it for one release through `canonical()` and the
`*_suffixes()`/`alive_filenames()` read-side builders; nothing writes it.
Stdlib only, so every seam (lead, follower, claim primitive, installer) can
import it instead of formatting the name itself.

CLI, for shell callers (never re-implement the mapping in bash):
    python3 src/pool_names.py worker_name 2        -> worker-2
    python3 src/pool_names.py canonical core-2     -> worker-2
    python3 src/pool_names.py seat_of worker-2     -> 2
    python3 src/pool_names.py launchd_label 2      -> com.sutando.worker-2
    python3 src/pool_names.py legacy_name worker-2 -> core-2
    python3 src/pool_names.py from_env             -> name from the env, or rc 2
"""
from __future__ import annotations

import os
import re
import sys

WORKER_PREFIX = "worker-"
LEGACY_PREFIX = "core-"
LAUNCHD_PREFIX = "com.sutando."

ENV_WORKER_ID = "SUTANDO_WORKER_ID"
ENV_WORKER_SEAT = "SUTANDO_WORKER_SEAT"
ENV_POOL_SIZE = "SUTANDO_WORKER_POOL_SIZE"
# One-release read aliases; the installer still exports them for in-session readers.
ENV_LEGACY_ID = "SUTANDO_CORE_ID"
ENV_LEGACY_POOL_SIZE = "SUTANDO_CORE_POOL_SIZE"

_WORKER_RE = re.compile(r"^worker-([1-9][0-9]*)$")
_LEGACY_RE = re.compile(r"^core-([1-9][0-9]*)$")
_SEAT_RE = re.compile(r"^[1-9][0-9]*$")


def worker_name(seat) -> str:
    """`worker-<seat>` for a positive integer seat (int or digit string)."""
    try:
        n = int(str(seat).strip())
    except (TypeError, ValueError):
        raise ValueError(f"seat must be a positive integer, got {seat!r}") from None
    if n < 1:
        raise ValueError(f"seat must be >= 1, got {seat!r}")
    return f"{WORKER_PREFIX}{n}"


def seat_of(name) -> "int | None":
    """Seat number of `worker-N` or legacy `core-N`; None for anything else."""
    s = str(name).strip()
    m = _WORKER_RE.match(s) or _LEGACY_RE.match(s)
    return int(m.group(1)) if m else None


def is_worker_name(name) -> bool:
    """True only for the canonical `worker-N` spelling."""
    return _WORKER_RE.match(str(name).strip()) is not None


def is_legacy_name(name) -> bool:
    return _LEGACY_RE.match(str(name).strip()) is not None


def canonical(name) -> str:
    """`core-N` -> `worker-N`; every other value passes through unchanged."""
    s = str(name).strip()
    m = _LEGACY_RE.match(s)
    return worker_name(m.group(1)) if m else s


def resolve(value) -> str:
    """Name from a seat (`2`) or a name (`worker-2` / `core-2`), canonical."""
    s = str(value).strip()
    return worker_name(s) if _SEAT_RE.match(s) else canonical(s)


def legacy_name(name) -> "str | None":
    """`core-N` for a worker name (either spelling); None otherwise."""
    seat = seat_of(name)
    return f"{LEGACY_PREFIX}{seat}" if seat is not None else None


def aliases(name) -> "tuple[str, ...]":
    """Every spelling a reader must accept: canonical first, legacy second."""
    c = canonical(name)
    legacy = legacy_name(c)
    return (c, legacy) if legacy else (c,)


# ── task-file state suffixes (write side canonical; read side both) ──────────

def assigned_suffix(name) -> str:
    return f".assigned-{canonical(name)}.txt"


def claimed_suffix(name) -> str:
    return f".claimed-{canonical(name)}.txt"


def assigned_suffixes(name) -> "tuple[str, ...]":
    return tuple(f".assigned-{a}.txt" for a in aliases(name))


def claimed_suffixes(name) -> "tuple[str, ...]":
    return tuple(f".claimed-{a}.txt" for a in aliases(name))


# ── host-facing surfaces: launchd label, tmux session, log stem, beat file ───

def launchd_label(value) -> str:
    return f"{LAUNCHD_PREFIX}{resolve(value)}"


def tmux_session(value) -> str:
    return resolve(value)


def log_stem(value) -> str:
    return resolve(value)


def alive_filename(name) -> str:
    return f"{canonical(name)}.alive"


def alive_filenames(name) -> "tuple[str, ...]":
    return tuple(f"{a}.alive" for a in aliases(name))


def done_dir_names(name) -> "tuple[str, ...]":
    """`state/cores/<name>/done` candidates, canonical first."""
    return aliases(name)


# ── environment ──────────────────────────────────────────────────────────────

def from_env(env=None) -> "str | None":
    """Worker name from the process env: WORKER_ID, else WORKER_SEAT, else the
    legacy CORE_ID seat. Always canonical; None outside a pool worker."""
    env = os.environ if env is None else env
    raw = (env.get(ENV_WORKER_ID) or "").strip()
    if raw:
        return canonical(raw)
    seat = (env.get(ENV_WORKER_SEAT) or env.get(ENV_LEGACY_ID) or "").strip()
    return worker_name(seat) if _SEAT_RE.match(seat) else None


def seat_from_env(env=None) -> "int | None":
    name = from_env(env)
    return seat_of(name) if name else None


def pool_size_from_env(env=None) -> "int | None":
    env = os.environ if env is None else env
    raw = (env.get(ENV_POOL_SIZE) or env.get(ENV_LEGACY_POOL_SIZE) or "").strip()
    return int(raw) if _SEAT_RE.match(raw) else None


_CLI = {
    "worker_name": worker_name, "canonical": canonical, "resolve": resolve,
    "seat_of": seat_of, "legacy_name": legacy_name, "launchd_label": launchd_label,
    "tmux_session": tmux_session, "log_stem": log_stem,
    "alive_filename": alive_filename, "assigned_suffix": assigned_suffix,
    "claimed_suffix": claimed_suffix,
}


def _main(argv: "list[str]") -> int:
    if len(argv) == 1 and argv[0] == "from_env":
        name = from_env()
        if name is None:
            return 2
        print(name)
        return 0
    if len(argv) != 2 or argv[0] not in _CLI:
        print("usage: pool_names.py <" + "|".join(sorted(_CLI)) + "> <seat-or-name>"
              "\n       pool_names.py from_env", file=sys.stderr)
        return 2
    try:
        out = _CLI[argv[0]](argv[1])
    except ValueError as e:
        print(f"pool_names: {e}", file=sys.stderr)
        return 2
    if out is None:
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
