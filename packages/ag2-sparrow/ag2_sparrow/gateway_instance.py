"""Named gateway instances: how one bridge's task ids and outbox stay apart from another's.

One host may run several bridges, each against its own gateway (GATEWAY_INSTANCE).
Broker ids are unique only within a gateway, so a named instance stores tasks as
`task-<instance>~<broker id>` and delivers results from `<results>/.outbox.<instance>`.
`~` is outside both the broker id and the instance charsets, so the mapping is
injective and the first `~` splits it. The empty instance is the primary: identity.
"""
from __future__ import annotations

import hashlib
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


def binding_digest(identity: str, gateway_url: str) -> str:
    """sha256 of an agent identity and the FULL gateway URL: what binds a saved reading to
    one enrollment without persisting the URL; '' when either is missing."""
    if not identity or not gateway_url:
        return ""
    return hashlib.sha256(f"{identity}\n{gateway_url}".encode("utf-8")).hexdigest()
