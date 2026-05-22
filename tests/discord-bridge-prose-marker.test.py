#!/usr/bin/env python3
"""Regression guard for the prose-quoted file-marker false positive.

An agent's reply body sometimes contains the inline-code substring
``` `[file: /tmp/sutando-x.png]` ``` as part of prose explaining the
marker convention. The bridge's regex extracts ANY
`[file:|send:|attach:]` substring regardless of markdown context
(inline code, code fence, blockquote, etc.), so it tried to send the
referenced file — which doesn't exist for prose-quoted markers — and
shipped a `(file not found: ...)` Discord/Telegram message as
confusing noise after the agent's clean reply.

A markdown-aware regex is the principled fix but a much larger
change. The minimal user-impact fix is to STOP shipping the warning
message to the user when the extracted path doesn't exist — those
are almost always false-positive extractions from prose, and the
warning is worse-than-useless noise. Operators retain visibility via
stderr logs for real typos.

`(file not allowed: ...)` (allowlist rejection) is NOT silenced —
that's a security signal worth surfacing.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC_DISCORD = (REPO / "src" / "discord-bridge.py").read_text()
SRC_TELEGRAM = (REPO / "src" / "telegram-bridge.py").read_text()


def test_no_send_sites_for_file_not_found_remain_in_discord():
    """All three send paths (poll_results, poll_proactive,
    poll_dm_fallback) previously called `channel.send(f"(file not
    found: ...)")` directly. After this PR, none should remain — the
    user-facing warning is replaced with a stderr log."""
    bad_patterns = [
        '.send(f"(file not found: ',
        ".send(f'(file not found: ",
    ]
    for pat in bad_patterns:
        assert pat not in SRC_DISCORD, (
            f"discord-bridge.py still contains {pat!r} — a send call would "
            "surface the false-positive warning to the user."
        )


def test_telegram_bridge_aligned():
    """Telegram bridge had the same pattern. Pin that it also no longer
    surfaces the false-positive to the user."""
    bad_patterns = [
        'api("sendMessage", chat_id=chat_id, text=f"(file not found: ',
        "api('sendMessage', chat_id=chat_id, text=f'(file not found: ",
    ]
    for pat in bad_patterns:
        assert pat not in SRC_TELEGRAM, (
            f"telegram-bridge.py still surfaces (file not found:) — {pat!r}"
        )
    # access-denied (the security-relevant case) still surfaces.
    assert "(file access denied: " in SRC_TELEGRAM, (
        "telegram-bridge dropped the security-relevant access-denied warning"
    )


def test_file_not_allowed_still_surfaces_to_user():
    """Defensive: the `(file not allowed: ...)` path MUST still send
    to the user. It's a security signal (someone's trying to exfil a
    file outside the allowlist) and silencing it would degrade
    operator + user awareness."""
    assert '.send(f"(file not allowed: ' in SRC_DISCORD, (
        "(file not allowed: ...) warning was removed too — that's a "
        "security signal and must stay user-visible"
    )


def main():
    test_no_send_sites_for_file_not_found_remain_in_discord()
    test_telegram_bridge_aligned()
    test_file_not_allowed_still_surfaces_to_user()
    print("All prose-marker regression-guard tests passed.")


if __name__ == "__main__":
    main()
