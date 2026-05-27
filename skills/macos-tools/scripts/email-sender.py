#!/usr/bin/env python3
"""Send email via macOS Mail.app. Usage: python3 email-sender.py "to" "subject" "body" [--draft] [--cc "cc"]"""
import subprocess, sys
from pathlib import Path

def escape(s): return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


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


def send(to, subject, body, cc=None, draft=False):
    action = "save m" if draft else "send m"
    visible = "true" if draft else "false"
    cc_block = ""
    if cc:
        cc_block = f'\n        make new cc recipient at end of cc recipients with properties {{address:"{escape(cc)}"}}'
    script = f'''tell application "Mail"
    set m to make new outgoing message with properties {{subject:"{escape(subject)}", content:"{escape(body)}", visible:{visible}}}
    tell m
        make new to recipient at end of to recipients with properties {{address:"{escape(to)}"}}{cc_block}
    end tell
    {action}
end tell'''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print(f"Error: {r.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    print("Draft created." if draft else "Email sent.")
    if not draft:
        ol = _get_outbox_log()
        if ol:
            ol.append(channel_type="email", recipient=to, body=body)

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python3 email-sender.py 'to' 'subject' 'body' [--draft] [--cc 'cc']")
        sys.exit(1)
    to, subject, body = sys.argv[1], sys.argv[2], sys.argv[3]
    draft = "--draft" in sys.argv
    cc = None
    if "--cc" in sys.argv:
        idx = sys.argv.index("--cc")
        cc = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
    send(to, subject, body, cc, draft)
