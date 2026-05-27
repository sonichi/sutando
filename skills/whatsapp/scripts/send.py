#!/usr/bin/env python3
"""WhatsApp send wrapper — calls wacli and records the delivery to outbox_log.

Usage:
  python3 send.py --to "+14155551234" --message "Hello!"
  python3 send.py --to "+14155551234" --message "$(cat /tmp/msg.txt)"

Records channel_type="whatsapp" in <workspace>/state/outbox.log on success.
Fails with a non-zero exit code and an error message if wacli fails.
"""

import argparse
import subprocess
import sys
from pathlib import Path

# Lazy outbox_log import — silently degrades if src/ is not importable.
_outbox_log = None


def _get_outbox_log():
    global _outbox_log
    if _outbox_log is None:
        try:
            _src = str(Path(__file__).resolve().parent.parent.parent.parent / "src")
            if _src not in sys.path:
                sys.path.insert(0, _src)
            import outbox_log as _ol
            _outbox_log = _ol
        except Exception:
            pass
    return _outbox_log


def send(to: str, message: str) -> None:
    result = subprocess.run(
        ["wacli", "send", "text", "--to", to, "--message", message],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error: {result.stderr.strip() or result.stdout.strip()}", file=sys.stderr)
        sys.exit(result.returncode)
    print(result.stdout, end="")
    ol = _get_outbox_log()
    if ol:
        ol.append(channel_type="whatsapp", recipient=to, body=message)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send a WhatsApp message via wacli.")
    parser.add_argument("--to", required=True, help="Recipient phone in E.164 format (+countrycode…) or group JID")
    parser.add_argument("--message", required=True, help="Message text to send")
    args = parser.parse_args()
    send(args.to, args.message)


if __name__ == "__main__":
    main()
