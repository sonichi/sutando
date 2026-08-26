#!/usr/bin/env python3
"""PostToolUse hook → append a TERSE per-tool line to the activity feed.

The Sutando Server chat's `--activity`/`/verbose` mode renders these so the
owner sees the core's real tool-by-tool work live. Deliberately terse (tool +
short target, never full input) so secrets/args stay out of the feed — the full
firehose stays behind `--raw`. Fail-open in every branch: a hook must never
block or error a tool call.
"""
import json
import sys
import time
from pathlib import Path


def _workspace() -> "Path | None":
    # Canonical resolver only: on any failure return None and the caller
    # skips the write — never invent a fallback tree for mutable state
    repo = next((p for p in Path(__file__).resolve().parents
                 if (p / "src" / "workspace_default.py").is_file()), None)
    if repo is None:
        return None
    try:
        sys.path.insert(0, str(repo / "src"))
        from workspace_default import resolve_workspace  # noqa: E402
        return Path(resolve_workspace())
    except Exception:
        return None


def _target(tool: str, ti: dict) -> str:
    # A short, non-sensitive locator per tool — never the full command/content.
    try:
        if tool == "Bash":
            # Verb only (+ safe subcommand for verb-based tools) — never args,
            # which can carry secrets (export FOO=…, curl -H "Authorization …").
            cmd = str(ti.get("command", ""))
            # Reduce to the first real command so the verb reflects the work,
            # not a `cd` prefix (handles && chains and multiline forms).
            real = ""
            for line in cmd.splitlines():
                line = line.strip()
                if line.startswith("cd ") and "&&" in line:
                    real = line.split("&&", 1)[1].strip()
                    break
                if not line or line == "cd" or line.startswith("cd "):
                    continue
                real = line
                break
            parts = (real or cmd).split()
            # Skip leading VAR=value / VAR=$(…) assignment tokens — the verb is
            # the first non-assignment word (fixes steps like "WS=$(bash").
            while parts and "=" in parts[0] and not parts[0].startswith("="):
                head = parts.pop(0)
                if "$(" in head:        # VAR=$(cmd …: the verb is inside
                    inner = head.split("$(", 1)[1]
                    if inner:
                        parts.insert(0, inner)
                    break
            if not parts:
                return ""
            verb = parts[0]
            subv = {"git", "gh", "npm", "pip", "pip3", "docker", "kubectl",
                    "python3", "python", "cargo", "yarn", "brew", "tmux", "bash"}
            if verb in subv and len(parts) > 1 and "=" not in parts[1]:
                return f"{verb} {parts[1]}"[:40]
            return verb[:40]
        if tool in ("Read", "Edit", "Write", "NotebookEdit"):
            return Path(str(ti.get("file_path", ""))).name
        if tool in ("Grep", "Glob"):
            return str(ti.get("pattern", ""))[:48]
        if tool in ("Task", "Agent"):
            return str(ti.get("description", ""))[:48]
        if tool == "WebFetch":
            return str(ti.get("url", ""))[:48]
    except Exception:
        pass
    return ""


def _detail(tool: str, ti: dict) -> str:
    # Full call content, /full level only — carries secrets by design;
    # the terse step above stays secret-safe.
    try:
        if tool == "Bash":
            return str(ti.get("command", ""))[:400]
        if tool == "Edit":
            old = str(ti.get("old_string", ""))[:180]
            new = str(ti.get("new_string", ""))[:180]
            return f"- {old}\n+ {new}"
        if tool in ("Write", "NotebookEdit"):
            return str(ti.get("content", ti.get("new_source", "")))[:360]
        if tool == "MultiEdit":
            n = len(ti.get("edits", []) or [])
            return f"{n} edit(s)"
        if tool in ("Grep", "Glob"):
            return str(ti.get("pattern", ""))[:200]
        if tool == "WebFetch":
            return str(ti.get("url", ""))[:200]
    except Exception:
        pass
    return ""


_VERB_PHRASES = {
    "gh": "working with GitHub", "git": "working with git",
    "curl": "fetching from the web", "npm": "running npm",
    "pip": "installing packages", "pip3": "installing packages",
    "python3": "running a script", "python": "running a script",
    "node": "running a script", "npx": "running a tool",
    "bash": "running a script", "sh": "running a script",
    "pio": "building firmware", "make": "building",
    "ls": "listing files", "grep": "searching", "find": "searching files",
}


def _humanize(tool: str, target: str) -> str:
    """Owner-facing phrasing for the feed (voice panels render this line).
    Falls back to the terse `Tool: target` for anything unmapped — honest,
    never invented."""
    try:
        if tool == "Bash" and target:
            verb = target.split()[0]
            phrase = _VERB_PHRASES.get(verb)
            if phrase:
                # keep the safe subcommand for context: "working with GitHub (gh pr)"
                return f"{phrase} ({target})" if " " in target else phrase
        if tool == "Read" and target:
            return f"reading {target}"
        if tool in ("Edit", "Write", "NotebookEdit") and target:
            return f"editing {target}"
        if tool in ("Grep", "Glob") and target:
            return f"searching for {target}"
        if tool in ("Task", "Agent") and target:
            return f"delegating: {target}"
        if tool == "WebFetch" and target:
            return f"reading {target}"
        if tool == "WebSearch":
            return "searching the web"
    except Exception:
        pass
    return f"{tool}: {target}" if target else tool


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool = data.get("tool_name") or "tool"
    ti = data.get("tool_input") or {}
    target = _target(tool, ti)
    label = _humanize(tool, target)
    rec = {"ts": int(time.time()), "kind": "tool", "step": label}
    detail = _detail(tool, ti)
    if detail:
        rec["detail"] = detail          # /full-only; consumers gate on the level
    try:
        ws = _workspace()
        if ws is None:
            sys.exit(0)  # fail open: no canonical workspace, no write
        feed = ws / "state" / "activity-feed.jsonl"
        feed.parent.mkdir(parents=True, exist_ok=True)
        # Bound the file so an always-on feed can't grow without limit.
        if feed.exists() and feed.stat().st_size > 512_000:
            tail = feed.read_text(errors="replace").splitlines()[-500:]
            feed.write_text("\n".join(tail) + "\n")
        with feed.open("a") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
