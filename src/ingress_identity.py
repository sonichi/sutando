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

from ag2_sparrow.identity import ingress_task_id


def provider_task_id(instance_label: str, provider_event_id: str) -> str:
    """Injective ingress id for one provider event on one receiving instance.
    Same event replayed -> same id, across restarts and hosts."""
    return ingress_task_id(instance_label, provider_event_id).value


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
