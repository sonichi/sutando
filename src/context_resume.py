#!/usr/bin/env python3
"""Extract recent conversation turns from a Claude Code transcript (.jsonl).

The minimal ContextProvider (owner directive 2026-06-11): session-handoff.sh
already receives the transcript path from the PreCompact hook but never used
it — conversation content died on every compaction while only system status
survived into session-state.md. This module closes that gap in place: same
session-state.md file, same readers (catchup-after-startup, CLAUDE.md session
start), one new "Recent Conversation" section.

Deliberately NOT here (wait for a second real use case before abstracting):
provider registries, caches, adaptive multi-mode formatting, cross-session
merge. One source (a transcript file), one output (cleaned markdown).

Usage:
  python3 src/context_resume.py <transcript.jsonl> [--turns N] [--chars M]
  python3 src/context_resume.py --latest [--turns N] [--chars M]

--latest finds the newest .jsonl under the Claude project dir for this repo
(resolved via scripts/sutando-config.sh claude-home-path projects/<slug>).
Exit 0 with output on success; exit 1 with a one-line stderr diagnostic
otherwise (callers append `|| echo "(unavailable)"`).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from util_paths import claude_project_slug  # noqa: E402

# Harness-injected noise that must not survive into the resumed context.
_NOISE_BLOCK_RE = re.compile(
    r"<system-reminder>.*?</system-reminder>"
    r"|<local-command-caveat>.*?</local-command-caveat>"
    r"|<command-name>.*?</command-name>"
    r"|<command-message>.*?</command-message>"
    r"|<command-args>.*?</command-args>"
    r"|<local-command-stdout>.*?</local-command-stdout>"
    r"|<task-notification>.*?</task-notification>",
    re.DOTALL,
)
_NOISE_LINE_RE = re.compile(
    r"^\s*(Caveat: The messages below|\[SYSTEM NOTIFICATION\b|\[watcher-ping\])",
)
_PER_MESSAGE_CHAR_CAP = 1500  # one runaway message must not eat the budget


def _clean(text: str) -> str:
    text = _NOISE_BLOCK_RE.sub("", text)
    lines = [ln for ln in text.splitlines() if not _NOISE_LINE_RE.match(ln)]
    out = "\n".join(lines).strip()
    if len(out) > _PER_MESSAGE_CHAR_CAP:
        out = out[:_PER_MESSAGE_CHAR_CAP] + " […truncated]"
    return out


def _message_text(message: dict) -> tuple[str, list[str]]:
    """Return (joined text, tool names) from a transcript message's content."""
    content = message.get("content")
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    tools: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            texts.append(block.get("text", ""))
        elif btype == "tool_use":
            tools.append(block.get("name", "?"))
        # thinking / tool_result / images: skipped — noise for resume purposes
    return "\n".join(texts), tools


def extract_recent_turns(transcript: Path, max_turns: int = 12, max_chars: int = 6000) -> str:
    """Last `max_turns` user/assistant turns as markdown, newest last, ≤ max_chars."""
    turns: list[tuple[str, str]] = []  # (role, text)
    with transcript.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("type") not in ("user", "assistant"):
                continue
            message = entry.get("message") or {}
            text, tools = _message_text(message)
            text = _clean(text)
            if not text and tools:
                text = f"[ran tools: {', '.join(dict.fromkeys(tools))}]"
            if not text:
                continue
            role = "User" if entry["type"] == "user" else "Assistant"
            # Tool-result echoes come back as user-type entries; their cleaned
            # text is usually empty (list content with tool_result blocks only),
            # so anything that survived _clean() is a real human message.
            turns.append((role, text))

    turns = turns[-max_turns:]
    rendered: list[str] = []
    used = 0
    for role, text in reversed(turns):  # budget from newest backwards
        piece = f"**{role}:** {text}"
        if used + len(piece) > max_chars and rendered:
            break
        rendered.append(piece)
        used += len(piece)
    return "\n\n".join(reversed(rendered))


def _latest_transcript() -> Path:
    # Slug caveat (john, #1909): the slug below derives from this file's real
    # path, while Claude Code slugifies the session's *logical* cwd — the two
    # diverge for symlinked checkouts (/tmp vs /private/tmp on macOS) and for
    # sessions anchored outside the repo (e.g. a cwd-anchor dir). --latest is
    # a best-effort fallback for standard checkouts; the exact-path arg (from
    # hook stdin JSON) is always authoritative when present.
    repo = Path(__file__).parent.parent
    proj = subprocess.run(
        ["bash", str(repo / "scripts" / "sutando-config.sh"), "claude-home-path", "projects"],
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    slug = claude_project_slug(repo)
    candidates = sorted(
        Path(proj, slug).glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ) if proj and Path(proj, slug).is_dir() else []
    if not candidates:
        raise FileNotFoundError(f"no transcript .jsonl under {proj}/{slug}")
    return candidates[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("transcript", nargs="?", help="path to a Claude Code .jsonl transcript")
    ap.add_argument("--latest", action="store_true", help="use the newest transcript for this repo")
    ap.add_argument("--turns", type=int, default=12)
    ap.add_argument("--chars", type=int, default=6000)
    args = ap.parse_args()

    try:
        path = _latest_transcript() if args.latest else Path(args.transcript or "")
        if not path.is_file():
            raise FileNotFoundError(f"not a file: {path}")
        out = extract_recent_turns(path, args.turns, args.chars)
    except Exception as e:  # noqa: BLE001 — single CLI boundary, fail one-line loud
        print(f"context-resume: {e}", file=sys.stderr)
        return 1
    if not out:
        print("context-resume: no conversation turns found", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
