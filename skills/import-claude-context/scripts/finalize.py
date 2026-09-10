#!/usr/bin/env python3
"""Turn the haiku summaries into Sutando's sinks — and undo them per project.

Reads, all under <data-dir> (= <workspace>/data/claude-import/):
  index.json                      session metadata (titles, dates, cwd) from index.py
  summaries/<slug>/<uuid>.json    one merged per-session summary (partials
                                  `<uuid>.<n>.json` are ignored)
  projects/<slug>.json            per-project roll-up incl. `note_markdown`
  entities.json                   people / companies / deals / decisions / open threads

Writes:
  <memory-dir>/claude_import.md               <= 2,000 bytes: "Imported N sessions /
                                              M projects on <date>", one line per
                                              project (name, what it is, status, top
                                              open thread), pointer to notes/claude-import/
  <memory-dir>/MEMORY.md                      ONE row, only when
                                              memory-index-budget.py --adding <row> exits 0
                                              (refused -> row skipped, logged, file kept)
  <workspace>/notes/claude-import/<slug>.md   one note per project; a re-run with a
                                              changed roll-up appends `## Update <date>`
  <workspace>/notes/claude-import/overview.md regenerated each run
  <data-dir>/status.json                      phase + counts only
  <data-dir>/state.json                       `summarized_at` per session

  --people-json     print <=25 People upsert payloads (people with >=2 citations)
                    for the station's people__upsert_person tool; writes nothing
  --forget <slug>   remove exactly that project's note, summaries, dumps, roll-up,
                    memory line, state and entity citations
  --purge-dumps     delete <data-dir>/dumps/ (run after a successful finalize)

The memory dir is util_paths.memory_dir() (the core's relocated tree); a memory
dir under the stock ~/.claude is refused unless passed explicitly — the import
never writes into Claude Code's own home.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402
from _common import (  # noqa: E402
    DUMPS_DIR, ENTITIES_FILE, INDEX_FILE, PROJECTS_DIR, REPO, SUMMARIES_DIR,
    now_iso, session_key, today, write_status,
)
from util_paths import claude_home_path, memory_dir as core_memory_dir  # noqa: E402

MEMORY_FILE = "claude_import.md"
MEMORY_INDEX = "MEMORY.md"
MEMORY_LIMIT = 2_000
MEMORY_ROW_LINK = f"]({MEMORY_FILE})"
NOTES_SUBDIR = ("notes", "claude-import")
OVERVIEW = "overview.md"
PEOPLE_CAP = 25
PEOPLE_MIN_CITATIONS = 2
BUDGET_SCRIPT = REPO / "skills" / "proactive-loop" / "scripts" / "memory-index-budget.py"
ENTITY_LISTS = ("people", "companies", "deals", "decisions", "open_threads")


def memory_row(n_projects: int) -> str:
    return (f"- [Claude Code history import]({MEMORY_FILE}) — "
            f"{n_projects} projects, open threads, people")


# -------------------------------------------------------------------- resolve

def resolve_memory_dir(explicit=None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    md = core_memory_dir()
    vanilla = claude_home_path(vanilla=True)
    real_md, real_v = os.path.realpath(str(md)), os.path.realpath(str(vanilla))
    try:
        inside = os.path.commonpath([real_md, real_v]) == real_v
    except ValueError:
        inside = False
    if inside:
        raise SystemExit(
            f"import-claude-context: the resolved memory dir {md} lies under the stock "
            f"Claude home {vanilla}; the import never writes there. Run inside the core "
            f"(CLAUDE_CONFIG_DIR set) or pass --memory-dir explicitly.")
    return md


def notes_dir(ws: Path) -> Path:
    return ws.joinpath(*NOTES_SUBDIR)


# ----------------------------------------------------------------------- inputs

def load_summaries(data_dir: Path) -> dict:
    """{(slug, uuid): summary dict} from summaries/<slug>/<uuid>.json (merged files only)."""
    out = {}
    base = data_dir / SUMMARIES_DIR
    if not base.is_dir():
        return out
    for slug_dir in sorted(p for p in base.iterdir() if p.is_dir()):
        for f in sorted(slug_dir.glob("*.json")):
            if f.name.count(".") != 1:
                continue  # `<uuid>.<n>.json` partials
            doc = _common.load_json(f, None)
            if isinstance(doc, dict):
                out[(slug_dir.name, f.stem)] = doc
    return out


def load_rollups(data_dir: Path) -> dict:
    out = {}
    base = data_dir / PROJECTS_DIR
    if not base.is_dir():
        return out
    for f in sorted(base.glob("*.json")):
        doc = _common.load_json(f, None)
        if isinstance(doc, dict):
            out[f.stem] = doc
    return out


def load_entities(data_dir: Path) -> dict:
    doc = _common.load_json(data_dir / ENTITIES_FILE, {}) or {}
    for k in ENTITY_LISTS:
        v = doc.get(k)
        doc[k] = v if isinstance(v, list) else []
    return doc


def session_meta(index_doc: dict, slug: str, uuid: str) -> dict:
    for s in ((index_doc.get("projects") or {}).get(slug) or {}).get("sessions") or []:
        if s.get("uuid") == uuid:
            return s
    return {}


def project_meta(index_doc: dict, slug: str) -> dict:
    return (index_doc.get("projects") or {}).get(slug) or {}


def display_name(slug: str, rollup: dict, index_doc: dict) -> str:
    name = rollup.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    cwd = project_meta(index_doc, slug).get("cwd")
    if isinstance(cwd, str) and cwd.strip("/"):
        return cwd.rstrip("/").rsplit("/", 1)[-1]
    return slug.strip("-").rsplit("-", 1)[-1] or slug


def _day(ts):
    return ts[:10] if isinstance(ts, str) and len(ts) >= 10 else "?"


def _one_line(text, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    out = " ".join(text.split())
    return out if len(out) <= limit else out[: max(0, limit - 1)].rstrip() + "…"


def date_range(index_doc: dict, slugs) -> tuple:
    firsts, lasts = [], []
    for slug in slugs:
        p = project_meta(index_doc, slug)
        if p.get("first_ts"):
            firsts.append(p["first_ts"])
        if p.get("last_ts"):
            lasts.append(p["last_ts"])
    return (_day(min(firsts)) if firsts else "?", _day(max(lasts)) if lasts else "?")


def header_line(index_doc: dict, slugs, run_kind: str) -> str:
    first, last = date_range(index_doc, slugs)
    return f"*[imported, claude-code] — import-claude-context | {first} → {last} | {run_kind}*"


# ------------------------------------------------------------------- memory file

def _project_line(slug: str, rollup: dict, index_doc: dict) -> str:
    name = display_name(slug, rollup, index_doc)
    what = _one_line(rollup.get("what_it_is") or rollup.get("summary"), 90) or "(no roll-up yet)"
    status = _one_line(rollup.get("status"), 20) or "unknown"
    thread = _one_line(rollup.get("top_open_thread"), 90)
    line = f"- {name} — {what}; {status}"
    if thread:
        line += f"; open: {thread}"
    return line


def render_memory_file(rollups: dict, index_doc: dict, n_sessions: int, limit: int = MEMORY_LIMIT) -> str:
    slugs = sorted(rollups, key=lambda s: (-(project_meta(index_doc, s).get("session_count") or 0), s))
    header = (f"# Claude Code history import\n\n"
              f"Imported {n_sessions} sessions / {len(slugs)} projects on {today()} "
              f"(from the owner's Claude Code transcripts; per-project notes in notes/claude-import/, "
              f"overview.md first).\n")
    lines = [_project_line(s, rollups[s], index_doc) for s in slugs]

    def total(ls):
        return len((header + "\n".join(ls) + ("\n" if ls else "")).encode("utf-8"))

    if total(lines) > limit and lines:
        share = max(48, (limit - len(header.encode("utf-8"))) // len(lines) - 1)
        lines = [_one_line(ln, share) for ln in lines]
    more = 0

    def tail(k):
        return [f"- …and {k} more projects in notes/claude-import/overview.md"] if k else []

    while lines and total(lines + tail(more)) > limit:
        lines.pop()
        more += 1
    body = lines + tail(more)
    text = header + "\n".join(body) + ("\n" if body else "")
    assert len(text.encode("utf-8")) <= limit, "memory file over budget"
    return text


def guard_memory_row(memory_dir: Path, row: str) -> tuple:
    """(written: bool, reason: str). Delegates the budget to memory-index-budget.py."""
    index_path = memory_dir / MEMORY_INDEX
    if not index_path.is_file():
        index_path.write_text(row + "\n", encoding="utf-8")
        return True, "created MEMORY.md with the row (empty index, nothing to drop)"
    text = index_path.read_text(encoding="utf-8", errors="replace")
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if MEMORY_ROW_LINK in ln:
            if len(row.encode("utf-8")) <= len(ln.encode("utf-8")) and ln.strip() != row:
                lines[i] = row
                index_path.write_text("\n".join(lines), encoding="utf-8")
                return True, "row already present; refreshed in place (no longer than before)"
            return True, "row already present"
    proc = subprocess.run(
        [sys.executable, str(BUDGET_SCRIPT), "--repo", str(REPO), "--index", str(index_path),
         "--adding", row],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()
        reason = tail[-1] if tail else f"exit {proc.returncode}"
        return False, f"memory-index-budget refused (exit {proc.returncode}): {reason}"
    with open(index_path, "a", encoding="utf-8") as fh:
        if text and not text.endswith("\n"):
            fh.write("\n")
        fh.write(row + "\n")
    return True, "row appended (budget exit 0)"


def remove_memory_row(memory_dir: Path) -> bool:
    index_path = memory_dir / MEMORY_INDEX
    if not index_path.is_file():
        return False
    text = index_path.read_text(encoding="utf-8", errors="replace")
    kept = [ln for ln in text.split("\n") if MEMORY_ROW_LINK not in ln]
    if len(kept) == len(text.split("\n")):
        return False
    index_path.write_text("\n".join(kept), encoding="utf-8")
    return True


# ------------------------------------------------------------------------- notes

def _frontmatter(title: str) -> str:
    return f"---\ntitle: {title}\ndate: {today()}\ntags: [imported, claude-code]\n---\n"


def _sessions_list(slug: str, rollup: dict, index_doc: dict, summaries: dict) -> str:
    uuids = rollup.get("sessions")
    if not isinstance(uuids, list) or not uuids:
        uuids = [u for (s, u) in summaries if s == slug]
    rows = []
    for u in uuids:
        if not isinstance(u, str):
            continue
        meta = session_meta(index_doc, slug, u)
        title = meta.get("title") or (summaries.get((slug, u)) or {}).get("title") or "(untitled)"
        rows.append(f"- {_day(meta.get('first_ts'))} — {_one_line(title, 100)} ({u[:8]})")
    return "\n".join(rows)


def _note_hash(rollup: dict) -> str:
    return hashlib.sha256((rollup.get("note_markdown") or "").encode("utf-8")).hexdigest()[:16]


def write_project_note(ndir: Path, slug: str, rollup: dict, index_doc: dict, summaries: dict,
                       state: dict, run_kind: str) -> str:
    """Returns 'created' | 'updated' | 'unchanged'."""
    path = ndir / f"{slug}.md"
    note = (rollup.get("note_markdown") or "").strip() or "_(no roll-up text)_"
    sessions = _sessions_list(slug, rollup, index_doc, summaries)
    digest = _note_hash(rollup)
    prev = state["projects"].get(slug) or {}
    if path.is_file():
        if prev.get("note_hash") == digest:
            return "unchanged"
        block = f"\n\n## Update {today()}\n\n{note}\n"
        if sessions:
            block += f"\n### Sessions\n{sessions}\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(block)
        result = "updated"
    else:
        name = display_name(slug, rollup, index_doc)
        cwd = project_meta(index_doc, slug).get("cwd")
        text = _frontmatter(f"Claude Code history — {name}")
        text += header_line(index_doc, [slug], run_kind) + "\n\n"
        text += f"# {name}\n\n"
        if cwd:
            text += f"Project dir: `{cwd}` (Claude Code slug `{slug}`)\n\n"
        text += note + "\n"
        if sessions:
            text += f"\n## Sessions\n{sessions}\n"
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        result = "created"
    state["projects"][slug] = {"note_hash": digest, "note_written_at": now_iso()}
    return result


def render_overview(rollups: dict, index_doc: dict, n_sessions: int, run_kind: str) -> str:
    slugs = sorted(rollups, key=lambda s: (-(project_meta(index_doc, s).get("session_count") or 0), s))
    text = _frontmatter("Claude Code history import — overview")
    text += header_line(index_doc, slugs, run_kind) + "\n\n"
    text += (f"# Claude Code history import\n\n"
             f"Imported {n_sessions} sessions across {len(slugs)} projects from the owner's "
             f"Claude Code transcripts (conversation text only; raw dumps stay in "
             f"`data/claude-import/`, which is not synced). One note per project sits next "
             f"to this file; `--forget <slug>` removes one.\n\n## Projects\n")
    for slug in slugs:
        r = rollups[slug]
        p = project_meta(index_doc, slug)
        name = display_name(slug, r, index_doc)
        what = _one_line(r.get("what_it_is") or r.get("summary"), 140)
        status = _one_line(r.get("status"), 20) or "unknown"
        thread = _one_line(r.get("top_open_thread"), 140)
        line = f"- [{name}]({slug}.md) — {what or '(no roll-up)'} · {status}"
        line += f" · {p.get('session_count') or 0} sessions ({_day(p.get('first_ts'))} → {_day(p.get('last_ts'))})"
        if thread:
            line += f" · open: {thread}"
        text += line + "\n"
    return text


# ------------------------------------------------------------------------ people

def _citation_project(c) -> str:
    return c.get("project") if isinstance(c, dict) else ""


def _citation_session(c) -> str:
    return c.get("session") if isinstance(c, dict) else ""


def people_payloads(entities: dict, index_doc: dict, cap: int = PEOPLE_CAP,
                    min_citations: int = PEOPLE_MIN_CITATIONS) -> list:
    people = [p for p in entities.get("people") or [] if isinstance(p, dict) and p.get("name")]
    people = [p for p in people if len([c for c in p.get("citations") or [] if isinstance(c, dict)]) >= min_citations]
    people.sort(key=lambda p: (-len(p.get("citations") or []), str(p.get("name"))))
    payloads = []
    for p in people[:cap]:
        cites = [c for c in p.get("citations") or [] if isinstance(c, dict)]
        dated = []
        for c in cites:
            meta = session_meta(index_doc, _citation_project(c) or "", _citation_session(c) or "")
            dated.append((meta.get("last_ts") or "", meta.get("title") or "", c))
        dated.sort(key=lambda t: t[0], reverse=True)
        name = _one_line(p["name"], 80)
        email = p.get("email") if isinstance(p.get("email"), str) and "@" in p.get("email", "") else None
        company = _one_line(p.get("company"), 80)
        role = _one_line(p.get("role"), 80)
        relationship = _one_line(p.get("relationship"), 200)
        doc = [f"# {name}", "", "## Contact",
               f"- Email: {email or 'unknown'}",
               f"- Company: {company or 'unknown'} · Role: {role or 'unknown'}",
               "", "## Who they are",
               " · ".join(x for x in (role, company) if x) or "(from Claude Code sessions only)",
               "", "## Your relationship", relationship or "(not stated)",
               "", "## Recent interactions"]
        for ts, title, c in dated[:8]:
            doc.append(f"- {_day(ts)} — {_one_line(title, 60) or 'session'}: "
                       f"{_one_line(c.get('quote_or_context') or c.get('context'), 160)}")
        doc += ["", "## Claims and citations"]
        for ts, title, c in dated:
            sess = (_citation_session(c) or "")[:8]
            doc.append(f"- \"{_one_line(c.get('quote_or_context') or c.get('context'), 160)}\" — "
                       f"Claude Code session {sess or '?'} ({_one_line(_citation_project(c), 60) or '?'}), {_day(ts)}")
        doc += ["", "_Imported by import-claude-context from the owner's Claude Code history._"]
        payload = {
            "name": name,
            "doc": "\n".join(doc) + "\n",
            "source": "claude-import",
            "last_interaction_at": dated[0][0] if dated and dated[0][0] else None,
        }
        if email:
            payload["email"] = email
            payload["identifiers"] = {"emails": [email]}
        payloads.append(payload)
    return payloads


# ---------------------------------------------------------------------- finalize

def finalize(*, data_dir: Path, ws: Path, memory_dir: Path, run_kind: str = "user") -> dict:
    index_doc = _common.load_json(data_dir / INDEX_FILE, {}) or {}
    summaries = load_summaries(data_dir)
    rollups = load_rollups(data_dir)
    entities = load_entities(data_dir)
    state = _common.load_state(data_dir)

    n_sessions = index_doc.get("counts", {}).get("sessions") or len(summaries)
    stamp = now_iso()
    for (slug, uuid) in summaries:
        rec = state["sessions"].setdefault(session_key(slug, uuid), {})
        rec.setdefault("summarized_at", stamp)
        if rec.get("extracted_at") and rec["summarized_at"] < rec["extracted_at"]:
            rec["summarized_at"] = stamp

    ndir = notes_dir(ws)
    ndir.mkdir(parents=True, exist_ok=True)
    outcomes = {"created": 0, "updated": 0, "unchanged": 0}
    for slug, rollup in rollups.items():
        outcomes[write_project_note(ndir, slug, rollup, index_doc, summaries, state, run_kind)] += 1
    if rollups:
        (ndir / OVERVIEW).write_text(render_overview(rollups, index_doc, n_sessions, run_kind),
                                     encoding="utf-8")

    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_text = render_memory_file(rollups, index_doc, n_sessions)
    (memory_dir / MEMORY_FILE).write_text(memory_text, encoding="utf-8")
    row_written, row_reason = guard_memory_row(memory_dir, memory_row(len(rollups)))
    print(f"import-claude-context: MEMORY.md row — {row_reason}", file=sys.stderr)

    people = people_payloads(entities, index_doc)
    _common.save_state(data_dir, state)
    counts = {
        "sessions": int(n_sessions), "summarized": len(summaries), "projects": len(rollups),
        "notes_created": outcomes["created"], "notes_updated": outcomes["updated"],
        "notes_unchanged": outcomes["unchanged"], "people": len(people),
        "memory_bytes": len(memory_text.encode("utf-8")), "memory_row": bool(row_written),
    }
    write_status(data_dir, "done", **counts)
    return counts


def forget(*, data_dir: Path, ws: Path, memory_dir: Path, slug: str, run_kind: str = "user") -> dict:
    removed = {"note": 0, "summaries": 0, "dumps": 0, "rollup": 0, "state_sessions": 0,
               "entity_citations": 0, "entities_dropped": 0, "memory_row_removed": 0}
    note = notes_dir(ws) / f"{slug}.md"
    if note.is_file():
        note.unlink()
        removed["note"] = 1
    for sub, key in ((SUMMARIES_DIR, "summaries"), (DUMPS_DIR, "dumps")):
        d = data_dir / sub / slug
        if d.is_dir():
            removed[key] = sum(1 for _ in d.rglob("*") if _.is_file())
            shutil.rmtree(d)
    rollup = data_dir / PROJECTS_DIR / f"{slug}.json"
    if rollup.is_file():
        rollup.unlink()
        removed["rollup"] = 1

    state = _common.load_state(data_dir)
    prefix = slug + "/"
    for key in [k for k in state["sessions"] if k.startswith(prefix)]:
        del state["sessions"][key]
        removed["state_sessions"] += 1
    state["projects"].pop(slug, None)
    _common.save_state(data_dir, state)

    entities_path = data_dir / ENTITIES_FILE
    if entities_path.is_file():
        entities = load_entities(data_dir)
        for k in ENTITY_LISTS:
            kept = []
            for item in entities[k]:
                if not isinstance(item, dict):
                    continue
                cites = [c for c in item.get("citations") or [] if isinstance(c, dict)]
                keep = [c for c in cites if _citation_project(c) != slug]
                removed["entity_citations"] += len(cites) - len(keep)
                if keep or not cites:
                    item["citations"] = keep
                    kept.append(item)
                else:
                    removed["entities_dropped"] += 1
            entities[k] = kept
        _common.write_json(entities_path, entities)

    index_doc = _common.load_json(data_dir / INDEX_FILE, {}) or {}
    rollups = load_rollups(data_dir)
    summaries = load_summaries(data_dir)
    n_sessions = index_doc.get("counts", {}).get("sessions") or len(summaries)
    ndir = notes_dir(ws)
    if rollups:
        if ndir.is_dir():
            (ndir / OVERVIEW).write_text(render_overview(rollups, index_doc, n_sessions, run_kind),
                                         encoding="utf-8")
        if memory_dir.is_dir():
            (memory_dir / MEMORY_FILE).write_text(
                render_memory_file(rollups, index_doc, n_sessions), encoding="utf-8")
    else:
        for p in (ndir / OVERVIEW, memory_dir / MEMORY_FILE):
            if p.is_file():
                p.unlink()
        removed["memory_row_removed"] = int(remove_memory_row(memory_dir))
    write_status(data_dir, "forgot", projects=len(rollups), summarized=len(summaries))
    return removed


def purge_dumps(data_dir: Path) -> int:
    d = data_dir / DUMPS_DIR
    if not d.is_dir():
        return 0
    n = sum(1 for p in d.rglob("*") if p.is_file())
    shutil.rmtree(d)
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", default=None, help="workspace root (default: sutando-config.sh workspace)")
    ap.add_argument("--data-dir", default=None, help="default <workspace>/data/claude-import")
    ap.add_argument("--memory-dir", default=None, help="default util_paths.memory_dir()")
    ap.add_argument("--run-kind", default="user", choices=["onboarding", "user"])
    ap.add_argument("--people-json", action="store_true")
    ap.add_argument("--forget", default=None, metavar="SLUG")
    ap.add_argument("--purge-dumps", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(_common.absorb_dash_values(argv, ("--forget",)))

    data_dir = _common.data_dir(a.workspace, a.data_dir)
    if a.people_json:
        index_doc = _common.load_json(data_dir / INDEX_FILE, {}) or {}
        print(json.dumps(people_payloads(load_entities(data_dir), index_doc),
                         ensure_ascii=False, indent=2))
        return 0
    if a.purge_dumps and not a.forget:
        n = purge_dumps(data_dir)
        print(json.dumps({"purged_files": n}) if a.json else f"purged {n} dump files")
        return 0

    ws = _common.workspace_root(a.workspace)
    memory_dir = resolve_memory_dir(a.memory_dir)
    if a.forget:
        r = forget(data_dir=data_dir, ws=ws, memory_dir=memory_dir, slug=a.forget, run_kind=a.run_kind)
        if a.purge_dumps:
            r["purged_files"] = purge_dumps(data_dir)
        print(json.dumps(r, sort_keys=True) if a.json else
              f"forgot {a.forget}: " + ", ".join(f"{k}={v}" for k, v in r.items()))
        return 0
    c = finalize(data_dir=data_dir, ws=ws, memory_dir=memory_dir, run_kind=a.run_kind)
    if a.json:
        print(json.dumps(c, sort_keys=True))
    else:
        print(f"finalized: {c['summarized']}/{c['sessions']} sessions summarised, {c['projects']} projects, "
              f"notes +{c['notes_created']}/~{c['notes_updated']}, {c['people']} people payloads, "
              f"memory {c['memory_bytes']} B, MEMORY.md row {'written' if c['memory_row'] else 'skipped'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
