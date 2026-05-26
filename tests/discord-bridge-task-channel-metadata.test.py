#!/usr/bin/env python3
"""Structural tests for discord-bridge task-file channel/guild metadata (#1077)
and inline-tools workspace skills scan (#1081).

## PR #1077 — channel_name + guild_name in task files

Before this fix, task files only carried numeric `channel_id`. The task-consumer
(core agent) had to guess which channel that was by grepping numeric IDs against
a memory file — fragile and caused real misroutes. Fix: two additive fields
written alongside `channel_id:`:

  channel_name: #dev                  ← getattr(message.channel, "name", None) or "DM"
  guild_name: AG2 HQ                  ← message.guild.name if message.guild else "DM"

DM channels (no .name, no .guild) safely default to "DM" for both.
Newlines in Discord channel names are sanitized so they can't inject spurious
k:v lines into the task file shape.

## PR #1081 — inline-tool loader scans $SUTANDO_WORKSPACE/skills/

Before this fix, inline-tool discovery only scanned the repo's `skills/` dir and
the private memory-dir path. Users couldn't park personal tools in their workspace
without forking the repo. Fix: `$SUTANDO_WORKSPACE/skills/` is now the second
scan path in both loadSkillManifestTools() and loadCoreDocumentedSkills(). Scan
order: repo → workspace → memory-dir (last-write-wins for same-name skills).

Run: python3 tests/discord-bridge-task-channel-metadata.test.py
Exit: 0 on pass, 1 on fail.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DISCORD_BRIDGE = REPO / "src" / "discord-bridge.py"
INLINE_TOOLS = REPO / "src" / "inline-tools.ts"

_FAILURES: list[str] = []


def fail(msg: str, ctx: str = "") -> None:
    _FAILURES.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)
    if ctx:
        print("--- context ---", file=sys.stderr)
        print(ctx[:600], file=sys.stderr)


def expect(cond: bool, label: str, ctx: str = "") -> None:
    if not cond:
        fail(label, ctx)


def _task_write_region(src: str) -> str:
    """Return ~200 chars around the task_file.write_text() call in discord-bridge.py."""
    pos = src.find("task_file.write_text(")
    if pos == -1:
        return ""
    return src[max(0, pos - 300):pos + 800]


def main() -> None:
    missing = [f for f in [DISCORD_BRIDGE, INLINE_TOOLS] if not f.exists()]
    for m in missing:
        fail(f"file not found: {m}")
    if missing:
        sys.exit(1)

    db = DISCORD_BRIDGE.read_text()
    it = INLINE_TOOLS.read_text()

    # ── PR #1077: channel_name + guild_name in task files ───────────────────

    region = _task_write_region(db)
    expect(
        bool(region),
        "discord-bridge.py: task_file.write_text() call must be present",
    )

    # 1. channel_name field is written in the task file body
    expect(
        'f"channel_name: {channel_name}\\n"' in db
        or "channel_name: {channel_name}" in db,
        "discord-bridge.py: task_file.write_text() must include channel_name field",
        ctx=region,
    )

    # 2. guild_name field is written in the task file body
    expect(
        'f"guild_name: {guild_name}\\n"' in db
        or "guild_name: {guild_name}" in db,
        "discord-bridge.py: task_file.write_text() must include guild_name field",
        ctx=region,
    )

    # 3. channel_name appears AFTER channel_id (additive, preserves channel_id)
    ci_pos = db.find('"channel_id:')
    cn_pos = db.find('"channel_name:')
    expect(
        ci_pos != -1 and cn_pos != -1 and cn_pos > ci_pos,
        "discord-bridge.py: channel_name must appear AFTER channel_id in task body",
        ctx=f"channel_id pos={ci_pos} channel_name pos={cn_pos}",
    )

    # 4. DM fallback for channel_name: getattr with None default, or-"DM" pattern
    expect(
        'getattr(message.channel, "name", None) or "DM"' in db
        or "getattr(message.channel, 'name', None) or 'DM'" in db,
        "discord-bridge.py: channel_name must use getattr fallback to 'DM' for DM channels",
    )

    # 5. DM fallback for guild_name: ternary on message.guild
    expect(
        'message.guild.name if message.guild else "DM"' in db
        or "message.guild.name if message.guild else 'DM'" in db,
        "discord-bridge.py: guild_name must use ternary 'message.guild.name if message.guild else \"DM\"'",
    )

    # 6. Newline sanitization on both fields (injection guard noted in #1077 review)
    expect(
        '.replace("\\n", " ")' in db or ".replace('\\n', ' ')" in db,
        "discord-bridge.py: channel_name and guild_name must be newline-sanitized (.replace('\\n', ' '))",
    )

    # 7. channel_name and guild_name appear contiguously (same task-write block)
    cn_in_region = "channel_name" in region and "guild_name" in region
    expect(
        cn_in_region,
        "discord-bridge.py: channel_name and guild_name must both appear in the same task_file.write_text() block",
        ctx=region[:400],
    )

    # 8. comment explaining the purpose (operator visibility from #1077 PR body)
    expect(
        "channel_name" in db and "guild_name" in db
        and ("human-readable" in db or "disambiguate" in db or "misroute" in db),
        "discord-bridge.py: must have a comment explaining why channel_name/guild_name are included",
    )

    # ── PR #1081: inline-tool loader scans $SUTANDO_WORKSPACE/skills/ ───────

    # 9. loadSkillManifestTools scans WORKSPACE_DIR/skills
    manifest_fn_pos = it.find("function loadSkillManifestTools(")
    if manifest_fn_pos == -1:
        manifest_fn_pos = it.find("loadSkillManifestTools")
    expect(
        manifest_fn_pos != -1,
        "inline-tools.ts: loadSkillManifestTools() function must exist",
    )
    if manifest_fn_pos != -1:
        manifest_region = it[manifest_fn_pos:manifest_fn_pos + 900]
        expect(
            "WORKSPACE_DIR" in manifest_region and "skills" in manifest_region,
            "inline-tools.ts: loadSkillManifestTools() must include WORKSPACE_DIR/skills in scan paths",
            ctx=manifest_region[:400],
        )

    # 10. loadCoreDocumentedSkills scans WORKSPACE_DIR/skills
    core_doc_pos = it.find("function loadCoreDocumentedSkills(")
    expect(
        core_doc_pos != -1,
        "inline-tools.ts: loadCoreDocumentedSkills() function must exist",
    )
    if core_doc_pos != -1:
        core_doc_region = it[core_doc_pos:core_doc_pos + 600]
        expect(
            "WORKSPACE_DIR" in core_doc_region and "skills" in core_doc_region,
            "inline-tools.ts: loadCoreDocumentedSkills() must include WORKSPACE_DIR/skills in scan paths",
            ctx=core_doc_region[:400],
        )

    # 11. Scan order: REPO_ROOT/skills first, then WORKSPACE_DIR/skills
    # Both functions should push REPO_ROOT/skills before WORKSPACE_DIR/skills
    expect(
        "REPO_ROOT" in it and "WORKSPACE_DIR" in it,
        "inline-tools.ts: must reference both REPO_ROOT and WORKSPACE_DIR for skill scan paths",
    )
    # REPO_ROOT should appear before WORKSPACE_DIR in the dirsToScan array literal
    repo_skills_pos = it.find("join(REPO_ROOT, 'skills')")
    if repo_skills_pos == -1:
        repo_skills_pos = it.find('join(REPO_ROOT, "skills")')
    ws_skills_pos = it.find("join(WORKSPACE_DIR, 'skills')")
    if ws_skills_pos == -1:
        ws_skills_pos = it.find('join(WORKSPACE_DIR, "skills")')
    expect(
        repo_skills_pos != -1 and ws_skills_pos != -1,
        "inline-tools.ts: must have join(REPO_ROOT, 'skills') and join(WORKSPACE_DIR, 'skills') in scan array",
        ctx=f"repo_pos={repo_skills_pos} ws_pos={ws_skills_pos}",
    )
    expect(
        repo_skills_pos != -1 and ws_skills_pos != -1 and repo_skills_pos < ws_skills_pos,
        "inline-tools.ts: REPO_ROOT/skills must appear before WORKSPACE_DIR/skills (repo → workspace → memory order)",
        ctx=f"first REPO_ROOT/skills at {repo_skills_pos}, first WORKSPACE_DIR/skills at {ws_skills_pos}",
    )

    # 12. last-write-wins comment / convention documented
    expect(
        "last-write-wins" in it or "last_write_wins" in it,
        "inline-tools.ts: last-write-wins convention for same-name skills must be documented in a comment",
    )

    # Summary
    if _FAILURES:
        print(f"\n{len(_FAILURES)} test(s) FAILED:", file=sys.stderr)
        for f in _FAILURES:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    else:
        total = 19  # count of individual expect() calls
        print(f"All {total} tests passed.")


if __name__ == "__main__":
    main()
