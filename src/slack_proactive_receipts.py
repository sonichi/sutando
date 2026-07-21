"""Durable idempotency receipts for Slack proactive-result delivery."""

from __future__ import annotations

import hashlib
from pathlib import Path


def _receipt_path(state_dir: Path, delivery_id: str) -> Path:
    digest = hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()
    return state_dir / "slack-proactive-delivered" / f"{digest}.sentinel"


def was_delivered(state_dir: Path, delivery_id: str) -> bool:
    """Return whether this stable proactive filename was already delivered."""
    try:
        return _receipt_path(state_dir, delivery_id).exists()
    except Exception:
        return False


def mark_delivered(state_dir: Path, delivery_id: str) -> None:
    """Persist a delivery receipt immediately after Slack confirms the send."""
    try:
        receipt = _receipt_path(state_dir, delivery_id)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(delivery_id + "\n")
    except Exception:
        # Receipt failure must not turn a successful Slack send into an error.
        pass
