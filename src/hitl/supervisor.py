"""Runtime supervisor pass: detector -> manager -> projector, one turn.

supervise_once() is the whole integration seam: the caller (a poll loop, the
watchdog, a bridge pulse — anything periodic) provides the manager, the room,
and the sender; this module owns only the ordering. Detection converges
requirement state to reality first, then projection pushes every un-projected
revision out. Both halves are idempotent, so overlapping or repeated calls
are safe by construction — the worst case is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .detector import DriveOutcome, Runner, _default_runner, drive
from .manager import HitlManager
from .projector import Sender, project


@dataclass
class SuperviseOutcome:
    drove: DriveOutcome = field(default_factory=DriveOutcome)
    projected: List[Tuple[str, Optional[str]]] = field(default_factory=list)
    # Task ids whose blocking requirement resolved this pass — the caller
    # owns resumption (requeue, notify, or ignore).
    resumed_tasks: List[str] = field(default_factory=list)


def supervise_once(
    manager: HitlManager,
    send: Sender,
    room_id: str,
    device: Optional[Dict[str, str]] = None,
    runner: Runner = _default_runner,
) -> SuperviseOutcome:
    out = SuperviseOutcome()
    out.drove = drive(manager, device=device, runner=runner)
    out.resumed_tasks = list(out.drove.resumed_tasks)
    out.projected = project(manager, send, room_id)
    return out
