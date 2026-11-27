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


_ROOM_POST = re.compile(r"\broom\s*\.\s*post\s*\(")
_GESTURES = r"(?:click|submit|keydown|keyup|pointerup|touchend)"
_NAME = r"[A-Za-z_$][\w$]*"
_FN_HEAD = re.compile(
    r"\bfunction\s+(" + _NAME + r")\s*\("
    r"|\b(?:const|let|var)\s+(" + _NAME + r")\s*=\s*(?=(?:async\b\s*)?"
    r"(?:function\b|\([^()]*\)\s*=>|" + _NAME + r"\s*=>))")
# Where code runs with nobody having pressed anything: a timer, a data watch, a non-gesture event.
_UNPROMPTED = re.compile(
    r"\b(?:setTimeout|setInterval|requestAnimationFrame|queueMicrotask)\s*\("
    r"|\.\s*(?:onChange|on)\s*\("
    r"|addEventListener\s*\(\s*['\"](?!" + _GESTURES + r"['\"])[\w-]+['\"]")


def _expression_end(code: str, start: int) -> int:
    """Where the expression starting at `start` ends: after the block it opens, or at its `;`."""
    depth, i, quote = 0, start, ""
    while i < len(code):
        ch = code[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch == "{" and depth == 0:
            return i + 2 + len(_balanced(code, i))
        elif ch in "([":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return i
        elif ch == ";" and depth == 0:
            return i
        i += 1
    return len(code)


def _call_span(code: str, match: "re.Match") -> Tuple[int, int]:
    """The span of the call a regex match opens: from the match to its closing bracket."""
    paren = code.index("(", match.start())
    return match.start(), paren + 2 + len(_balanced(code, paren))


def _sends_without_a_click(code: str) -> Tuple[int, int, int]:
    """(sends, refused, unproven) for agent.sendTask and room.post calls, read from the source.

    A send is fine when it sits in a gesture handler (a click, a submit, a key press), or in a
    named function that is only ever referred to from such handlers or from other such
    functions. It is REFUSED when it sits in a timer, a data watch or a non-gesture listener
    with no gesture handler around it, or when the page has no gesture handler at all. Anything
    else (a send at the top of the script, in a method or an unnamed callback) cannot be shown
    to need a click and is UNPROVEN. This reads text, not a parse: it errs towards asking the
    author to look."""
    code = re.sub(r"(?<![:'\"\\])//[^\n]*", lambda m: " " * len(m.group(0)), code)
    sends = [m.start() for rx in (_AGENT_CALL, _ROOM_POST) for m in rx.finditer(code)]
    if not sends:
        return 0, 0, 0
    gestures: List[Tuple[int, int]] = []
    for m in re.finditer(r"addEventListener\s*\(\s*['\"]" + _GESTURES + r"['\"]", code):
        gestures.append(_call_span(code, m))
    for m in re.finditer(r"\.\s*on" + _GESTURES + r"\s*=(?!=)", code):
        gestures.append((m.start(), _expression_end(code, m.end())))
    for m in re.finditer(r"\son" + _GESTURES + r"\s*=\s*([\"'])(.*?)\1", code, re.S):
        gestures.append((m.start(2), m.end(2)))
    if not gestures:
        return len(sends), len(sends), 0
    unprompted = [_call_span(code, m) for m in _UNPROMPTED.finditer(code)]
    functions: List[Tuple[str, int, int, int]] = []          # name, where the name is, start, end
    for m in _FN_HEAD.finditer(code):
        if m.group(1):
            paren = m.end() - 1
            brace = code.find("{", paren + 1 + len(_balanced(code, paren)))
            end = brace + 2 + len(_balanced(code, brace)) if brace >= 0 else len(code)
            functions.append((m.group(1), m.start(1), m.start(), end))
        else:
            functions.append((m.group(2), m.start(2), m.start(), _expression_end(code, m.end())))

    def within(pos: int, spans: List[Tuple[int, int]]) -> bool:
        return any(start <= pos < end for start, end in spans)

    def owner(pos: int) -> str:
        holding = [f for f in functions if f[2] <= pos < f[3]]
        return min(holding, key=lambda f: f[3] - f[2])[0] if holding else ""

    def click_only(name: str, seen: frozenset) -> bool:
        if name in seen:
            return True
        declared = {f[1] for f in functions if f[0] == name}
        for m in re.finditer(r"(?<![\w.$])" + re.escape(name) + r"(?![\w$])", code):
            if m.start() in declared or within(m.start(), gestures):
                continue
            holder = owner(m.start())
            if not holder or not click_only(holder, seen | {name}):
                return False
        return True

    refused = unproven = 0
    for pos in sends:
        if within(pos, gestures):
            continue
        holder = owner(pos)
        if holder and click_only(holder, frozenset()):
            continue
        if within(pos, unprompted):
            refused += 1
        else:
            unproven += 1
    return len(sends), refused, unproven


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
    # GUIDE "only a deliberate click sends": the host asks the viewer on every call, so a page
    # that sends from a timer, a watch or page load trains people to press yes unread.
    _, refused, unproven = _sends_without_a_click(code)
    if refused:
        add(errors, "send-not-from-click",
            "agent.sendTask or room.post runs with no click behind it (a timer, a data watch, a "
            "load or change event, or a page with no click handler): send only from the handler "
            "of a control the viewer pressed")
    elif unproven:
        add(warnings, "send-click-unproven",
            "could not show from the source that every agent.sendTask or room.post call is only "
            "reached from a click: check that none runs on load, from a timer or from a watch")
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
