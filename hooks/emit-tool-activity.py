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


def _workspace() -> Path:
    repo = next((p for p in Path(__file__).resolve().parents
                 if (p / "src" / "workspace_default.py").is_file()), None)
    if repo is not None:
        try:
            sys.path.insert(0, str(repo / "src"))
            from workspace_default import resolve_workspace  # noqa: E402
            return Path(resolve_workspace())
        except Exception:
            return repo / "workspace"
    return Path.home() / "workspace"


def _target(tool: str, ti: dict) -> str:
    # A short, non-sensitive locator per tool — never the full command/content.
    try:
        if tool == "Bash":
            # Verb only (+ safe subcommand for verb-based tools) — never args,
            # which can carry secrets (export FOO=…, curl -H "Authorization …").
            cmd = str(ti.get("command", ""))
            # Reduce to the first REAL command so the verb reflects the work, not
            # a `cd` prefix — handles both `cd <dir> && real` and newline-multiline
            # `cd <dir>\nreal` (our common shapes).
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
    # The actual CONTENT of the tool call — the diff / command / text — for the
    # /full level only. This carries secrets (that's the point of /full being
    # opt-in above /verbose); the terse `step` above stays secret-safe.
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


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool = data.get("tool_name") or "tool"
    ti = data.get("tool_input") or {}
    target = _target(tool, ti)
    label = f"{tool}: {target}" if target else tool
    rec = {"ts": int(time.time()), "kind": "tool", "step": label}
    detail = _detail(tool, ti)
    if detail:
        rec["detail"] = detail          # /full-only; consumers gate on the level
    try:
        ws = _workspace()
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
