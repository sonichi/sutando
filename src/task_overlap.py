"""Task overlap classifier for issue #1487.

Python port of src/task-overlap.ts (layer 1 of #1487).

Determines whether two queued tasks are safe to execute concurrently.
Used by the proactive-loop when multiple tasks arrive while one is in-flight.

Heuristics (conservative first pass):
  - Tasks with explicit serial-dependency language queue serially regardless.
  - "code" tasks always serialize — they mutate git state / filesystem.
  - CANCEL_INSTRUCTION tasks never overlap — they must interrupt immediately.
  - Two tasks in *different* non-code categories are safe to overlap (1 max).
  - When uncertain, serialize (False).
"""
from __future__ import annotations

import re
from typing import Literal

TaskCategory = Literal["cancel", "code", "research", "email", "calendar", "file", "unknown"]

_I = re.IGNORECASE

# Pre-compiled patterns for classify_task_category (order = precedence).
_PAT_CANCEL = re.compile(r"\bCANCEL_INSTRUCTION:", re.MULTILINE)
_PAT_EMAIL = re.compile(
    r"\b(gmail|inbox|draft\s+email|send\s+email|reply\s+to|unread\s+email|email\s+to)\b", _I
)
_PAT_CALENDAR = re.compile(
    r"\b(calendar|schedule\s+(a\s+)?(meeting|event|call)|add\s+(a\s+)?reminder|appointments?)\b", _I
)
_PAT_CODE = re.compile(
    r"\b(fix\s+(the\s+)?bug|pull\s+request|create\s+(a\s+)?PR|open\s+(a\s+)?PR|git\s+"
    r"|commit|refactor|implement|deploy|build\s+fail|test\s+fail|run\s+tests?)\b",
    _I,
)
_PAT_FILE = re.compile(
    r"\b(open\s+file|save\s+(the\s+)?file|move\s+(the\s+)?file|delete\s+(the\s+)?file"
    r"|create\s+(a\s+)?folder)\b",
    _I,
)
_PAT_RESEARCH = re.compile(
    r"\b(search|look\s+up|find\s+(me\s+)?|what\s+is|who\s+is|explain|summarize|research"
    r"|read\s+(the\s+)?article|check\s+(the\s+)?news|weather)\b",
    _I,
)
_PAT_SERIAL = re.compile(
    r"\b(after\s+(you\s+)?(finish|complete|do)|depends?\s+on"
    r"|once\s+\S+('s?\s+|\s+is\s+)(done|complete|finished)"
    r"|wait\s+for|when\s+you('re|\s+are)\s+(done|finished))\b",
    _I,
)


def extract_task_body(raw_content: str) -> str:
    """Return everything from the first ``task:`` header line onward.

    Task files put freeform user content after the ``task:`` header
    (established by PR #982 to prevent header-field forging). Classifiers
    should see only the actual request text, not ``source: voice`` etc.
    Returns ``raw_content`` unchanged if no ``task:`` line is found.
    """
    idx = raw_content.find("\ntask:")
    if idx == -1:
        return raw_content
    return raw_content[idx + 1:]  # include the "task:" line itself


def classify_task_category(task_body: str) -> TaskCategory:
    """Classify a task body into a broad category.

    Operates on the task body (output of extract_task_body), not the full
    raw task-file content — header fields must not skew the keyword match.
    """
    if _PAT_CANCEL.search(task_body):
        return "cancel"
    if _PAT_EMAIL.search(task_body):
        return "email"
    if _PAT_CALENDAR.search(task_body):
        return "calendar"
    if _PAT_CODE.search(task_body):
        return "code"
    if _PAT_FILE.search(task_body):
        return "file"
    if _PAT_RESEARCH.search(task_body):
        return "research"
    return "unknown"


def has_serial_dependency(task_body: str) -> bool:
    """Return True if task_body contains explicit serial-dependency language.

    Phrases like "after you finish X", "depends on", "once X is done",
    "wait for" mean the user intended strict ordering.
    """
    return bool(_PAT_SERIAL.search(task_body))


def are_safe_to_overlap(raw_content1: str, raw_content2: str) -> bool:
    """Return True if task2 can start while task1 is still in-flight.

    Both arguments should be the full raw task-file content;
    extract_task_body is applied internally.

    Rules (conservative first pass per issue #1487):
      1. Serial-dependency language in either task → serialize.
      2. Either task is "cancel" → serialize (must interrupt).
      3. Either task is "code" → serialize (shared git/filesystem state).
      4. Same category → serialize (may share external state).
      5. Different non-code, non-cancel categories → safe to overlap.
    """
    body1 = extract_task_body(raw_content1)
    body2 = extract_task_body(raw_content2)

    if has_serial_dependency(body1) or has_serial_dependency(body2):
        return False

    cat1 = classify_task_category(body1)
    cat2 = classify_task_category(body2)

    if cat1 == "cancel" or cat2 == "cancel":
        return False
    if cat1 == "code" or cat2 == "code":
        return False
    if cat1 == cat2:
        return False

    return True
