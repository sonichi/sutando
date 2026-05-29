#!/usr/bin/env python3
"""Workspace contract audit — LLM-based version.

Sends each candidate file to gemini-2.5-flash with a system prompt describing
the V1 workspace contract (Code / Workspace / Memory spaces), asks the model
to list all path-access points and classify each. Better than the regex
version (run-audit.py, v0.1) because it understands semantic intent — can
distinguish "Code-correct readFileSync of own source" from
"Workspace-violation readFileSync of state file under repo dir".

Usage:
  python3 ~/.claude/skills/workspace-contract-audit/scripts/run-audit-llm.py
  python3 ~/.claude/skills/workspace-contract-audit/scripts/run-audit-llm.py --files src/dashboard.py,scripts/sync-memory.sh   # focused
  python3 ~/.claude/skills/workspace-contract-audit/scripts/run-audit-llm.py --notify-on-new
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# ---- Workspace resolution -----------------------------------------------------
def resolve_workspace() -> Path:
    ws = os.environ.get("SUTANDO_WORKSPACE")
    if ws:
        return Path(os.path.expanduser(ws))
    return Path.home() / ".sutando" / "workspace"

def resolve_repo() -> Path:
    rd = os.environ.get("SUTANDO_REPO_DIR")
    if rd:
        return Path(os.path.expanduser(rd))
    here = Path(__file__).resolve()
    for cand in [here.parent.parent.parent.parent, Path.home() / "Documents" / "sutando" / "sutando", Path.home() / "Desktop" / "sutando"]:
        if (cand / "CLAUDE.md").exists() and (cand / "skills").is_dir():
            return cand
    raise SystemExit("ERROR: cannot resolve repo dir")

REPO = resolve_repo()
WS = resolve_workspace()

# Load .env so GEMINI_API_KEY is set
env_path = REPO / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))

from google import genai
from google.genai import types

API_KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
if not API_KEY:
    raise SystemExit("ERROR: GEMINI_API_KEY not set")
client = genai.Client(api_key=API_KEY)

# ---- Candidate file pre-filter -----------------------------------------------
SCAN_DIRS = ["src", "skills", "scripts", "tests"]
SKIP_DIRS_RE = re.compile(r"/(node_modules|\.build|\.git|\.venv|__pycache__|\.pytest_cache|sample/)/")
SCAN_EXTS = {".ts", ".py", ".sh", ".swift", ".js", ".tsx", ".jsx", ".json"}

# Pre-filter: only send files that contain at least one likely path-access marker.
# Keeps LLM token cost bounded.
PREFILTER_PATTERNS = [
    re.compile(r"\$\{?REPO[_A-Z]*\}?/"),
    re.compile(r"\$\{?SUTANDO_(REPO_DIR|WORKSPACE|MEMORY_DIR|PRIVATE_DIR)\}?/"),
    re.compile(r"\$\{?WORKSPACE\}?/|\$\{?WS\}?/"),
    re.compile(r"~/Documents/sutando|~/Desktop/sutando|~/\.sutando|~/\.claude/projects"),
    re.compile(r"\.claude/projects/[-A-Za-z0-9]+/memory"),
    re.compile(r"(readFileSync|writeFileSync|read_text|write_text|mkdir|mkdirSync)\s*\("),
    re.compile(r"pgrep\s+-f|pkill\s+-f"),
    re.compile(r"(state|tasks|results|logs|notes|audit)/[a-zA-Z0-9._-]+\.(json|md|txt|jsonl|log)"),
    re.compile(r"build_log\.md|pending-questions\.md|core-status\.json|voice-session-context\.json|quota-state\.json"),
    re.compile(r"REPO_DIR|REPO_ROOT|WORKSPACE_DIR|MEMORY_DIR"),
]

def is_candidate(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return False
    return any(p.search(text) for p in PREFILTER_PATTERNS)

def iter_candidate_files(no_prefilter: bool = False):
    """Yield candidate files. If `no_prefilter`, yield every file in SCAN_DIRS
    matching an allowed extension — exhaustive 0-to-2-space audit mode where
    pre-V1 code may not contain any V1-aware markers (the very gap we're hunting).
    Slower (3x+) but catches files the prefilter misses."""
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
            if no_prefilter or is_candidate(p):
                yield p

# ---- LLM system prompt -------------------------------------------------------
SYSTEM_PROMPT = """You are auditing source code in the Sutando repository for path-access points that touch one of three spaces under the V1 workspace contract:

- **Code space** — the repo source tree itself (read-only canonical home of `.ts`, `.py`, `.swift` files, skills/ source, etc.). Path roughly: `~/Documents/sutando/sutando/` or `$REPO`.
- **Workspace space** — per-host runtime state (tasks/, results/, state/, logs/, notes/, audit/, build_log.md, pending-questions.md, voice-session-context.json, core-status.json, quota-state.json). Path: `$SUTANDO_WORKSPACE` or `~/.sutando/workspace/`.
- **Memory space** — per-user cross-host synced content (memory/MEMORY.md and individual memory files). Path: `$SUTANDO_MEMORY_DIR` or `~/.claude/projects/<key>/memory/`.

The migration is going from a 0-space world (everything implicitly under the repo) to an explicit 2-space (Code vs Workspace) + 3rd space (Memory) contract. Many older path-access points still hardcode the repo dir for workspace-or-memory data — that's the V1 violation we're hunting.

For each line in the file that touches a path under any of these three spaces, return a JSON object with:

- `line`: 1-indexed line number
- `snippet`: the literal source line (strip leading whitespace, max 200 chars)
- `access`: one of `writer` | `reader` | `path-derive` | `process-pattern` | `manifest-text` | `config-env`
- `destination`: one of `Code` | `Workspace` | `Memory` | `Ambiguous`
- `is_violation`: true if destination is Workspace or Memory but the path resolves under the repo source tree (i.e. NOT using `$SUTANDO_WORKSPACE` / `$SUTANDO_MEMORY_DIR`); false if the access is correct per V1 (Code-reading source, or already using the right env)
- `reasoning`: 1-2 sentences explaining the classification and why it is or isn't a violation
- `severity`: one of `high` (production code, live path) | `medium` (utility script, test fixture) | `low` (defensive/legacy compatibility code that's intentionally probing old paths)

Output STRICTLY as a JSON object with a single key `findings` whose value is an array of these objects. If the file has zero path-access points, return `{"findings": []}`. No prose outside the JSON.

Categorization rules:

1. `readFileSync(path_to_a_src_file_in_repo)` → `Code` destination, `is_violation: false`.
2. `readFileSync(REPO + "/state/foo.json")` or `Path(REPO_DIR) / "tasks" / "..."` → `Workspace` destination, `is_violation: true` (state/tasks/results/logs/notes/audit all belong to Workspace).
3. `readFileSync(SUTANDO_WORKSPACE + "/state/foo.json")` → `Workspace` destination, `is_violation: false`.
4. `~/.claude/projects/<key>/memory/...` references → `Memory` destination.
5. Files in `scripts/sync-memory.sh` or `scripts/stage-readiness.sh` that probe both old and new memory-sync defaults (`$HOME/.sutando/memory-sync` vs `$HOME/.sutando-memory-sync`) → `Ambiguous`, `is_violation: false`, `low` severity — these are intentional legacy bridges during the migration window.
6. Skip generic file I/O that isn't sutando-domain (`Path("/tmp/...")`, `os.tmpfile()`, third-party config files outside the repo): just return `{"findings": []}` for files where no sutando-domain path-access exists.

Be selective. Only flag actual path-access points; don't return every `Path(...)` constructor or every `open()` call regardless of context. The goal is to surface migration candidates, not to enumerate every I/O call."""

# ---- LLM call ----------------------------------------------------------------
def classify_file(path: Path, text: str, max_retries: int = 2) -> dict:
    """Call gemini-2.5-flash with the file content + system prompt, parse JSON."""
    rel = str(path.relative_to(REPO))
    user_prompt = f"File: {rel}\n\nContent:\n```{path.suffix.lstrip('.') or 'text'}\n{text[:60_000]}\n```\n\nReturn the JSON object now."

    for attempt in range(max_retries + 1):
        try:
            resp = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[types.Content(role="user", parts=[types.Part(text=user_prompt)])],
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.0,
                    response_mime_type="application/json",
                ),
            )
            if not (resp.candidates and resp.candidates[0].content and resp.candidates[0].content.parts):
                return {"file": rel, "error": "empty response", "findings": []}
            raw = resp.candidates[0].content.parts[0].text or ""
            try:
                d = json.loads(raw)
                findings = d.get("findings", []) if isinstance(d, dict) else []
                # Tag each finding with the file path
                for f in findings:
                    f["file"] = rel
                return {"file": rel, "findings": findings}
            except json.JSONDecodeError as e:
                if attempt < max_retries:
                    time.sleep(1)
                    continue
                return {"file": rel, "error": f"JSON parse failed: {e}", "raw_excerpt": raw[:300], "findings": []}
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2)
                continue
            return {"file": rel, "error": str(e), "findings": []}

