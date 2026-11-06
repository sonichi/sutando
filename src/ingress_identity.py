"""Provider-event ingress admission — the shared policy behind Slice 3.

A bridge that receives a provider event derives its task id injectively from
the event (never the wall clock), then asks whether that id was already
admitted anywhere in its lifecycle; a replay is skipped, which is Sutando's
idempotent re-ack of a provider retry. Adapters inject their resolved
directories and archive layout; this module owns only the policy.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

# Reserved grammar separators, escaped inside a component so the id stays
# injective; '%' escapes itself so decoding is unambiguous.
_RESERVED = "%@#+~:"


def escape_component(raw: str) -> str:
    """Injective: printable ASCII passes; reserved, whitespace, path separators
    and every non-ASCII char become fixed-width uppercase %XX per UTF-8 byte."""
    if not isinstance(raw, str) or not raw:
        raise ValueError("identity component must be a non-empty string")
    out = []
    for ch in raw:
        if 0x21 <= ord(ch) <= 0x7E and ch not in _RESERVED and ch not in "/\\":
            out.append(ch)
        else:
            out.extend(f"%{b:02X}" for b in ch.encode("utf-8"))
    return "".join(out)


def provider_task_id(instance_label: str, provider_event_id: str) -> str:
    """Injective ingress id for one provider event on one receiving instance:
    task-<instance>~<event>, the ag2space shape generalized. Same event
    replayed -> same id, across restarts and hosts."""
    return f"task-{escape_component(instance_label)}~{escape_component(provider_event_id)}"


def already_admitted(task_id: str, tasks_dir: Path, results_dir: Path,
                     archive_probe: "Callable[[str], bool] | None" = None) -> bool:
    """True when the task exists pending/claimed, has a result, or the
    adapter's archive_probe finds it archived."""
    # "." delimiter anchors the exact id (forms are all dot-led); a bare `{id}*`
    # also matches a LONGER id sharing this prefix — a different event dropped.
    if any(tasks_dir.glob(f"{task_id}.*")):
        return True
    if (results_dir / f"{task_id}.txt").exists():
        return True
    return bool(archive_probe and archive_probe(task_id))
