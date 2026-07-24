#!/usr/bin/env python3
"""Send email via macOS Mail.app. Usage: python3 email-sender.py "to" "subject" "body" [--draft] [--cc "cc"]"""
import subprocess
import sys

def escape(s): return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

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
