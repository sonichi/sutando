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
import urllib.parse
from pathlib import Path

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


def redact_url(value: str) -> str:
    """Scheme, host, port and path of a gateway URL, as Sparrow persists it: userinfo,
    query and fragment dropped. The bare string when it does not parse."""
    try:
        p = urllib.parse.urlsplit(str(value))
        if not p.scheme and not p.netloc:
            return str(value)
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
        return urllib.parse.urlunsplit((p.scheme, host, p.path, "", ""))
    except Exception:  # noqa: BLE001 - redaction must never break status I/O
        return str(value)


def binding_key_path() -> Path:
    """The per-install key the binding is computed with: in the user's config dir
    (XDG_CONFIG_HOME, else ~/.config), never the state dir, which may be synced."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "ag2-sparrow" / "binding.key"


def _binding_key(create: bool) -> "bytes | None":
    path = binding_key_path()
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(os.urandom(32).hex())
    try:
        key = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return key.encode("ascii") if re.fullmatch(r"[0-9a-f]{64}", key) else None


def binding_digest(identity: str, gateway_url: str, create: bool = False) -> str:
    """HMAC-SHA256, under the per-install key, of an agent identity and the FULL gateway
    URL: binds a saved reading to one enrollment without persisting anything the URL could
    be recovered from. '' when either is missing or there is no key; only the owner-routing
    writer creates one (create=True)."""
    key = _binding_key(create) if identity and gateway_url else None
    if key is None:
        return ""
    return hmac.new(key, f"{identity}\n{gateway_url}".encode("utf-8"), hashlib.sha256).hexdigest()
