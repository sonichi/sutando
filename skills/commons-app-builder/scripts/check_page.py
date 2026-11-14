#!/usr/bin/env python3
"""Static pre-flight for a Commons app page against GUIDE.md's hard rules.

Catches what a read of the source can prove (sandbox-blocked APIs, missing viewport or dark
mode, no consent retry, a hard-coded database id, a request to the viewer's agent with no
stable key). It is no substitute for rendering the page.

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


# What makes a value new on every call. A key built from one of these never repeats.
_UNSTABLE = r"Date\s*\.\s*now|Math\s*\.\s*random|randomUUID|getRandomValues|performance\s*\.\s*now|new\s+Date\b"
_AGENT_CALL = re.compile(
    r"(?:\.\s*(?:sendTask|sendToMyAgent)|\[\s*['\"](?:sendTask|sendToMyAgent)['\"]\s*\]"
    r"|(?<![\w.$])sendToMyAgent)\s*\(")
_PAIRS = {"(": ")", "[": "]", "{": "}"}


def _balanced(code: str, start: int) -> str:
    """The text between the bracket at `start` and its match, strings and templates skipped;
    the rest of the source when it never closes."""
    close, depth, i, quote = _PAIRS[code[start]], 0, start, ""
    while i < len(code):
        ch = code[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch == code[start]:
            depth += 1
        elif ch == close:
            depth -= 1
            if depth == 0:
                return code[start + 1:i]
        i += 1
    return code[start + 1:]


def _top_level_parts(body: str) -> List[str]:
    """An object literal's properties: its text split at the commas that are not nested."""
    parts, depth, last, quote, i = [], 0, 0, "", 0
    while i < len(body):
        ch = body[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch in _PAIRS:
            depth += 1
        elif ch in _PAIRS.values():
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(body[last:i])
            last = i + 1
        i += 1
    parts.append(body[last:])
    return [p.strip() for p in parts if p.strip()]


def _definition(code: str, name: str) -> str:
    """The text a local `name` is defined by (a const/let/var or a function), or ''."""
    esc = re.escape(name)
    m = re.search(r"\bfunction\s+" + esc + r"\s*\([^)]*\)\s*\{", code)
    if m:
        return _balanced(code, m.end() - 1)
    m = re.search(r"\b(?:const|let|var)\s+" + esc + r"\s*=", code)
    if not m:
        return ""
    rest = code[m.end():]
    brace = re.match(r"[^;{]*\{", rest)
    if brace:
        return rest[:brace.end()] + _balanced(rest, brace.end() - 1)
    return rest.split(";", 1)[0]


def _agent_task_keys(code: str) -> List[Tuple[str, str]]:
    """One (finding, detail) per sendTask call whose argument is an object literal, judged on
    THAT literal's own `key`: 'missing', 'unstable' (the clock or a random number is in the key's
    own expression), 'unproven' (a local it names reads one) or 'ok'. A call given anything else
    (a variable, a spread) cannot be judged from the source and is left alone."""
    found: List[Tuple[str, str]] = []
    for call in _AGENT_CALL.finditer(code):
        arg = _balanced(code, call.end() - 1).strip()
        if not arg.startswith("{"):
            continue
        parts = _top_level_parts(_balanced(arg, 0))
        if any(p.startswith("...") for p in parts):
            continue
        expr = None
        for part in parts:
            m = re.match(r"(?:key|['\"]key['\"])\s*:\s*(.+)$", part, re.S)
            if m:
                expr = m.group(1)
            elif part == "key":
                expr = "key"
        if expr is None:
            found.append(("missing", ""))
        elif re.search(_UNSTABLE, expr):
            found.append(("unstable", ""))
        else:
            names = [n for n in re.findall(r"(?<![\w.$])([A-Za-z_$][\w$]*)", expr)
                     if re.search(_UNSTABLE, _definition(code, n))]
            found.append(("unproven", names[0]) if names else ("ok", ""))
    return found


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
    # GUIDE "A button that asks the viewer's own agent": the two calls that do reach someone.
    asks_agent = _AGENT_CALL.search(code) is not None
    posts = re.search(r"\broom\s*\.\s*post\s*\(", code) is not None
    if asks_agent:
        keys = _agent_task_keys(code)
        # The host refuses a request with no key, so the button would never work.
        if any(kind == "missing" for kind, _ in keys):
            add(errors, "agent-task-key", "sendTask without a key: the host refuses it. Pass a stable key per action")
        # A key made new on every call defeats the point of it: a double tap asks twice.
        if any(kind == "unstable" for kind, _ in keys):
            add(errors, "agent-task-key-unstable",
                "the sendTask key changes on every call (time or random): a double tap or a reload "
                "asks twice. Derive it from the row or action")
        # Reading the source cannot prove what a helper returns: say what to look at.
        for helper in sorted({name for kind, name in keys if kind == "unproven"}):
            add(warnings, "agent-task-key-unproven",
                f"the sendTask key comes from `{helper}`, which reads the clock or a random number: "
                "check that the key itself does not change from one tap to the next")
        if not re.search(r"\.reason\b", code):
            add(warnings, "agent-task-refusal",
                "sendTask's refusal reason is never read: say why nothing was sent")
    if posts and not re.search(r"\.reason\b|\.posted\b", code):
        add(warnings, "room-post-refusal", "room.post's result is never read: say whether it was posted")
    if (not asks_agent and not posts
            and re.search(r">\s*(?:Send|Notify|Email|DM|Message)\b[^<]{0,40}</button>", code, re.I)):
        add(warnings, "no-fake-send",
            "a button claims to send or notify, and the page calls neither agent.sendTask nor room.post")
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
