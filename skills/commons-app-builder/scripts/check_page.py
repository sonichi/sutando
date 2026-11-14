#!/usr/bin/env python3
"""Static pre-flight for a Commons app page against GUIDE.md's hard rules.

Catches what a read of the source can prove (sandbox-blocked APIs, missing viewport or dark
mode, no consent retry, a hard-coded database id). It is no substitute for rendering the page.

Usage: python3 check_page.py page.html [--json]
Exit 0 = no errors (warnings may remain), 1 = errors, 2 = unreadable input.
"""
from __future__ import annotations

import json
import re
import sys
from typing import List, Tuple

MAX_BODY_BYTES = 196_608

# (rule id, regex, message): any match is an error.
BLOCKED: List[Tuple[str, str, str]] = [
    ("no-network", r"\bfetch\s*\(|XMLHttpRequest|new\s+WebSocket|EventSource\s*\(",
     "the sandbox has no network: keep data in databases or artifact.state"),
    ("no-modals", r"(?<![\w.])(?<!function )(?:window\.)?(?:alert|confirm|prompt)\s*\(",
     "alert/confirm/prompt are blocked in the sandbox: use inline UI"),
    ("no-form-submit", r"<form\b", "a <form> never submits in the sandbox: use buttons and Enter handlers"),
    ("no-history", r"history\.(?:replaceState|pushState)", "history is unavailable: keep view state in location.hash"),
    ("no-hardcoded-db", r"db\.(?:read|write|createRow|onChange)\(\s*['\"]",
     "a database id is hard-coded: use artifact.app.database or find it by name via db.list()"),
]


def _strip_comments(src: str) -> str:
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


def check(src: str) -> dict:
    errors: List[dict] = []
    warnings: List[dict] = []
    code = _strip_comments(src)

    def add(bucket: List[dict], rule: str, message: str) -> None:
        bucket.append({"rule": rule, "message": message})

    for rule, pattern, message in BLOCKED:
        if re.search(pattern, code):
            add(errors, rule, message)
    if len(src.encode("utf-8")) > MAX_BODY_BYTES:
        add(warnings, "body-size", f"over {MAX_BODY_BYTES} bytes: create, then pages.append the rest")
    if not re.search(r"<meta[^>]+name=[\"']viewport[\"'][^>]*width=device-width", code, re.I):
        add(errors, "viewport", "no <meta name=viewport content=width=device-width>: phones render it zoomed out")
    if not re.search(r"prefers-color-scheme:\s*dark|color-scheme:\s*[^;]*dark", code):
        add(errors, "dark-mode", "no dark-mode styles (color-scheme or prefers-color-scheme: dark)")
    uses_runtime = re.search(r"\.me\b|\bstate\.(?:get|set|keys|all|on)\b|personId\s*\(|members\s*\(", code)
    writes = re.search(r"\.(?:write|createRow)\s*\(", code) is not None
    if uses_runtime and not re.search(r"artifact\s*\.\s*ready|\.ready\b", code):
        add(errors, "await-ready", "artifact.ready is never awaited before reading me or state")
    if writes and not re.search(r"waiting for the viewer", code, re.I):
        add(errors, "consent-retry", "writes never retry on 'waiting for the viewer': the first click is lost")
    if re.search(r"\.(?:read|write|createRow)\s*\(", code) and "onChange" not in code:
        add(warnings, "live-updates", "reads without db.onChange: other viewers' edits will not appear")
    if re.search(r"\b(?:local|session)Storage\b", code):
        add(warnings, "storage", "localStorage throws in the sandbox: wrap it in try or use artifact.state")
    if re.search(r"<button\b", code) and not re.search(r"(?:min-)?(?:height|block-size)\s*:\s*(?:4[4-9]|[5-9]\d)px", code):
        add(warnings, "touch-target", "no 44px+ control height found: touch targets must be at least 44px")
    for m in re.finditer(r":hover[^{]*\{([^}]*)\}", code):
        if re.search(r"\b(?:display|visibility|opacity)\s*:", m.group(1)):
            add(warnings, "hover-only", "a :hover rule changes display/visibility/opacity: controls must not be hover-only")
            break
    if re.search(r">\s*(?:Send|Notify|Email|DM|Message)\b[^<]{0,40}</button>", code, re.I):
        add(warnings, "no-fake-send", "a button claims to send or notify: a page cannot message anyone")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def main(argv: List[str]) -> int:
    args = [a for a in argv if a != "--json"]
    if len(args) != 1:
        print("usage: check_page.py page.html [--json]", file=sys.stderr)
        return 2
    try:
        with open(args[0], encoding="utf-8") as fh:
            src = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        print(f"check_page: cannot read {args[0]}: {exc}", file=sys.stderr)
        return 2
    result = check(src)
    if "--json" in argv:
        print(json.dumps(result, indent=2))
    else:
        for item in result["errors"]:
            print(f"error   {item['rule']}: {item['message']}")
        for item in result["warnings"]:
            print(f"warning {item['rule']}: {item['message']}")
        print("ok" if result["ok"] else "fix the errors above")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
