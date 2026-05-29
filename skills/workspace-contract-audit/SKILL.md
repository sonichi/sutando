# Workspace Contract Audit

Scan the Sutando codebase for path-access points that should be migrated to the V1 workspace contract (Code / Workspace / Memory three-space model). Categorizes each by access type × destination space, cross-refs against shipped migration PRs to identify NOT-YET-MIGRATED sites, and emits a structured report with code links + reasoning.

**Usage**: `/workspace-contract-audit`

## What it does

Walks `src/`, `skills/`, `scripts/`, `tests/`, `src/Sutando/` and grep-scans for path access points that touch:

- **Code space** — `~/Documents/sutando/sutando/` (the repo source)
- **Workspace space** — `~/.sutando/workspace/` (runtime state: tasks/, results/, state/, logs/, build_log.md, pending-questions.md, audit/)
- **Memory space** — `~/.claude/projects/<key>/memory/` (cross-machine synced user content)

For each match it tags:

**Access type:**
- `writer` — `writeFileSync(...)`, `Path(...).write_text(...)`, `mkdir_p`, `>` redirects in shell, etc.
- `reader` — `readFileSync(...)`, `Path(...).read_text()`, `open(path)`, `cat $path`, `<` reads, etc.
- `path-derive` — indirect construction like `os.path.join(REPO_DIR, "notes")`, `${SUTANDO_WORKSPACE}/state/...`, `Path(__file__).parent.parent`
- `process-pattern` — `pgrep -f "Documents/sutando"`, `pkill -f "voice-agent.ts"` (patterns that grep the ps tree)
- `manifest-text` — SKILL.md / CLAUDE.md prompt text that names a path the runtime interprets
- `config-env` — hardcoded path values in .env / launchd / crontab / Dockerfile / shell aliases

**Destination space:**
- `Code` — source-tree paths (`src/`, `skills/`, `tests/`, `scripts/`, `*.ts`, `*.py`, `*.swift`)
- `Workspace` — runtime state (`state/`, `tasks/`, `results/`, `logs/`, `build_log.md`, `pending-questions.md`, `audit/`)
- `Memory` — memory paths (`memory/MEMORY.md`, individual `*.md` memory files, `.claude/projects/<key>/memory/`)
- `Ambiguous` — legitimately could go either way (e.g., migration-bridge code probing both old and new defaults — should NOT be flagged for change)

Cross-references each site against shipped audit PRs (`#1330-#1334`) and tags whether it was already migrated.

## Output

Writes a dated markdown report to `<workspace>/audit/workspace-contract-audit-YYYY-MM-DD.md`. Each finding entry:

```
### file:line — <short title>
**Access:** writer | **Destination:** Workspace | **Covered by:** ❌ (NOT migrated)
**Snippet:**
```code
<original line>
```
**Reasoning:** <why this is or isn't a violation; what should change; recommended fix path>
```

Also writes a `<workspace>/audit/baseline.json` for diff-against-yesterday detection.

## Triggers

- **Manual**: `python3 ~/.claude/skills/workspace-contract-audit/scripts/run-audit.py`
- **Nightly cron** (1 AM local): `0 1 * * * python3 .../run-audit.py --notify-on-new`. With `--notify-on-new` it diffs against `baseline.json` and DMs Susan if any NEW sites appeared (vs the previous baseline) — caught early before they accumulate.
- **PreCommit hook** (opt-in): block commits that introduce NEW unmigrated sites. Off by default.

## When NOT to invoke

- Mid-migration churn (sites changing by the minute — let the dust settle first).
- When V1 contract isn't finalized yet — re-running before "destination spaces" stabilize produces report churn.
- During active feature work that touches paths (run AFTER the feature lands, not during it).

## Companions

This skill is intended to be one of a family:
- `workspace-contract-audit` (this skill) — path-access points
- `unused-skills-audit` — skills/ entries not registered in inline-tools loader
- `unmerged-fork-skills-audit` — sutando-skills repo vs local install
- `stale-process-audit` — health-check stale-process patterns systematically
- `deprecated-env-var-audit` — old env names (REPO/REPO_DIR/etc) still referenced

All five share the same shape: walk → categorize → baseline-diff → notify on NEW. See `~/.claude/skills/workspace-contract-audit/scripts/run-audit.py` as the reference impl.

## Related

- V1 workspace contract design: `docs/workspace-contract.md` + `docs/workspace-design.md`
- Migration PR batch: `#1330-#1334`
- Subagent-fed manual audit that motivated this skill: `/tmp/workspace-audit-systematic.md` (2026-05-28, 230 sites found, 8 actionable not-yet-migrated)
