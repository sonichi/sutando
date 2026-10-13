#!/usr/bin/env python3
"""A per-run macOS Seatbelt (`sandbox-exec`) profile that fences one CLI run.

The profile starts from `(deny default)`. Every process in the run may read the
system's own libraries and tools and read/write two per-run dirs: the work dir
(`cwd`) and a scratch temp dir. Nothing else under HOME, the repo, the workspace,
other temp dirs, /Library, /Applications or /opt is readable or writable.

The tool binary itself (matched by `process-path`) may additionally read its own
install dir and the files listed in `tool_read`, and read/write `tool_rw` (its
private state dir). Never put a credential file there: in-process tools can move it. A
child it spawns (a shell, cat, grep) matches a different path and gets none of
that, and no process may exec the tool binary or sandbox-exec again.

Network: outbound TCP to port 443 on any host, plus DNS through mDNSResponder.
Seatbelt cannot filter remote hosts other than "localhost", so 443 on a LAN host
stays reachable. Outbound loopback is denied to every process, so no local
service is reachable; `loopback=True` only lets the tool binary itself listen on
127.0.0.1 (agy serves its own agent API there).

Stdlib only: vendored verbatim into ag2-sparrow beside sandbox_runner.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Read-only for every process in the run: the OS, its shells and tools, zoneinfo.
SYSTEM_READ = (
    "/System", "/usr/lib", "/usr/share", "/usr/libexec", "/usr/bin", "/usr/sbin",
    "/bin", "/sbin", "/etc", "/var/db/timezone", "/var/select",
    "/Library/Preferences/Logging",
)
# Executable by children when `exec_children`: the OS shells and tools only.
SYSTEM_EXEC = ("/bin", "/usr/bin", "/usr/libexec", "/usr/sbin", "/sbin")
DEVICES_RW = ("/dev/null", "/dev/zero", "/dev/tty", "/dev/ptmx", "/dev/dtracehelper")
DEVICES_R = ("/dev/random", "/dev/urandom")
# Every process: name lookup, logging, notifications. Tool binary only: TLS trust
# and network configuration.
MACH_ALL = (
    "com.apple.system.opendirectoryd.libinfo", "com.apple.system.logger",
    "com.apple.logd", "com.apple.system.notification_center",
    "com.apple.diagnosticd",
)
MACH_TOOL = (
    "com.apple.SystemConfiguration.configd", "com.apple.trustd", "com.apple.trustd.agent",
    "com.apple.SecurityServer",
)
MDNS_SOCKET = "/var/run/mDNSResponder"
# The root symlinks into /private: resolving /etc/hosts or /var/folders needs them.
ROOT_LINKS = ("/etc", "/var", "/tmp")


def available(platform: str = sys.platform) -> bool:
    """True on macOS with sandbox-exec present."""
    return platform == "darwin" and os.access(SANDBOX_EXEC, os.X_OK)


def _q(path: str) -> str:
    """An SBPL string literal; a path that needs escaping is refused, not escaped."""
    if not path or any(c in path for c in '"\\') or not path.isprintable():
        raise ValueError(f"unsupported path for a sandbox profile: {path!r}")
    return f'"{path}"'


def _real(path) -> str:
    """Seatbelt matches resolved paths, so /etc and /var are given as their targets."""
    return os.path.realpath(str(path))


def _ancestors(paths: Iterable[str]) -> list:
    out = set()
    for p in paths:
        for parent in Path(p).parents:
            out.add(str(parent))
    return sorted(out)


def profile(*, cwd, scratch, tool_binary, tool_dir=None, tool_read=(), tool_rw=(),
            helpers=(), loopback: bool = False, exec_children: bool = True) -> str:
    """The SBPL text for one run. `cwd` and `scratch` are fresh per-run dirs that
    every process may read and write; `helpers` are executables only the tool may
    start, which run with the shared rights, not the tool's. The rest is described
    in the module doc."""
    cwd, scratch, tool = _real(cwd), _real(scratch), _real(tool_binary)
    tool_dir = _real(tool_dir) if tool_dir else str(Path(tool).parent)
    shared_rw = [cwd, scratch]
    system_read = [_real(p) for p in SYSTEM_READ]
    tool_r = [tool_dir, *(_real(p) for p in tool_read)]
    tool_w = [_real(p) for p in tool_rw]
    me = f"(process-path {_q(tool)})"

    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-fork)",
        "(allow signal (target same-sandbox))",
        "(allow process-info* (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow pseudo-tty)",
        '(allow file-ioctl (literal "/dev/tty") (regex #"^/dev/ttys[0-9]+$"))',
        # stat (not readdir) on the ancestors of every allowed path: path resolution
        # and getcwd need it; the names they reveal are the allowed paths themselves.
        "(allow file-read-metadata " + " ".join(
            f"(literal {_q(p)})" for p in [*ROOT_LINKS, *_ancestors(shared_rw + tool_r + tool_w + system_read)]) + ")",
        # dyld reads the root directory itself (its entry names, nothing below).
        '(allow file-read* (literal "/") ' + " ".join(f"(subpath {_q(p)})" for p in system_read) + ")",
        "(allow file-read* file-write* " + " ".join(f"(literal {_q(p)})" for p in DEVICES_RW)
        + ' (regex #"^/dev/ttys[0-9]+$"))',
        "(allow file-read* " + " ".join(f"(literal {_q(p)})" for p in DEVICES_R) + ")",
        "(allow file-read* file-write* " + " ".join(f"(subpath {_q(p)})" for p in shared_rw) + ")",
        f"(allow file-read* (require-all {me} (require-any "
        + " ".join(f"(subpath {_q(p)})" for p in tool_r) + ")))",
        "(allow mach-lookup " + " ".join(f'(global-name "{m}")' for m in MACH_ALL) + ")",
        '(allow ipc-posix-shm-read* (ipc-posix-name "apple.shm.notification_center"))',
        "(allow network-outbound (remote tcp \"*:443\"))",
        # Later rules win: no local service, even one on 443.
        '(deny network-outbound (remote ip "localhost:*"))',
        f"(allow network-outbound (remote unix-socket (path-literal {_q(_real(MDNS_SOCKET))})))",
        "(allow system-socket)",
    ]
    lines.append(f"(allow mach-lookup (require-all {me} (require-any "
                 + " ".join(f'(global-name "{m}")' for m in MACH_TOOL) + ")))")
    if tool_w:
        lines.append(f"(allow file-read* file-write* (require-all {me} (require-any "
                     + " ".join(f"(subpath {_q(p)})" for p in tool_w) + ")))")
    for h in (_real(h) for h in helpers):
        lines.append(f"(allow file-read* (literal {_q(h)}))")
        lines.append(f"(allow process-exec (require-all (literal {_q(h)}) {me}))")
    if exec_children:
        lines.append("(allow process-exec " + " ".join(f"(subpath {_q(p)})" for p in SYSTEM_EXEC) + ")")
        lines.append(f"(deny process-exec (literal {_q(SANDBOX_EXEC)}) (literal {_q(tool)}))")
    # Only sandbox-exec itself may start the tool: no child re-runs it to inherit
    # its reads, and a nested sandbox-exec is refused by the kernel anyway.
    lines.append(f"(allow process-exec (require-all (literal {_q(tool)}) "
                 f'(process-path "{SANDBOX_EXEC}")))')
    if loopback:
        lines += [
            f'(allow network-bind (require-all {me} (local ip "localhost:*")))',
            f'(allow network-inbound (require-all {me} (local ip "localhost:*")))',
        ]
    return "\n".join(lines) + "\n"


def wrap(profile_path: str, argv: list) -> list:
    """argv run under the profile at `profile_path`."""
    return [SANDBOX_EXEC, "-f", profile_path, *argv]