# ---- Cache (per-file-hash) ---------------------------------------------------
def cache_dir() -> Path:
    p = WS / "audit" / "llm-cache"
    p.mkdir(parents=True, exist_ok=True)
    return p

def cache_key(path: Path, text: str) -> str:
    h = hashlib.sha256()
    h.update(str(path.relative_to(REPO)).encode())
    h.update(b"\n")
    h.update(text.encode("utf-8", errors="replace"))
    return h.hexdigest()[:16]

def load_cached(key: str) -> dict | None:
    f = cache_dir() / f"{key}.json"
    if f.exists():
        try:
            return json.loads(f.read_text())
        except Exception:
            return None
    return None

def save_cached(key: str, result: dict) -> None:
    (cache_dir() / f"{key}.json").write_text(json.dumps(result, indent=2))

# ---- Report writer (re-uses regex-version markdown format) -------------------
def write_report(all_findings: list, out_path: Path, candidate_files: list, llm_metadata: dict) -> tuple[int, int]:
    today = datetime.now().strftime("%Y-%m-%d")
    actionable = [f for f in all_findings if f.get("is_violation")]
    by_dest = {}
    by_access = {}
    by_severity = {}
    for f in all_findings:
        by_dest[f.get("destination", "?")] = by_dest.get(f.get("destination", "?"), 0) + 1
        by_access[f.get("access", "?")] = by_access.get(f.get("access", "?"), 0) + 1
        if f.get("is_violation"):
            by_severity[f.get("severity", "?")] = by_severity.get(f.get("severity", "?"), 0) + 1

    lines = []
    lines.append(f"# Workspace Contract Audit — {today} (LLM)")
    lines.append("")
    lines.append(f"Repo: `{REPO}`  Workspace: `{WS}`")
    lines.append(f"Model: `gemini-2.5-flash`  | candidate files scanned: **{llm_metadata.get('files_scanned', 0)}** | findings: **{len(all_findings)}** | violations: **{len(actionable)}**")
    lines.append(f"LLM stats: {llm_metadata.get('cache_hits', 0)} cached, {llm_metadata.get('llm_calls', 0)} live calls, {llm_metadata.get('errors', 0)} errors, ~{llm_metadata.get('elapsed_s', 0)}s elapsed")
    lines.append("")
    lines.append("## Aggregate")
    lines.append("")
    lines.append("| dim | count |")
    lines.append("|---|---|")
    for k in ("writer", "reader", "path-derive", "process-pattern", "manifest-text", "config-env"):
        if k in by_access:
            lines.append(f"| access:{k} | {by_access[k]} |")
    for k in ("Code", "Workspace", "Memory", "Ambiguous"):
        if k in by_dest:
            lines.append(f"| dest:{k} | {by_dest[k]} |")
    lines.append(f"| **violations (Workspace+Memory + repo-rooted)** | **{len(actionable)}** |")
    if by_severity:
        for sev in ("high", "medium", "low"):
            if sev in by_severity:
                lines.append(f"| severity:{sev} | {by_severity[sev]} |")
    lines.append("")

    actionable.sort(key=lambda f: (f.get("severity", "z"), f.get("file", ""), f.get("line", 0)))

    lines.append("## Violations (Workspace+Memory, repo-rooted)")
    lines.append("")
    if not actionable:
        lines.append("(none — clean)")
    for f in actionable:
        sev = f.get("severity", "?")
        sev_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(sev, "?")
        lines.append(f"### {sev_emoji} [`{f['file']}:{f['line']}`](https://github.com/sonichi/sutando/blob/main/{f['file']}#L{f['line']}) — {f.get('access','?')} → {f.get('destination','?')}")
        lines.append(f"**Severity:** {sev}")
        lines.append("```")
        lines.append((f.get("snippet") or "")[:200])
        lines.append("```")
        lines.append(f"**Reasoning:** {f.get('reasoning','(none)')}")
        lines.append("")

    # Optional: ambiguous + non-violation findings tail (for context, not action)
    non_viol = [f for f in all_findings if not f.get("is_violation") and f.get("destination") in ("Ambiguous", "Memory", "Workspace")]
    if non_viol:
        lines.append("---")
        lines.append("")
        lines.append("## Non-violations (intentional / already-migrated) — for context")
        lines.append("")
        non_viol.sort(key=lambda f: (f.get("file", ""), f.get("line", 0)))
        for f in non_viol[:40]:
            lines.append(f"- [`{f['file']}:{f['line']}`](https://github.com/sonichi/sutando/blob/main/{f['file']}#L{f['line']}) — {f.get('access','?')} → {f.get('destination','?')}: `{(f.get('snippet') or '')[:100]}`")
        if len(non_viol) > 40:
            lines.append(f"- ...and {len(non_viol) - 40} more")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    return len(actionable), len(all_findings)

