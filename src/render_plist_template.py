#!/usr/bin/env python3
"""Render a launchd plist: literal __TOKEN__ substitution, XML escaping, parse
check. Usage: render_plist_template.py TEMPLATE DEST TOKEN=VALUE ..."""

from __future__ import annotations

import os
import plistlib
import re
import sys
import tempfile
from xml.sax.saxutils import escape


class RenderError(Exception):
    """Rendering failed; the destination is left untouched."""


def render(template_text: str, values: dict) -> str:
    """Literal __TOKEN__ substitution; values XML-escaped for a <string> node."""
    out = template_text
    for token, value in values.items():
        if not token:
            raise RenderError("empty token name")
        out = out.replace(f"__{token}__", escape(str(value)))
    return out


def unresolved_tokens(text: str) -> list:
    """Placeholders left after substitution: these still parse, so the parse
    check alone cannot catch a forgotten value."""
    return sorted(set(re.findall(r"__([A-Z0-9_]+)__", text)))


def render_to_file(template_path: str, dest_path: str, values: dict) -> str:
    """Render to *dest_path*, validating first: a failed render leaves any
    existing destination unchanged rather than publishing a broken plist."""
    with open(template_path, "r", encoding="utf-8") as fh:
        text = render(fh.read(), values)

    leftover = unresolved_tokens(text)
    if leftover:
        raise RenderError(
            f"{template_path}: no value supplied for: {', '.join(leftover)}"
        )

    # launchd accepts "--" inside an XML comment and a shipped template relies
    # on that; escape() already stops a value terminating a comment.
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    try:
        plistlib.loads(body.encode("utf-8"))
    except Exception as exc:
        raise RenderError(f"{template_path}: rendered output is not a valid plist: {exc}")

    dest_dir = os.path.dirname(os.path.abspath(dest_path)) or "."
    os.makedirs(dest_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest_dir, prefix=".plist-render-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return text


def main(argv: list) -> int:
    if len(argv) < 3:
        print(__doc__.strip().split("Usage:")[-1].strip(), file=sys.stderr)
        return 2
    template_path, dest_path = argv[1], argv[2]
    values = {}
    for pair in argv[3:]:
        if "=" not in pair:
            print(f"render-plist: not TOKEN=VALUE: {pair!r}", file=sys.stderr)
            return 2
        token, value = pair.split("=", 1)
        values[token] = value
    try:
        render_to_file(template_path, dest_path, values)
    except (RenderError, OSError) as exc:
        print(f"render-plist: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv))
