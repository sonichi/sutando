#!/usr/bin/env python3
"""LLM-free index of the owner's stock Claude Code transcripts (the stock projects
dir, `claude_home_path("projects", vanilla=True)`).

Enumerates `<root>/<slug>/*.jsonl` (top level only — `subagents/**` and every
other nested transcript is counted and skipped), drops sidechain transcripts
(first message event carries `isSidechain: true`, the session-recap rule) and
records per session the cheap metadata Claude Code already wrote:

  the LAST `ai-title` (it is re-emitted as the session goes), `custom-title`,
  `last-prompt`, `agent-name`, any `summary` record, first/last message
  timestamps, `cwd` (the real project path — slugs are lossy), `gitBranch`,
  user/assistant message counts and the byte volume of the dialog-bearing
  lines; the first prompt through context_resume's message_text/clean_text.

Every metadata string (titles, prompts, summary, agent name) is redacted with
the importer's policy — `_common.redact_text`, the same object extract.py
uses on the dumps — BEFORE it is cut to length: the cut first could leave
half a key in index.json, and the generic scanner alone let an `sk-ant-…`
key through (PR #4127 review).

One streaming pass per file with a cheap type dispatch per line: JSON is
parsed only for the short meta lines, the first message line (sidechain flag,
cwd, timestamp) and the user lines up to the first real prompt; big tool I/O
lines are never decoded. No model call.

Modes:
  (default)      full pass; writes index.json + claude-import-index.md into
                 --out-dir and records each session's (mtime,size) in state.json
  --counts-only  readdir + stat only: opens no transcript, writes nothing —
                 a FILE count, in ms (sidechains and sessions nobody answered
                 cannot be told apart without a read: `empty`/`conversations`
                 are null here; the full pass has them)
  --dry-run      full pass, writes nothing, prints counts
  --new          also list only the sessions whose (mtime,size) changed since
                 the last index run (the `new` count is always reported)
  --json         print the counts as JSON — counts only, never titles, prompts
                 or paths (the onboarding card reads this)
  --task-id ID   the task this run answers (the task header `id:`; for a
                 message-triggered run the owner message's task id). A full
                 pass is a RUN START: it mints a `run_id` (uuid4) and stores
                 `run: {task_id, run_id, started_at}` in state.json, which
                 every status.json write (here and in extract / progress /
                 finalize) carries, so the orphan check can tell whose run a
                 status is. Omitted → `task_id: null` (the orphan check then
                 keeps the task rather than archiving it); run_id is minted
                 regardless.

Counts: `sessions` is every non-sidechain transcript; `empty` the ones with no
assistant message at all (aborted / never-answered — extract.py skips them,
nothing is summarised); `conversations` = sessions − empty, the number the
owner is told ("N conversations across M projects").

The source tree is opened read-only and --out-dir may not lie inside it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402
from _common import (  # noqa: E402
    INDEX_FILE, INDEX_MD, matches_project, now_iso, parse_since, redact_text, session_key,
    split_csv, write_status,
)
from context_resume import clean_text, message_text
from util_paths import claude_home_path, write_private_text

SNIPPET_CHARS = 200
TITLE_CHARS = 120

META_TYPES = frozenset({"ai-title", "custom-title", "last-prompt", "agent-name", "summary"})
MESSAGE_TYPES = frozenset({"user", "assistant"})

# Dispatch without decoding: the raw `"type":"user"` form cannot come from message
# text (quotes inside JSON strings are escaped) or a nested object, so the first match is the record type.
_TYPE_RE = re.compile(
    r'"type":\s*"(user|assistant|ai-title|custom-title|last-prompt|agent-name|summary)"')
_TEXT_BLOCK_RE = re.compile(r'"type":\s*"text"')
_TOOL_RESULT_RE = re.compile(r'"type":\s*"tool_result"')
_CWD_RE = re.compile(r'"cwd":\s*"((?:[^"\\]|\\.)*)"')
_BRANCH_RE = re.compile(r'"gitBranch":\s*"((?:[^"\\]|\\.)*)"')
# Top-level keys sit either side of the (possibly huge) `message` object —
# real lines carry cwd/gitBranch AFTER it — so the fallback probes both ends.
_HEADER_PROBE_CHARS = 2000


def default_root() -> Path:
    """Stock Claude Code's projects dir — NOT Sutando's relocated config dir."""
    return claude_home_path("projects", vanilla=True)


# --------------------------------------------------------------------------- scan

def _line_type(line: str):
    m = _TYPE_RE.search(line)
    return m.group(1) if m else None


def _loads(line: str):
    try:
        d = json.loads(line)
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _unescape(fragment: str) -> str:
    try:
        return json.loads(f'"{fragment}"')
    except ValueError:
        return fragment


def _snippet(text, limit: int):
    if not isinstance(text, str):
        return None
    out = " ".join(text.split())
    if not out:
        return None
    return out[:limit]


def _meta(text, limit: int):
    """A metadata string as the index keeps it: the importer's redaction first,
    the cut to `limit` after — the other order can leave half a key behind."""
    if not isinstance(text, str) or not text.strip():
        return None
    return _snippet(redact_text(text)[1], limit)


def _take_header(rec: dict, d: dict) -> None:
    if rec["cwd"] is None and isinstance(d.get("cwd"), str) and d["cwd"]:
        rec["cwd"] = d["cwd"]
    if rec["git_branch"] is None and isinstance(d.get("gitBranch"), str) and d["gitBranch"]:
        rec["git_branch"] = d["gitBranch"]
    ts = d.get("timestamp")
    if isinstance(ts, str) and ts:
        if rec["first_ts"] is None:
            rec["first_ts"] = ts
        rec["last_ts"] = ts


def scan_session(path: Path) -> dict:
    """Stream one transcript once; see the module docstring for what is decoded."""
    rec = {
        "user_msgs": 0, "assistant_msgs": 0, "tool_lines": 0, "dialog_bytes": 0,
        "lines": 0, "first_ts": None, "last_ts": None, "cwd": None,
        "git_branch": None, "ai_title": None, "custom_title": None,
        "last_prompt": None, "agent_name": None, "summary": None,
        "first_prompt": None, "sidechain": False,
    }
    first_message_seen = False
    last_message_line = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            rec["lines"] += 1
            t = _line_type(line)
            if t is None:
                continue
            if t in META_TYPES:
                d = _loads(line)
                if d is None:
                    continue
                if t == "ai-title":
                    rec["ai_title"] = _meta(d.get("aiTitle"), TITLE_CHARS) or rec["ai_title"]
                elif t == "custom-title":
                    rec["custom_title"] = _meta(d.get("title"), TITLE_CHARS) or rec["custom_title"]
                elif t == "last-prompt":
                    rec["last_prompt"] = _meta(d.get("lastPrompt"), SNIPPET_CHARS) or rec["last_prompt"]
                elif t == "agent-name":
                    rec["agent_name"] = _meta(d.get("agentName"), TITLE_CHARS) or rec["agent_name"]
                elif t == "summary":
                    rec["summary"] = _meta(d.get("summary"), 500) or rec["summary"]
                continue
            if not first_message_seen:
                first_message_seen = True
                d = _loads(line)
                if d is not None:
                    if d.get("isSidechain"):
                        rec["sidechain"] = True
                        return rec
                    _take_header(rec, d)
            elif rec["cwd"] is None or rec["git_branch"] is None:
                probe = (line[:_HEADER_PROBE_CHARS] if len(line) <= 2 * _HEADER_PROBE_CHARS
                         else line[:_HEADER_PROBE_CHARS] + "\n" + line[-_HEADER_PROBE_CHARS:])
                if rec["cwd"] is None:
                    m = _CWD_RE.search(probe)
                    if m:
                        rec["cwd"] = _unescape(m.group(1)) or None
                if rec["git_branch"] is None:
                    m = _BRANCH_RE.search(probe)
                    if m:
                        rec["git_branch"] = _unescape(m.group(1)) or None
            last_message_line = line
            if t == "user":
                if _TOOL_RESULT_RE.search(line):
                    rec["tool_lines"] += 1
                    continue
                rec["user_msgs"] += 1
                rec["dialog_bytes"] += len(line)
                if rec["first_prompt"] is None:
                    d = _loads(line)
                    if d is not None:
                        text, _tools = message_text(d.get("message") or {})
                        rec["first_prompt"] = _meta(clean_text(text), SNIPPET_CHARS)
            else:
                if _TEXT_BLOCK_RE.search(line):
                    rec["assistant_msgs"] += 1
                    rec["dialog_bytes"] += len(line)
                else:
                    rec["tool_lines"] += 1
    if last_message_line is not None:
        d = _loads(last_message_line)
        if d is not None:
            ts = d.get("timestamp")
            if isinstance(ts, str) and ts:
                rec["last_ts"] = ts
                if rec["first_ts"] is None:
                    rec["first_ts"] = ts
    return rec


def session_record(slug: str, path: Path, st: os.stat_result, scan: dict) -> dict:
    """The index row; every string in `scan` already went through `_meta`."""
    title_source = "none"
    title = None
    if scan["custom_title"]:
        title, title_source = scan["custom_title"], "custom"
    elif scan["ai_title"]:
        title, title_source = scan["ai_title"], "ai"
    elif scan["first_prompt"]:
        title, title_source = scan["first_prompt"][:80], "prompt"
    return {
        "slug": slug,
        "uuid": path.stem,
        "file": path.name,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "title": title or "(untitled)",
        "title_source": title_source,
        "first_prompt": scan["first_prompt"],
        "last_prompt": scan["last_prompt"],
        "summary": scan["summary"],
        "agent_name": scan["agent_name"],
        "first_ts": scan["first_ts"],
        "last_ts": scan["last_ts"],
        "cwd": scan["cwd"],
        "git_branch": scan["git_branch"],
        "user_msgs": scan["user_msgs"],
        "assistant_msgs": scan["assistant_msgs"],
        "tool_lines": scan["tool_lines"],
        "dialog_bytes": scan["dialog_bytes"],
    }


# ---------------------------------------------------------------------- enumerate

def _nested_jsonl_count(dirpath: str) -> int:
    n = 0
    for _root, _dirs, files in os.walk(dirpath):
        n += sum(1 for f in files if f.endswith(".jsonl"))
    return n


def enumerate_root(root: Path, projects=None, since=None) -> dict:
    """readdir + stat only. Returns {slug: {"sessions": [(path, stat)], "subagent_files": n,
    "skipped_since": n}} for the matching slugs."""
    if not root.is_dir():
        raise SystemExit(f"import-claude-context: no Claude Code projects dir at {root}")
    out = {}
    with os.scandir(root) as it:
        slugs = sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
    for slug in slugs:
        if not matches_project(slug, projects):
            continue
        entry = {"sessions": [], "subagent_files": 0, "skipped_since": 0}
        with os.scandir(root / slug) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    entry["subagent_files"] += _nested_jsonl_count(e.path)
                    continue
                if not (e.name.endswith(".jsonl") and e.is_file(follow_symlinks=False)):
                    continue
                st = e.stat(follow_symlinks=False)
                if since is not None and st.st_mtime < since:
                    entry["skipped_since"] += 1
                    continue
                entry["sessions"].append((Path(e.path), st))
        entry["sessions"].sort(key=lambda ps: ps[1].st_mtime_ns)
        out[slug] = entry
    return out


# ------------------------------------------------------------------------- render

def _day(ts):
    return ts[:10] if isinstance(ts, str) and len(ts) >= 10 else "?"


def _most_common(values):
    counts = {}
    for v in values:
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def build_projects(scanned: dict) -> dict:
    """scanned: {slug: {"sessions": [record...], "sidechains": n, "subagent_files": n}}"""
    projects = {}
    for slug, entry in scanned.items():
        sessions = sorted(entry["sessions"], key=lambda r: (r["first_ts"] or "", r["uuid"]))
        firsts = [r["first_ts"] for r in sessions if r["first_ts"]]
        lasts = [r["last_ts"] for r in sessions if r["last_ts"]]
        projects[slug] = {
            "slug": slug,
            "cwd": _most_common(r["cwd"] for r in sessions),
            "git_branches": sorted({r["git_branch"] for r in sessions if r["git_branch"]}),
            "session_count": len(sessions),
            "sidechains": entry["sidechains"],
            "subagent_files": entry["subagent_files"],
            "skipped_since": entry.get("skipped_since", 0),
            "first_ts": min(firsts) if firsts else None,
            "last_ts": max(lasts) if lasts else None,
            "dialog_bytes": sum(r["dialog_bytes"] for r in sessions),
            "user_msgs": sum(r["user_msgs"] for r in sessions),
            "assistant_msgs": sum(r["assistant_msgs"] for r in sessions),
            "empty": sum(1 for r in sessions if r["assistant_msgs"] == 0),
            "sessions": sessions,
        }
    return projects


def render_index_md(index: dict) -> str:
    c = index["counts"]
    lines = [
        "# Claude Code history — import index",
        "",
        f"Generated {index['generated_at']} from `{index['root']}` — "
        f"{c['sessions']} sessions across {c['projects']} projects — "
        f"{c['conversations']} conversations, {c['empty']} with no assistant reply "
        f"({c['sidechains']} sidechain and {c['subagent_files']} subagent transcripts skipped).",
        "",
        "A session's summary lands in `summaries/<slug>/<uuid>.json`; `progress.py` counts them. "
        "A `0` assistant count is a session nobody answered: extract.py skips it, nothing is summarised.",
        "",
    ]
    projects = sorted(index["projects"].values(),
                      key=lambda p: (-p["session_count"], p["slug"]))
    for p in projects:
        if not p["sessions"]:
            continue
        lines.append(f"## {p['slug']}")
        lines.append("")
        cwd = p["cwd"] or "(cwd not recovered)"
        branches = ", ".join(p["git_branches"][:4]) or "-"
        lines.append(f"- cwd: `{cwd}` · branches: {branches}")
        lines.append(f"- sessions: {p['session_count']} · {_day(p['first_ts'])} → {_day(p['last_ts'])}"
                     f" · dialog ≈ {p['dialog_bytes'] / 1e6:.1f} MB"
                     f" · skipped: {p['sidechains']} sidechain, {p['subagent_files']} subagent")
        lines.append("")
        lines.append("| done | date | session | title | user/assistant msgs |")
        lines.append("|---|---|---|---|---|")
        for s in p["sessions"]:
            title = (s["title"] or "(untitled)").replace("|", "\\|")
            lines.append(f"| [ ] | {_day(s['first_ts'])} | {s['uuid'][:8]} | {title} "
                         f"| {s['user_msgs']}/{s['assistant_msgs']} |")
        lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- index

COUNT_KEYS = (
    "projects", "project_dirs", "sessions", "empty", "conversations", "sidechains", "subagent_files",
    "skipped_since", "user_msgs", "assistant_msgs", "tool_lines", "dialog_bytes",
    "bytes_on_disk", "earliest", "latest", "new", "counts_only", "dry_run",
)


def new_run(task_id=None) -> dict:
    """The identity of a run that starts now: the initiating task (None when
    the caller did not say) and a fresh uuid4 run id."""
    return {"task_id": task_id or None, "run_id": str(uuid.uuid4()), "started_at": now_iso()}


def index(root=None, *, out_dir, projects=None, dry_run=False, counts_only=False,
          since=None, new_only=False, task_id=None) -> dict:
    """Index the transcripts under `root` (default: stock ~/.claude/projects).

    Returns a dict with the counts (see COUNT_KEYS), `session_keys` (the
    sessions listed — only the changed ones with new_only), `run` (the
    identity minted for this run — `task_id` / `run_id` / `started_at`; None
    for the two read-only modes, which start no run) and, for a full pass,
    the full `index` structure that was (or, with dry_run, would be) written
    to `<out_dir>/index.json`.
    """
    root = Path(root) if root else default_root()
    out_dir = Path(out_dir)
    _common.refuse_inside(root, out_dir)
    if isinstance(projects, str):
        projects = split_csv(projects)
    since_ts = parse_since(since) if isinstance(since, str) else since

    listing = enumerate_root(root, projects, since_ts)
    project_dirs = len(listing)

    if counts_only:
        # stat-level truth only: sidechains cannot be told apart without a read
        sessions = sum(len(e["sessions"]) for e in listing.values())
        result = {
            "projects": sum(1 for e in listing.values() if e["sessions"]),
            "project_dirs": project_dirs,
            "sessions": sessions,
            "empty": None, "conversations": None,   # a file count cannot know
            "sidechains": None,
            "subagent_files": sum(e["subagent_files"] for e in listing.values()),
            "skipped_since": sum(e["skipped_since"] for e in listing.values()),
            "user_msgs": None, "assistant_msgs": None, "tool_lines": None,
            "dialog_bytes": None,
            "bytes_on_disk": sum(st.st_size for e in listing.values() for _p, st in e["sessions"]),
            "earliest": None, "latest": None, "new": None,
            "counts_only": True, "dry_run": True,
            "session_keys": [session_key(slug, p.stem) for slug, e in listing.items()
                             for p, _st in e["sessions"]],
            "run": None,   # a stat pass starts no run
        }
        return result

    state = _common.load_state(out_dir) if not counts_only else {"sessions": {}}
    known = state["sessions"]
    # A full pass is a run start: mint the identity now, store it with the
    # state below so every later status.json write carries it.
    run = new_run(task_id) if not dry_run else None
    scanned = {}
    new_keys = []
    all_keys = []
    for slug, entry in listing.items():
        records = []
        sidechains = 0
        for path, st in entry["sessions"]:
            scan = scan_session(path)
            if scan["sidechain"]:
                sidechains += 1
                continue
            rec = session_record(slug, path, st, scan)
            key = session_key(slug, rec["uuid"])
            prev = known.get(key) or {}
            if (prev.get("mtime_ns"), prev.get("size")) != (rec["mtime_ns"], rec["size"]):
                new_keys.append(key)
            all_keys.append(key)
            records.append(rec)
        scanned[slug] = {"sessions": records, "sidechains": sidechains,
                         "subagent_files": entry["subagent_files"],
                         "skipped_since": entry["skipped_since"]}

    fresh = build_projects(scanned)
    firsts = [p["first_ts"] for p in fresh.values() if p["first_ts"]]
    lasts = [p["last_ts"] for p in fresh.values() if p["last_ts"]]
    counts = {
        "projects": sum(1 for p in fresh.values() if p["session_count"]),
        "project_dirs": project_dirs,
        "sessions": sum(p["session_count"] for p in fresh.values()),
        "empty": sum(p["empty"] for p in fresh.values()),
        "sidechains": sum(p["sidechains"] for p in fresh.values()),
        "subagent_files": sum(p["subagent_files"] for p in fresh.values()),
        "skipped_since": sum(p["skipped_since"] for p in fresh.values()),
        "user_msgs": sum(p["user_msgs"] for p in fresh.values()),
        "assistant_msgs": sum(p["assistant_msgs"] for p in fresh.values()),
        "tool_lines": sum(sum(s["tool_lines"] for s in p["sessions"]) for p in fresh.values()),
        "dialog_bytes": sum(p["dialog_bytes"] for p in fresh.values()),
        "bytes_on_disk": sum(sum(s["size"] for s in p["sessions"]) for p in fresh.values()),
        "earliest": min(firsts) if firsts else None,
        "latest": max(lasts) if lasts else None,
        "new": len(new_keys),
        "counts_only": False,
        "dry_run": bool(dry_run),
    }
    counts["conversations"] = counts["sessions"] - counts["empty"]

    # A filtered run (--projects/--since) must not forget the projects it did
    # not look at: merge over the previous index.json.
    previous = _common.load_json(out_dir / INDEX_FILE, {}) if not dry_run else {}
    merged_projects = dict(previous.get("projects") or {})
    merged_projects.update(fresh)
    index_doc = {
        "generated_at": now_iso(),
        "root": str(root),
        "counts": counts,
        "projects": merged_projects,
    }

    if not dry_run:
        _common.ensure_private_dir(out_dir)
        write_private_text(out_dir / INDEX_FILE, _common.dump_json(index_doc))
        write_private_text(out_dir / INDEX_MD, render_index_md(index_doc))
        stamp = now_iso()
        for p in fresh.values():
            for s in p["sessions"]:
                rec = known.setdefault(session_key(s["slug"], s["uuid"]), {})
                rec.update({"mtime_ns": s["mtime_ns"], "size": s["size"], "indexed_at": stamp})
        state[_common.RUN_KEY] = run
        _common.save_state(out_dir, state)
        write_status(out_dir, "indexed", sessions=counts["sessions"], projects=counts["projects"],
                     conversations=counts["conversations"], empty=counts["empty"],
                     new=counts["new"], sidechains=counts["sidechains"],
                     subagent_files=counts["subagent_files"])

    result = dict(counts)
    result["session_keys"] = new_keys if new_only else all_keys
    result["new_session_keys"] = new_keys
    result["index"] = index_doc
    result["run"] = run
    return result


def counts_line(r: dict) -> str:
    if r.get("counts_only"):
        return (f"{r['sessions']} transcripts across {r['projects']} projects "
                f"({r['subagent_files']} subagent transcripts skipped; "
                f"{r['bytes_on_disk'] / 1e6:.1f} MB on disk) — stat only, a file count")
    return (f"{r['sessions']} sessions across {r['projects']} projects "
            f"({r['conversations']} conversations, {r['empty']} empty; "
            f"{r['sidechains']} sidechain + {r['subagent_files']} subagent transcripts skipped); "
            f"{r['new']} new since the last index; dialog ≈ {r['dialog_bytes'] / 1e6:.1f} MB; "
            f"{_day(r['earliest'])} → {_day(r['latest'])}"
            + ("; dry run — nothing written" if r["dry_run"] else ""))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None, help="Claude Code projects dir (default: stock ~/.claude/projects)")
    ap.add_argument("--out-dir", default=None, help="default <workspace>/data/claude-import")
    ap.add_argument("--workspace", default=None, help="workspace root (default: sutando-config.sh workspace)")
    ap.add_argument("--projects", default=None, help="comma-separated slugs or slug substrings")
    ap.add_argument("--since", default=None, help="only sessions modified since: 30d, 12h, 2w or YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="full pass, write nothing")
    ap.add_argument("--counts-only", action="store_true", help="readdir+stat only; opens no transcript")
    ap.add_argument("--new", action="store_true", help="list only sessions changed since the last index")
    ap.add_argument("--json", action="store_true", help="print counts as JSON (counts only)")
    ap.add_argument("--task-id", default=None,
                    help="the task this run answers (task header `id:`); stored with the minted run id "
                         "in state.json and every status.json so the orphan check can match them")
    a = ap.parse_args(_common.absorb_dash_values(argv, ("--projects",)))

    out_dir = _common.data_dir(a.workspace, a.out_dir)
    r = index(a.root, out_dir=out_dir, projects=split_csv(a.projects), dry_run=a.dry_run,
              counts_only=a.counts_only, since=a.since, new_only=a.new, task_id=a.task_id)
    if a.json:
        print(json.dumps({k: r.get(k) for k in COUNT_KEYS}, sort_keys=True))
    else:
        print(counts_line(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
