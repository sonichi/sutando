#!/usr/bin/env python3
"""Regression tests for recreated proactive-result delivery IDs."""

import importlib.util
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "slack_proactive_receipts",
    REPO / "src" / "slack_proactive_receipts.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def main():
    state = Path(tempfile.mkdtemp(prefix="sutando-slack-proactive-receipt-"))
    delivery_id = "proactive-daily-top-ai-news-1784638800.txt"

    assert not module.was_delivered(state, delivery_id)
    module.mark_delivered(state, delivery_id)
    assert module.was_delivered(state, delivery_id)

    # Recreating the same deterministic filename must remain a duplicate even
    # if its content changes. A genuinely new schedule slot gets a new ID.
    assert module.was_delivered(state, delivery_id)
    assert not module.was_delivered(
        state,
        "proactive-daily-top-ai-news-1784725200.txt",
    )

    # Receipt paths are hashes, so a malformed filename cannot escape state/.
    hostile = "../../outside.txt"
    module.mark_delivered(state, hostile)
    assert module.was_delivered(state, hostile)
    assert not (state.parent / "outside.txt").exists()

    bridge = (REPO / "src" / "slack-bridge.py").read_text()
    check_pos = bridge.index("proactive_was_delivered(STATE_DIR, delivery_id)")
    claim_pos = bridge.index("f.rename(claim)", check_pos)
    send_pos = bridge.index("_send_reply(dm_channel", claim_pos)
    mark_pos = bridge.index("mark_proactive_delivered(STATE_DIR, delivery_id)", send_pos)
    assert check_pos < claim_pos < send_pos < mark_pos

    print("PASS: recreated Slack proactive delivery IDs are suppressed durably")


if __name__ == "__main__":
    main()