# ---- Main --------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", help="comma-separated file paths (relative to repo) — overrides auto-scan")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--no-prefilter", action="store_true", help="scan every file matching SCAN_EXTS, skip regex-marker prefilter (0-to-2-space exhaustive)")
    ap.add_argument("--limit", type=int, help="max files to scan (for testing)")
    ap.add_argument("--concurrency", type=int, default=6, help="parallel LLM calls (default 6)")
    args = ap.parse_args()

    if args.files:
        files = [REPO / f.strip() for f in args.files.split(",")]
        files = [f for f in files if f.exists()]
    else:
        files = list(iter_candidate_files(no_prefilter=args.no_prefilter))
        if args.limit:
            files = files[: args.limit]

    print(f"Scanning {len(files)} file(s) with {args.concurrency} parallel workers...", file=sys.stderr)
    all_findings = []
    cache_hits = 0
    llm_calls = 0
    errors = 0
    start = time.time()
    done_count = [0]

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from threading import Lock
    stats_lock = Lock()

    def worker(path: Path):
        nonlocal cache_hits, llm_calls, errors
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            with stats_lock:
                done_count[0] += 1
                print(f"  [{done_count[0]}/{len(files)}] skip {path.relative_to(REPO)}: {e}", file=sys.stderr)
            return None

        key = cache_key(path, text)
        cached = None if args.no_cache else load_cached(key)
        if cached:
            with stats_lock:
                cache_hits += 1
                done_count[0] += 1
            return cached
        result = classify_file(path, text)
        save_cached(key, result)
        with stats_lock:
            llm_calls += 1
            done_count[0] += 1
            if done_count[0] % 10 == 0 or done_count[0] == len(files):
                elapsed = int(time.time() - start)
                print(f"  [{done_count[0]}/{len(files)}] +{result['file']} | elapsed {elapsed}s, cache={cache_hits} llm={llm_calls} err={errors}", file=sys.stderr)
        return result

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = [executor.submit(worker, p) for p in files]
        for fut in as_completed(futures):
            result = fut.result()
            if result is None:
                continue
            if result.get("error"):
                with stats_lock:
                    errors += 1
                print(f"    error in {result.get('file','?')}: {result['error']}", file=sys.stderr)
            all_findings.extend(result.get("findings", []))

    today = datetime.now().strftime("%Y-%m-%d")
    out_path = WS / "audit" / f"workspace-contract-audit-llm-{today}.md"
    actionable, total = write_report(
        all_findings,
        out_path,
        files,
        {
            "files_scanned": len(files),
            "cache_hits": cache_hits,
            "llm_calls": llm_calls,
            "errors": errors,
            "elapsed_s": int(time.time() - start),
        },
    )

    print(f"Done. {total} findings, {actionable} violations.")
    print(f"Cache: {cache_hits} hits, {llm_calls} live LLM calls, {errors} errors. Elapsed: {int(time.time() - start)}s")
    print(f"Report: {out_path}")

if __name__ == "__main__":
    main()
