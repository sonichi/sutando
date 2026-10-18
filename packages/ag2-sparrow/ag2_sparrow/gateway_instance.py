"""Named gateway instances: how one bridge's task ids and outbox stay apart from another's.

One host may run several bridges, each against its own gateway (GATEWAY_INSTANCE).
Broker ids are unique only within a gateway, so a named instance stores tasks as
`task-<instance>~<broker id>` and delivers results from `<results>/.outbox.<instance>`.
`~` is outside both the broker id and the instance charsets, so the mapping is
injective and the first `~` splits it. The empty instance is the primary: identity.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
import tempfile
import time
import urllib.parse
from pathlib import Path

from . import holder_lock

INSTANCE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def suffix(instance: str) -> str:
    return f".{instance}" if instance else ""


def local_tid(broker_tid: str, instance: str) -> str:
    return f"task-{instance}~{broker_tid}" if instance else broker_tid


def broker_tid(local_tid: str, instance: str) -> str:
    prefix = f"task-{instance}~"
    if instance and local_tid.startswith(prefix):
        return local_tid[len(prefix):]
    return local_tid


def owns(local_tid: str, instance: str) -> bool:
    """Whether `instance` wrote this local id: its own prefix, or no `~` for the primary."""
    return local_tid.startswith(f"task-{instance}~") if instance else "~" not in local_tid


def outbox_root(results_dir: Path, instance: str) -> Path:
    """The outbox the bridge for `instance` delivers results from."""
    return Path(results_dir) / f".outbox{suffix(instance)}"


REDACTED_URL = "<unparseable gateway URL>"


def redact_url(value: str) -> str:
    """Scheme, host, port and path of a gateway URL, as Sparrow persists and logs it: userinfo,
    query and fragment dropped. Fails closed: REDACTED_URL when it does not parse as one."""
    try:
        p = urllib.parse.urlsplit(str(value))
        if not p.scheme or not p.hostname:
            return REDACTED_URL
        host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
        if p.port:
            host = f"{host}:{p.port}"
        return urllib.parse.urlunsplit((p.scheme, host, p.path, "", ""))
    except Exception:  # noqa: BLE001 - redaction must never break status I/O
        return REDACTED_URL


KEY_GRACE_S = 5.0


def binding_key_path() -> Path:
    """The per-install key the binding is computed with: in the config dir under the effective
    uid's passwd home (holder_lock's), so every launch context resolves one key; never the
    state dir, which may be synced."""
    return Path(holder_lock._home()) / ".config" / "ag2-sparrow" / "binding.key"


def _read_key(path: Path) -> "bytes | None":
    """The key, only from a regular file this uid owns with mode 0600 and 64 hex digits."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="ascii") as fh:
            st = os.fstat(fh.fileno())
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                    or stat.S_IMODE(st.st_mode) != 0o600):
                return None
            key = fh.read(256).strip()
    except (OSError, UnicodeDecodeError):
        return None
    return key.encode("ascii") if re.fullmatch(r"[0-9a-f]{64}", key) else None


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _binding_key(create: bool) -> "bytes | None":
    try:
        path = binding_key_path()
    except holder_lock.LockError:
        return None
    key = _read_key(path)
    if key is not None or not create:
        return key
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    except OSError:
        return None
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(os.urandom(32).hex())
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, str(path))  # the key appears whole or not at all
        except FileExistsError:
            key = _read_key(path)
            if key is not None:
                return key
            # A bad key younger than the grace may be an older writer mid-write.
            if time.time() - path.stat().st_mtime < KEY_GRACE_S:
                return None
            os.replace(tmp, str(path))
        _fsync_dir(path.parent)
        return _read_key(path)
    except OSError:
        return None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def binding_digest(identity: str, gateway_url: str, create: bool = False) -> str:
    """HMAC-SHA256, under the per-install key, of an agent identity and the FULL gateway
    URL: binds a saved reading to one enrollment without persisting anything the URL could
    be recovered from. '' when either is missing or there is no key; only the owner-routing
    writer creates one (create=True)."""
    key = _binding_key(create) if identity and gateway_url else None
    if key is None:
        return ""
    return hmac.new(key, f"{identity}\n{gateway_url}".encode("utf-8"), hashlib.sha256).hexdigest()
