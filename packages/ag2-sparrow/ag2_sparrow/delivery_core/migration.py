"""Migration fencing: single-protocol-per-epoch (seam doc §4).

One logical item is interpreted by exactly ONE claim protocol within an
epoch. Migration = lock out drainers -> one-shot convert (each item
individually atomic) -> write the version fence -> start only the new
drainer. The fence is written LAST: a crash anywhere mid-conversion leaves
the fence at the old epoch, so the old protocol stays authoritative and no
item is ever interpreted by both protocols.

Legacy delivered-sentinels map conservatively: a sentinel written after the
provider call returned is evidence, not proof (the crash window between
API-return and sentinel-write means its absence proves nothing and its
presence only witnesses the call returned). Only a durable provider receipt
reference upgrades to CONFIRMED; anything else converts to OUTCOME_UNKNOWN
for park/reconcile — never CONFIRMED (seam doc §4, Discord's sentinel).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable, Optional

from .contract import BackendCapabilities, CleanupReport, DeliveryOutcome

EPOCH_FILE = "protocol-epoch"
DEFAULT_EPOCH = "A"


def read_epoch(root: Path) -> str:
    try:
        return (Path(root) / EPOCH_FILE).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return DEFAULT_EPOCH


EPOCH_OK = "ok"
EPOCH_MISSING = "missing"
EPOCH_UNREADABLE = "unreadable"
EPOCH_UNKNOWN = "unknown"
EPOCH_STAGED = "staged"
SUPPORTED_EPOCHS = ("A", "C")


def staged_fence_path(root: Path) -> Path:
    """The temp name write_fence() stages at — same derivation, one place."""
    p = Path(root) / EPOCH_FILE
    return p.with_name(p.name + ".tmp")


def classify_epoch(root: Path) -> "tuple[str, str]":
    """(state, value) for the protocol fence. NEVER raises: selection runs
    before any result poller, and invalid UTF-8 is a ValueError not an OSError."""
    p = Path(root) / EPOCH_FILE
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        # lexists distinguishes "no fence" from a DANGLING link, which is
        # anomalous state someone created — never a clean-root bootstrap.
        if os.path.lexists(p):
            return (EPOCH_UNREADABLE, "")
        # A staged temp means write_fence() was interrupted before its
        # os.replace: mid-transition, never an untouched clean root.
        if os.path.lexists(staged_fence_path(root)):
            return (EPOCH_STAGED, "")
        return (EPOCH_MISSING, DEFAULT_EPOCH)
    except (OSError, ValueError):
        return (EPOCH_UNREADABLE, "")
    value = raw.strip()
    if value in SUPPORTED_EPOCHS:
        return (EPOCH_OK, value)
    return (EPOCH_UNKNOWN, value)


def c_selection_allowed(root: Path, root_is_clean: bool) -> "tuple[bool, str]":
    """(allow C, reason). Fail closed: only an explicit C fence, or a clean
    root bootstrapping one, may start C. Unknown/unreadable defers to A."""
    state, value = classify_epoch(root)
    if state == EPOCH_OK and value == "C":
        return (True, "epoch=C")
    if state == EPOCH_OK:
        return (False, f"epoch={value} is authoritative")
    if state == EPOCH_MISSING:
        if root_is_clean:
            return (True, "clean-root bootstrap (no fence, no A entries)")
        return (False, "no fence and the root holds A entries")
    if state == EPOCH_STAGED:
        return (False, "an interrupted epoch write is staged; not a clean root")
    return (False, f"epoch {state}")


def write_fence(root: Path, epoch: str) -> None:
    p = Path(root) / EPOCH_FILE
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(epoch, encoding="utf-8")
    os.replace(tmp, p)


def classify_legacy_sentinel(
        receipt_ref: Optional[str]) -> DeliveryOutcome:
    """Sentinel-mapping table (normative). receipt_ref is the provider's
    durable receipt reference when the legacy record carries one; a bare
    sentinel (written after the call, no receipt) is OUTCOME_UNKNOWN."""
    if receipt_ref:
        return DeliveryOutcome.CONFIRMED
    return DeliveryOutcome.OUTCOME_UNKNOWN


def convert_item_atomic(path: Path, render: Callable[[bytes], bytes],
                        is_converted: Callable[[bytes], bool],
                        fault: Optional[Callable[[str], None]] = None) -> bool:
    """The per-item conversion primitive: read -> write temp -> ONE
    os.replace over the SAME path. There is no unlink step and no second
    visible name, so no crash point can leave old and new both visible.
    Idempotent: an already-converted item is left untouched (False), which
    is what lets a restarted migrator resume mid-pass. `fault` is the
    fault-injection hook — called before each internal mutation with a
    step label; tests raise from it to crash INSIDE the item."""
    data = path.read_bytes()
    if is_converted(data):
        return False
    tmp = path.with_name(path.name + ".tmp")
    if fault:
        fault("pre-write-tmp")
    tmp.write_bytes(render(data))
    if fault:
        fault("pre-replace")
    os.replace(tmp, path)
    if fault:
        fault("post-replace")
    return True


def convert_epoch(root: Path, items: Iterable[str],
                  convert_one: Callable[[str], None], target_epoch: str,
                  crash_after: Optional[int] = None) -> int:
    """One-shot conversion pass. convert_one(item) must be per-item atomic
    over a single path (use convert_item_atomic — one os.replace, no
    unlink, no second visible name) and idempotent, so a restarted pass
    resumes over the same item list and completes BEFORE any drainer
    starts: the fence is written only after ALL items converted, and until
    then read_epoch still names the old protocol. crash_after=N simulates
    a crash before the (N+1)th item; intra-item crash points are the
    convert_item_atomic fault hook."""
    done = 0
    for item in items:
        if crash_after is not None and done >= crash_after:
            raise RuntimeError("simulated crash mid-conversion")
        convert_one(item)
        done += 1
    write_fence(root, target_epoch)
    return done


def c_live_state(root: Path) -> "list[str]":
    """Evidence Design C has OPERATED on this root: entries in its live
    namespaces, or the epoch fence naming C. Fences the REVERSE transition
    (C -> A): A over such a root resets the durable retry budget (a body C
    parked at N attempts retries from zero) and resurrects parked items.
    Read-only; an unreadable namespace is REPORTED as state (fail closed)."""
    root = Path(root)
    found = []
    _state, _value = classify_epoch(root)
    if _state == EPOCH_OK and _value == "C":
        found.append("epoch=C")
    elif _state in (EPOCH_UNREADABLE, EPOCH_UNKNOWN):
        # Ambiguous C-side state is not proof of absence — fail closed.
        found.append(f"epoch({_state})")
    for name in ("ready", "inflight", "undelivered", "attempts"):
        d = root / name
        try:
            if d.is_dir() and not d.is_symlink():
                if any(d.iterdir()):
                    found.append(name)
            elif os.path.lexists(d):
                # A file, symlink, or anything else at a C namespace name is
                # unrecognized C-side state — fail closed on lexical presence.
                found.append(f"{name}(unrecognized)")
        except OSError:
            found.append(f"{name}(unreadable)")
    return found


class TransitionRefusalBackend:
    """ClaimBackend that claims nothing: installed when a root cannot be
    safely served by the selected protocol (e.g. A over live C state).
    Bodies stay queued on disk — delivery DEFERS, never duplicates."""
    persists_receipt_metadata = False
    refuses_claims = True

    def __init__(self, reason: str):
        self.reason = reason

    capabilities = BackendCapabilities(supports_force_release=False)

    def cleanup(self):
        return CleanupReport(0, f"refusal backend: {self.reason}")

    def force_release(self, item_id):
        raise NotImplementedError(
            "TransitionRefusalBackend refuses claims; nothing to force-release")

    def publish(self, item_id, payload):
        return False                       # record kept where it already is

    def claim(self, item_id, worker):
        return None                        # nothing is ever processed

    def complete(self, token, outcome, **kw):
        return False

    def park(self, item_id, reason):
        return None

    def attempts(self, item_id):
        return 0

    # Refusal DEFERS, it does not decide: a queued body will be claimed once the
    # root is servable, so reporting terminal here would strand it permanently.
    def is_terminal(self, item_id):
        return False

    def recover(self):
        from .contract import RecoverReport
        return RecoverReport()
