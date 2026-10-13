#!/usr/bin/env python3
"""Stop hook: owner questions left only in the core's terminal text are routed to ask-owner.

The core's terminal reaches no one, so a turn whose final text lists questions for the
owner, in a turn that filed none through `scripts/ask-owner.py`, is refused once with the
instruction to file them. Core sessions only (SUTANDO_CORE_SESSION=1, not a pool worker);
guests and workers pass untouched.

Detection is a deliberately narrow textual heuristic, since the final text carries no
structural marker: at least MIN_QUESTIONS markdown list items ending in "?", outside code
fences and blockquotes. A misfire costs one continuation; imperative items ("Approve X")
are not counted, so a list of only those passes.

"Filed this turn" is proven from tool identity plus result, never command text alone: a
Bash tool_use in this turn that invoked ask-owner.py (or `pq.py ask`) and whose
tool_result carries the ask report's `sent: queued` or a non-FAILED `recorded:` line.

One refusal per turn: `stop_hook_active` passes, and a spent marker keyed by the turn's
promptId (state/pending-questions-terminal-guard.json) passes a later Stop of that turn.
Every error fails open: a hook that raises would block the turn it guards.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

MIN_QUESTIONS = 2
TAIL_BYTES = 8 * 1024 * 1024
MARKER = "pending-questions-terminal-guard.json"
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(\S.*)$")
_ASK_SCRIPT = re.compile(r"(?:^|[\s/'\"])(?:ask-owner\.py|pq\.py\s+ask)\b")


def is_core_session(env=os.environ) -> bool:
    return env.get("SUTANDO_CORE_SESSION") == "1" and not env.get("SUTANDO_INSTANCE_ID")


def owner_questions(text: str) -> List[str]:
    """List items ending in '?' — outside fenced code and blockquotes."""
    out, fenced = [], False
    for line in (text or "").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _LIST_ITEM.match(line)
        if m and m.group(1).rstrip(" *_`").endswith("?"):
            out.append(m.group(1).strip())
    return out


def _tail_entries(path: str) -> List[dict]:
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - TAIL_BYTES))
        data = fh.read()
    lines = data.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]  # the seek lands mid-line
    out = []
    for raw in lines:
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _content(e: dict) -> list:
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [x for x in c if isinstance(x, dict)] if isinstance(c, list) else []


def _is_prompt(e: dict) -> bool:
    return e.get("type") == "user" and not any(x.get("type") == "tool_result" for x in _content(e))


def current_turn(entries: List[dict]) -> Tuple[Optional[str], List[dict]]:
    """(turn id, this turn's entries): from the first entry carrying the last prompt's id."""
    start, turn = None, None
    for i in range(len(entries) - 1, -1, -1):
        if _is_prompt(entries[i]):
            start, turn = i, entries[i].get("promptId") or entries[i].get("uuid")
            break
    if start is None:
        return None, entries
    if entries[start].get("promptId"):
        while start > 0 and entries[start - 1].get("promptId") == turn:
            start -= 1
    return turn, entries[start:]


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c if isinstance(c, str) else ""


def filed_this_turn(turn: Iterable[dict]) -> bool:
    asks = set()
    for e in turn:
        for b in _content(e):
            if b.get("type") == "tool_use" and b.get("name") == "Bash":
                if _ASK_SCRIPT.search(str((b.get("input") or {}).get("command", ""))):
                    asks.add(b.get("id"))
            elif b.get("type") == "tool_result" and b.get("tool_use_id") in asks:
                for line in _result_text(b).splitlines():
                    line = line.strip()
                    if line.startswith("sent: queued") or (
                            line.startswith("recorded: ") and not line.startswith("recorded: FAILED")):
                        return True
    return False


def _last_text(turn: List[dict]) -> str:
    for e in reversed(turn):
        if e.get("type") == "assistant":
            texts = [b.get("text", "") for b in _content(e) if b.get("type") == "text"]
            if texts:
                return "\n".join(texts)
    return ""


def _workspace() -> Path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    from workspace_default import resolve_workspace  # noqa: PLC0415
    return Path(resolve_workspace(migrate=False))


def _spent(ws: Path, key: str) -> bool:
    try:
        return json.loads((ws / "state" / MARKER).read_text()).get("turn") == key
    except (OSError, ValueError, AttributeError):
        return False


def _spend(ws: Path, key: str) -> bool:
    try:
        d = ws / "state"
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=f".{MARKER}.")
        with os.fdopen(fd, "w") as fh:
            json.dump({"turn": key}, fh)
        os.replace(tmp, d / MARKER)
        return True
    except OSError:
        return False


def decide(payload: dict, ws: Path, env=os.environ) -> Optional[str]:
    """The block reason, or None to let the turn end. Writes the spent marker on a block."""
    if not is_core_session(env) or payload.get("stop_hook_active") is True:
        return None
    path = payload.get("transcript_path")
    entries = _tail_entries(path) if isinstance(path, str) and path else []
    turn_id, turn = current_turn(entries)
    text = payload.get("last_assistant_message")
    if not isinstance(text, str):
        text = _last_text(turn)
    questions = owner_questions(text)
    if len(questions) < MIN_QUESTIONS or filed_this_turn(turn):
        return None
    key = f"{payload.get('session_id') or env.get('CLAUDE_CODE_SESSION_ID', '')}:" + (
        turn_id or hashlib.sha256(text.encode()).hexdigest())
    if _spent(ws, key) or not _spend(ws, key):
        return None
    return (f"Your final text lists {len(questions)} questions for the owner "
            f"(first: \"{questions[0][:120]}\"), and none was filed this turn. This terminal "
            "reaches no one. File each open one with `python3 scripts/ask-owner.py \"<question>\" "
            "[--context \"<why / options>\"] [--task-file <task>]` (a row in his Pending questions "
            "database, sent to him once); skip any already filed or answered. Then end with a "
            "one-line status, not the list. If these are not questions for the owner, end the "
            "turn as is. This is the only reminder this turn.")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict) or not is_core_session():
            return 0
        reason = decide(payload, _workspace())
        if reason:
            sys.stdout.write(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))
    except Exception:  # noqa: BLE001 — a hook that raises blocks the turn it guards
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
