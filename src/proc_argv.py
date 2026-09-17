#!/usr/bin/env python3
"""The argv of one pid as a LIST — the authoritative form of a process identity.

WHY THIS MODULE EXISTS. A flattened `ps -o args=` string cannot separate an
operand containing a space from two operands, so once anything follows the
script no rule over that text can name the executed script: the Codex notifier's
`/bin/bash <script> <tasks-dir>` and the core Monitor's `bash <script> <tasks-dir>`
read exactly like a shell running a script whose path merely contains a space.
The kernel kept the boundaries — Linux exposes them NUL-delimited in
/proc/<pid>/cmdline, macOS through sysctl KERN_PROCARGS2 (argc, the exec path,
padding, then argv, then envp) — and this is the ONE reader of them, so the
signaller (src/watcher_identity.sh, via src/process-ops.sh) and the reporter
(src/health-check.py) cannot disagree about one pid's argv.

`argv_vector(pid)` returns that list, or None when no authoritative read exists
(no such pid, a pid this user may not inspect, an unsupported platform). None is
the caller's cue to fall back to the flattened string AND to keep treating an
operand-bearing one as unprovable — never to split the text.

CLI, for the shell seam:
  proc_argv.py <pid>   -> a JSON list on stdout, exit 0; nothing and exit 1
                          when the vector cannot be read; exit 2 on a bad pid.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# CTL_KERN, KERN_PROCARGS2: sysctl reads any process this user owns.
_KERN_PROCARGS2_MIB = (1, 49)
_PROCARGS_BUF = 262144


def _linux_vector(pid: int) -> "list[str] | None":
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return [a for a in raw.decode("utf8", "replace").split("\0") if a]


def _darwin_vector(pid: int) -> "list[str] | None":
    import ctypes
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    mib = (ctypes.c_int * 3)(*_KERN_PROCARGS2_MIB, int(pid))
    size = ctypes.c_size_t(_PROCARGS_BUF)
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None
    data = buf.raw[:size.value]
    argc = int.from_bytes(data[:4], sys.byteorder)
    parts = data[4:].split(b"\0")
    i = 0
    while i < len(parts) and parts[i] == b"":
        i += 1
    i += 1                                   # the exec path
    while i < len(parts) and parts[i] == b"":
        i += 1
    out: list[str] = []
    while i < len(parts) and len(out) < argc:
        out.append(parts[i].decode("utf8", "replace"))
        i += 1
    return out or None


def argv_vector(pid: int) -> "list[str] | None":
    """Real argv of `pid` as a LIST, or None when no authoritative read exists."""
    try:
        vec = _linux_vector(pid)
        if vec:
            return vec
    except Exception:  # noqa: BLE001 -- not linux, or gone
        pass
    try:
        return _darwin_vector(pid)
    except Exception:  # noqa: BLE001 -- a probe failure must read as unreadable, never partial
        return None


def main(argv_in: "list[str] | None" = None) -> int:
    args = sys.argv[1:] if argv_in is None else argv_in
    if len(args) != 1 or not args[0].isdigit() or int(args[0]) <= 0:
        print("usage: proc_argv.py <pid>", file=sys.stderr)
        return 2
    vec = argv_vector(int(args[0]))
    if vec is None:
        return 1
    print(json.dumps(vec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
