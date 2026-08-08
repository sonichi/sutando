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


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    tool = data.get("tool_name") or "tool"
    target = _target(tool, data.get("tool_input") or {})
    label = f"{tool}: {target}" if target else tool
    try:
        ws = _workspace()
        feed = ws / "state" / "activity-feed.jsonl"
        feed.parent.mkdir(parents=True, exist_ok=True)
        # Bound the file so an always-on feed can't grow without limit.
        if feed.exists() and feed.stat().st_size > 512_000:
            tail = feed.read_text(errors="replace").splitlines()[-500:]
            feed.write_text("\n".join(tail) + "\n")
        with feed.open("a") as fh:
            fh.write(json.dumps({"ts": int(time.time()), "kind": "tool",
                                 "step": label}) + "\n")
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
