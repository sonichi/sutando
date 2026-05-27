"""
Checklist detector — parses the [checklist] marker and extracts items
from bot reply text.

Called from discord-bridge.py's poll_results() after _split_file_markers().
Returns a ChecklistSpec or None if no [checklist] marker is found.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Literal


ChecklistKind = Literal["lettered", "checklist", "yesno"]

# Pattern for lettered options: "a) ...", "A. ...", "(a) ..."
_LETTERED_RE = re.compile(r'^[a-zA-Z][).]\s+', re.MULTILINE)
# Pattern for markdown checklist items: "- [ ] ...", "* [ ] ..."
_MD_LIST_RE = re.compile(r'^[-*]\s+\[\s*[xX ]?\s*\]\s+', re.MULTILINE)
# Plain list items (fallback): "- ...", "• ..."
_PLAIN_LIST_RE = re.compile(r'^[-•*]\s+', re.MULTILINE)


@dataclass
class ChecklistItem:
    id: str             # "a", "b", "c" for lettered; "0", "1", ... for list; "yes"/"no"
    label: str          # display text
    state: str = "pending"  # pending | picked | checked | unchecked


@dataclass
class ChecklistSpec:
    kind: ChecklistKind
    preamble: str           # text before the list
    items: list[ChecklistItem] = field(default_factory=list)
    original_text: str = ""  # full text after stripping [checklist]


def parse_checklist(text: str) -> ChecklistSpec | None:
    """Return a ChecklistSpec if `text` contains [checklist], else None."""
    marker_re = re.compile(r'\[checklist\]\s*', re.IGNORECASE)
    m = marker_re.search(text)
    if not m:
        return None

    # Strip the marker
    body = text[:m.start()] + text[m.end():]
    body = body.strip()

    # Try lettered options first
    lettered_matches = list(_LETTERED_RE.finditer(body))
    if lettered_matches:
        return _parse_lettered(body, lettered_matches)

    # Try markdown checklist items
    md_matches = list(_MD_LIST_RE.finditer(body))
    if md_matches:
        return _parse_md_checklist(body, md_matches)

    # Try plain bullet list (≥3 items)
    plain_matches = list(_PLAIN_LIST_RE.finditer(body))
    if len(plain_matches) >= 3:
        return _parse_plain_list(body, plain_matches)

    # Fallback: yes/no question
    return ChecklistSpec(
        kind="yesno",
        preamble=body,
        items=[
            ChecklistItem(id="yes", label="✅ Yes"),
            ChecklistItem(id="no", label="❌ No"),
        ],
        original_text=body,
    )


def _parse_lettered(body: str, matches: list[re.Match]) -> ChecklistSpec:
    lines = body.splitlines()
    # Find which lines are lettered options
    lettered_linenos: set[int] = set()
    for m in matches:
        # Map char position to line number
        pos = m.start()
        lineno = body[:pos].count('\n')
        lettered_linenos.add(lineno)

    preamble_lines = []
    item_lines: list[tuple[str, str]] = []   # (id, text)

    for i, line in enumerate(lines):
        if i in lettered_linenos:
            # Extract id and label
            stripped = line.strip()
            id_char = stripped[0].lower()
            label = re.sub(r'^[a-zA-Z][).]\s*', '', stripped).strip()
            item_lines.append((id_char, label))
        elif not item_lines:
            preamble_lines.append(line)

    return ChecklistSpec(
        kind="lettered",
        preamble='\n'.join(preamble_lines).strip(),
        items=[ChecklistItem(id=id_, label=label) for id_, label in item_lines],
        original_text=body,
    )


def _parse_md_checklist(body: str, matches: list[re.Match]) -> ChecklistSpec:
    lines = body.splitlines()
    md_linenos: set[int] = set()
    for m in matches:
        lineno = body[:m.start()].count('\n')
        md_linenos.add(lineno)

    preamble_lines = []
    items = []

    for i, line in enumerate(lines):
        if i in md_linenos:
            # Extract checked state + label
            stripped = line.strip()
            checked = bool(re.match(r'^[-*]\s+\[[xX]\]', stripped))
            label = re.sub(r'^[-*]\s+\[\s*[xX ]?\s*\]\s*', '', stripped).strip()
            state = "checked" if checked else "pending"
            items.append(ChecklistItem(id=str(len(items)), label=label, state=state))
        elif not items:
            preamble_lines.append(line)

    return ChecklistSpec(
        kind="checklist",
        preamble='\n'.join(preamble_lines).strip(),
        items=items,
        original_text=body,
    )


def _parse_plain_list(body: str, matches: list[re.Match]) -> ChecklistSpec:
    lines = body.splitlines()
    plain_linenos: set[int] = set()
    for m in matches:
        lineno = body[:m.start()].count('\n')
        plain_linenos.add(lineno)

    preamble_lines = []
    items = []

    for i, line in enumerate(lines):
        if i in plain_linenos:
            stripped = line.strip()
            label = re.sub(r'^[-•*]\s*', '', stripped).strip()
            items.append(ChecklistItem(id=str(len(items)), label=label))
        elif not items:
            preamble_lines.append(line)

    return ChecklistSpec(
        kind="checklist",
        preamble='\n'.join(preamble_lines).strip(),
        items=items,
        original_text=body,
    )
