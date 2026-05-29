#!/usr/bin/env python3
"""Workspace contract audit — scan codebase for path-access points and classify
by access type × destination space against the V1 workspace contract.

Output: <workspace>/audit/workspace-contract-audit-YYYY-MM-DD.md

Usage:
  python3 ~/.claude/skills/workspace-contract-audit/scripts/run-audit.py
  python3 ~/.claude/skills/workspace-contract-audit/scripts/run-audit.py --notify-on-new
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ---- Workspace resolution (mirrors src/workspace_default.py) ----------------
def resolve_workspace() -> Path:
    ws = os.environ.get("SUTANDO_WORKSPACE")
    if ws:
        return Path(os.path.expanduser(ws))
    return Path.home() / ".sutando" / "workspace"

def resolve_repo() -> Path:
    rd = os.environ.get("SUTANDO_REPO_DIR")
    if rd:
        return Path(os.path.expanduser(rd))
    # Walk up from this file to find a sutando checkout
    here = Path(__file__).resolve()
    for cand in [here.parent.parent.parent.parent, Path.home() / "Documents" / "sutando" / "sutando", Path.home() / "Desktop" / "sutando"]:
        if (cand / "CLAUDE.md").exists() and (cand / "skills").is_dir():
            return cand
    raise SystemExit("ERROR: cannot resolve repo dir")

REPO = resolve_repo()
WS = resolve_workspace()

# ---- Scan config ------------------------------------------------------------
SCAN_DIRS = ["src", "skills", "scripts", "tests"]
SKIP_DIRS_RE = re.compile(r"/(node_modules|\.build|\.git|\.venv|__pycache__|\.pytest_cache)/")
SCAN_EXTS = {".ts", ".py", ".sh", ".swift", ".js", ".tsx", ".jsx"}

# ---- Match patterns ---------------------------------------------------------
# Each entry: (regex, access_type, destination_hint, fix_template)
# destination_hint: function (matched_line, full_match_obj) -> "Code" | "Workspace" | "Memory" | "Ambiguous"
# Patterns are run per-line; multi-line constructs are not caught (intentional, false-negative is acceptable per scope).

def dest_from_path_suffix(snippet: str) -> str:
    """Heuristic: look at the suffix portion of the path to decide destination."""
    lower = snippet.lower()
    if any(seg in lower for seg in ["/state/", "/tasks/", "/results/", "/logs/", "/audit/", "build_log.md", "pending-questions.md", "voice-session-context.json", "core-status.json"]):
        return "Workspace"
    if "/memory/" in lower or "memory.md" in lower or ".claude/projects" in lower:
        return "Memory"
    if "memory-sync" in lower or ".sutando-memory-sync" in lower:
        return "Ambiguous"
    if any(seg in lower for seg in ["src/", "skills/", "tests/", "scripts/", ".ts", ".py", ".swift"]):
        return "Code"
    return "Ambiguous"

# Pattern -> (access_type, brief_label)
# Tightened: only match sites that touch *sutando-domain paths* (REPO/REPO_DIR/SUTANDO_*
# env interpolations, hardcoded ~/Documents/sutando|~/.sutando|~/.claude/projects strings,
# explicit workspace-suffix joins). Generic writeFileSync(any-path) is no longer matched —
# too noisy. Aim: produce a curated list of ~200-300 decision points, not 800+ raw matches.
PATTERNS = [
    # ---- path-derive (highest signal) ----
    (re.compile(r"\$\{?REPO_?DIR\}?/"), "path-derive", "shell ${REPO_DIR}/"),
    (re.compile(r"\$\{?REPO\}?/"), "path-derive", "shell ${REPO}/"),
    (re.compile(r"\$\{?PROBE_REPO\}?/"), "path-derive", "shell ${PROBE_REPO}/"),
    (re.compile(r"\$\{?SUTANDO_REPO_DIR\}?/"), "path-derive", "shell ${SUTANDO_REPO_DIR}/"),
    (re.compile(r"\$\{?SUTANDO_WORKSPACE\}?/"), "path-derive", "shell ${SUTANDO_WORKSPACE}/ (intended)"),
    (re.compile(r"\$\{?WORKSPACE\}?/"), "path-derive", "shell ${WORKSPACE}/"),
    (re.compile(r"\$\{?WS\}?/"), "path-derive", "shell ${WS}/ (intended)"),
    (re.compile(r"\$\{?SUTANDO_MEMORY_DIR\}?/"), "path-derive", "shell ${SUTANDO_MEMORY_DIR}/ (intended)"),
    # Python/TS path-join with workspace-likely suffixes
    (re.compile(r"(os\.path\.)?join\s*\(\s*REPO[_A-Z]*\s*,\s*['\"](state|tasks|results|logs|notes|audit)"), "path-derive", "join(REPO, ws-suffix)"),
    (re.compile(r"(os\.path\.)?join\s*\(\s*WORKSPACE\s*,\s*"), "path-derive", "join(WORKSPACE, ...)"),
    (re.compile(r"REPO_DIR\s*/\s*['\"](state|tasks|results|logs|notes|audit)"), "path-derive", "Path(REPO_DIR) / 'ws-suffix'"),
    (re.compile(r"REPO\s*/\s*['\"](state|tasks|results|logs|notes|audit)"), "path-derive", "Path(REPO) / 'ws-suffix'"),
    # ---- config-env hardcodes (high signal) ----
    (re.compile(r"['\"]~\/Documents\/sutando"), "config-env", "hardcoded ~/Documents/sutando"),
    (re.compile(r"['\"]~\/Desktop\/sutando"), "config-env", "hardcoded ~/Desktop/sutando"),
    (re.compile(r"['\"]~\/\.sutando[\/'\"]"), "config-env", "hardcoded ~/.sutando"),
    (re.compile(r"\.claude\/projects\/[-A-Za-z0-9]+\/memory"), "config-env", "hardcoded .claude/projects/<key>/memory"),
    (re.compile(r"os\.path\.expanduser\s*\(\s*['\"]~\/\.sutando"), "config-env", "expanduser('~/.sutando/...')"),
    (re.compile(r"os\.path\.expanduser\s*\(\s*['\"]~\/Documents\/sutando"), "config-env", "expanduser('~/Documents/sutando')"),
    # ---- process-pattern ----
    (re.compile(r"pgrep\s+-f\s+['\"]?.*[Ss]utando"), "process-pattern", "pgrep -f sutando-path"),
    (re.compile(r"pkill\s+-f\s+['\"]?.*[Ss]utando"), "process-pattern", "pkill -f sutando-path"),
    # ---- writer/reader of well-known workspace files (catches indirect paths) ----
    (re.compile(r"(writeFileSync|write_text|read_text|readFileSync)\s*\([^)]*['\"](build_log\.md|pending-questions\.md|core-status\.json|voice-session-context\.json|context-drop\.txt)"), "writer", "well-known workspace file write/read"),
    (re.compile(r"['\"](state|tasks|results|logs|audit)\/[A-Za-z0-9._-]+\.(json|txt|md|jsonl|log)"), "writer", "literal ws-suffix path string"),
]

# ---- Migration coverage (shipped PRs) ---------------------------------------
COVERED_FILES = {
    # PR #1330 — subscription-scanner
    "skills/subscription-scanner",
    # PR #1331 — install-claude-hooks + verify-setup + 2 test fixtures
    "scripts/install-claude-hooks",
    "scripts/verify-setup",
    # PR #1332 — voice-agent voice-context fallback
    "src/voice-agent.ts",
    # PR #1333 — REPO/REPO_DIR rename to WORKSPACE (7 files)
    # PR #1334 — dead code + empty dirs (already deleted)
}

def is_covered(path: Path) -> bool:
    rel = str(path.relative_to(REPO))
    return any(rel.startswith(cov) for cov in COVERED_FILES)

# ---- Iter source files ------------------------------------------------------
def iter_source_files():
    for sub in SCAN_DIRS:
        base = REPO / sub
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_dir():
                continue
            if SKIP_DIRS_RE.search(str(p) + "/"):
                continue
            if p.suffix not in SCAN_EXTS:
                continue
            yield p

# ---- Reasoning generator ----------------------------------------------------
REASON_TEMPLATES = {
    "Workspace": "Path resolves under the repo source tree but the data it touches is runtime state (lives under `$SUTANDO_WORKSPACE`). Workspace-resident files should NEVER be written or read from the repo source tree — pollutes `git status`, breaks per-host isolation, and undoes the workspace contract. Fix: resolve via `${{SUTANDO_WORKSPACE:-$HOME/.sutando/workspace}}/...` in shell, or `resolve_workspace()` in Python/TS.",
    "Memory": "Memory-space access. Memory dir is per-user, cross-machine synced, and lives outside both repo and workspace. Use `${{SUTANDO_MEMORY_DIR:-$HOME/.claude/projects/<key>/memory}}/...` instead of any hardcoded path.",
    "Code": "Path accesses source-tree code (read-only). This is a Code-space access and CORRECT per V1 contract — repo source IS the canonical location. No fix needed unless the suffix points to runtime state (e.g. `src/state/...` would actually be a Workspace destination misidentified).",
    "Ambiguous": "Destination unclear from the snippet alone. Most likely a legacy-migration bridge probing old vs new defaults (e.g. memory-sync dir probing). These are intentionally kept during V1 migration window and should NOT be changed until the bridge is retired.",
}

# ---- Main scan loop ---------------------------------------------------------
def scan():
    findings = []
    for path in iter_source_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = path.relative_to(REPO)
        for i, line in enumerate(text.splitlines(), 1):
            for pat, access, label in PATTERNS:
                m = pat.search(line)
                if not m:
                    continue
                dest = dest_from_path_suffix(line)
                findings.append({
                    "file": str(rel),
                    "line": i,
                    "access": access,
                    "label": label,
                    "dest": dest,
                    "snippet": line.strip()[:200],
                    "covered": is_covered(path),
                })
    return findings

# ---- Report writer ----------------------------------------------------------
def write_report(findings, out_path: Path):
    today = datetime.now().strftime("%Y-%m-%d")
    by_access = {}
    by_dest = {}
    not_covered = []
    for f in findings:
        by_access[f["access"]] = by_access.get(f["access"], 0) + 1
        by_dest[f["dest"]] = by_dest.get(f["dest"], 0) + 1
        if not f["covered"] and f["dest"] in ("Workspace", "Memory"):
            not_covered.append(f)

    lines = []
    lines.append(f"# Workspace Contract Audit — {today}")
    lines.append("")
    lines.append(f"Repo: `{REPO}`  Workspace: `{WS}`")
    lines.append(f"Total path-access sites found: **{len(findings)}**")
    lines.append("")
    lines.append("## Aggregate")
    lines.append("")
    lines.append("| dim | count |")
    lines.append("|---|---|")
    for k in ("writer", "reader", "path-derive", "process-pattern", "config-env"):
        if k in by_access:
            lines.append(f"| access:{k} | {by_access[k]} |")
    for k in ("Code", "Workspace", "Memory", "Ambiguous"):
        if k in by_dest:
            lines.append(f"| dest:{k} | {by_dest[k]} |")
    lines.append(f"| sites covered by `#1330-#1334` | {sum(1 for f in findings if f['covered'])} |")
    lines.append(f"| **actionable not-migrated (Workspace+Memory, ❌)** | **{len(not_covered)}** |")
    lines.append("")

    # List ALL findings with full detail (code link + reasoning), grouped by destination.
    # Code-dest = correct usage (verify, no fix needed)
    # Workspace/Memory + NOT covered = actionable migration
    # Ambiguous = review case-by-case
    lines.append("## All findings (grouped by destination)")
    lines.append("")

    findings.sort(key=lambda f: (f["file"], f["line"]))

    for dest_label in ("Workspace", "Memory", "Ambiguous", "Code"):
        bucket = [f for f in findings if f["dest"] == dest_label]
        if not bucket:
            continue
        if dest_label == "Workspace":
            header = "### Workspace destination (state/tasks/results/logs/audit) — actionable if NOT covered"
        elif dest_label == "Memory":
            header = "### Memory destination — actionable if NOT covered"
        elif dest_label == "Ambiguous":
            header = "### Ambiguous (legacy migration bridges or unclear — verify before changing)"
        else:
            header = "### Code destination (source-tree access — correct per V1 contract, no fix needed)"
        lines.append(header)
        lines.append("")
        for f in bucket:
            cov = "✓ already migrated by `#1330-#1334`" if f["covered"] else "❌ NOT migrated"
            lines.append(f"#### [`{f['file']}:{f['line']}`](https://github.com/sonichi/sutando/blob/main/{f['file']}#L{f['line']}) — {f['label']}")
            lines.append(f"**Access:** {f['access']} | **Destination:** {f['dest']} | **Covered:** {cov}")
            lines.append("```")
            lines.append(f["snippet"])
            lines.append("```")
            lines.append(f"**Reasoning:** {REASON_TEMPLATES.get(f['dest'], 'No template.')}")
            lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    return len(not_covered), len(findings)

# ---- Baseline diff ----------------------------------------------------------
def write_baseline(findings, baseline_path: Path):
    sigs = sorted(f"{f['file']}:{f['line']}:{f['access']}" for f in findings if f["dest"] in ("Workspace", "Memory") and not f["covered"])
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps({"date": datetime.now().isoformat(), "actionable_sigs": sigs}, indent=2))

def diff_against_baseline(findings, baseline_path: Path):
    if not baseline_path.exists():
        return [], []
    old = json.loads(baseline_path.read_text())
    old_sigs = set(old.get("actionable_sigs", []))
    new_sigs = set(f"{f['file']}:{f['line']}:{f['access']}" for f in findings if f["dest"] in ("Workspace", "Memory") and not f["covered"])
    added = sorted(new_sigs - old_sigs)
    removed = sorted(old_sigs - new_sigs)
    return added, removed

# ---- Main -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify-on-new", action="store_true", help="DM Susan via results/proactive-*.txt if new actionable sites appeared")
    ap.add_argument("--update-baseline", action="store_true", help="Write today's set as the new baseline (default: only update if no baseline exists)")
    args = ap.parse_args()

    findings = scan()
    today = datetime.now().strftime("%Y-%m-%d")
    audit_dir = WS / "audit"
    out_path = audit_dir / f"workspace-contract-audit-{today}.md"
    baseline_path = audit_dir / "baseline.json"

    actionable, total = write_report(findings, out_path)

    added, removed = diff_against_baseline(findings, baseline_path)

    if not baseline_path.exists() or args.update_baseline:
        write_baseline(findings, baseline_path)

    print(f"Audit complete: {total} sites scanned, {actionable} actionable not-migrated.")
    print(f"Report: {out_path}")
    if added or removed:
        print(f"Diff vs baseline: +{len(added)} new, -{len(removed)} resolved")
        if added:
            print("NEW sites:")
            for s in added[:20]:
                print(f"  + {s}")
        if removed:
            print("RESOLVED sites:")
            for s in removed[:20]:
                print(f"  - {s}")

    if args.notify_on_new and added:
        notify = WS / "results" / f"proactive-audit-{int(datetime.now().timestamp())}.txt"
        notify.parent.mkdir(parents=True, exist_ok=True)
        notify.write_text(
            f"workspace-contract-audit nightly: {len(added)} NEW unmigrated path-access sites since last baseline.\n"
            f"See {out_path} for details.\nFirst 5: {chr(10).join('  ' + s for s in added[:5])}\n"
        )

if __name__ == "__main__":
    main()
