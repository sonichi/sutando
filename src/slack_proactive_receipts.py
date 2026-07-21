"""Durable idempotency receipts for Slack proactive-result delivery."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path


# Long enough to cover the result-watcher/manual-recreation race, but shorter
# than the hourly pending-question reminder cadence.  A permanent receipt would
# suppress content-keyed reminders forever because they intentionally reuse the
# same filename while a question remains unanswered.
RECEIPT_TTL_SECONDS = 5 * 60


def _receipt_path(state_dir: Path, delivery_id: str) -> Path:
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
    return state_dir / "slack-proactive-delivered" / f"{digest}.sentinel"


def was_delivered(state_dir: Path, delivery_id: str) -> bool:
    """Return whether this proactive filename was delivered very recently."""
    try:
        receipt = _receipt_path(state_dir, delivery_id)
        if not receipt.exists():
            return False
        if time.time() - receipt.stat().st_mtime <= RECEIPT_TTL_SECONDS:
            return True
        receipt.unlink(missing_ok=True)
        return False
    except Exception:
        return False


def _prune_expired(receipt_dir: Path) -> None:
    cutoff = time.time() - RECEIPT_TTL_SECONDS
    for receipt in receipt_dir.glob("*.sentinel"):
        try:
            if receipt.stat().st_mtime < cutoff:
                receipt.unlink(missing_ok=True)
        except Exception:
            continue


def mark_delivered(state_dir: Path, delivery_id: str) -> None:
    """Persist a delivery receipt immediately after Slack confirms the send."""
    try:
        receipt = _receipt_path(state_dir, delivery_id)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        _prune_expired(receipt.parent)
        receipt.write_text(delivery_id + "\n")
    except Exception:
        # Receipt failure must not turn a successful Slack send into an error.
        pass
