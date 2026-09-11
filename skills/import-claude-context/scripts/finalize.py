#!/usr/bin/env python3
"""Turn the haiku summaries into Sutando's sinks — once the owner has reviewed
them — and undo them per project.

Reads, all under <data-dir> (= <workspace>/data/claude-import/):
  index.json                      session metadata (titles, dates, cwd) from index.py
  summaries/<slug>/<uuid>.json    one merged per-session summary (partials
                                  `<uuid>.<n>.json` are ignored)
  projects/<slug>.json            per-project roll-up incl. `note_markdown`
  entities.json                   people / companies / deals / decisions / open threads

Nothing may land in the agent's memory, notes or People store until the owner
has seen a digest of what was learned and said yes (owner, 2026-09-10), so the
finalize runs in two steps:

  --stage  (the default: an unguarded run cannot write into a sink)
    renders everything under <data-dir>/staged/ and touches no sink:
      staged/memory/claude_import.md            the <= 2,000-byte memory summary
      staged/notes/claude-import/<slug>.md      one note per project, + overview.md
      staged/people.json                        the <= 25 People upsert payloads
      staged/review.md                          the digest the owner reads: one
                                                paragraph per project, its open
                                                threads and decisions, the people
                                                it would add (name, why, citation
                                                counts), the memory size, and how
                                                to answer
      staged/manifest.json                      staged_at, run kind, pending slugs,
                                                a fingerprint of the inputs
      status.json                               phase `staged`, counts only

  --commit [--projects a,b]  (only on the owner's explicit yes)
    moves the staged set into the real sinks with the same guards as before:
      <memory-dir>/claude_import.md             <= 2,000 bytes: "Imported N sessions /
                                                M projects on <date>", one line per
                                                project (name, what it is, status, top
                                                open thread), pointer to notes/claude-import/
      <memory-dir>/MEMORY.md                    ONE row, only when
                                                memory-index-budget.py --adding <row> exits 0
                                                (refused -> row skipped, logged, file kept)
      <workspace>/notes/claude-import/<slug>.md one note per project; a re-run with a
                                                changed roll-up appends `## Update <date>`
      <workspace>/notes/claude-import/overview.md regenerated
      <data-dir>/state.json                     `summarized_at` per session, note hashes
                                                (a session extract.py marked
                                                `skipped_empty` has no dump and no
                                                summary: it is left out of every
                                                session count and never pending)
      <data-dir>/status.json                    phase `done` + counts (`staged` while a
                                                --projects remainder is still pending)
    Refused (exit 1) when nothing is staged, or when the summaries, roll-ups or
    entities changed since staging (stale: stage and review again). `--projects`
    takes an exact slug or a unique part of one, commits that subset and
    re-stages the rest.

  --discard [--projects a,b]
                    drop the staged set (or a subset); the sinks are untouched
  --people-json [--projects a,b] [--known-people FILE]
                    print the People upsert payloads (people with >= 2
                    citations; <= 25 NEW people) for the station's
                    people__upsert_person tool; writes nothing. Prints the
                    staged copy while one is pending, else the last commit's
                    approved copy (<data-dir>/people.json), else computes.
                    A payload is APPROVED only by --commit: the caller upserts
                    after the commit, and after `--commit --projects a` asks for
                    `--people-json --projects a` so only people cited in the
                    approved projects land.
  --people-doc-merge --existing-doc FILE --append FILE
                    print an existing dossier with the appended section merged
                    in: a section with the same `## ` heading is replaced,
                    otherwise the section is appended (idempotent)
  --held-json       print the held (personal) sessions: project, uuid, date,
                    reason — never a title or summary text
  --include <date|uuid> / --include-personal / --hold <date|uuid>
                    the owner's word over the classifier: re-admit one held
                    session (or all of them), or hold one the classifier missed;
                    recorded in state.json, a pending review is re-rendered
  --forget-session <date|uuid>
                    delete that session's summary, dumps and state entry
  --forget <slug>   remove exactly that project's note, summaries, dumps, roll-up,
                    memory line, state, entity citations and staged copy
  --purge-dumps     delete <data-dir>/dumps/ (alone, or after --stage / --forget)

People and companies are de-duplicated in memory first (merge_people /
merge_companies: shared email -> one person; a bare first name folds into its
unique fuller match; same company name case-insensitively), before the >= 2
citations / <= 25 selection and before review.md; the counts land in the JSON as
`people_merged` / `companies_merged`. entities.json itself is never rewritten here.

Existing people are never overwritten (a live run replaced a dossier, owner,
2026-09-10): `--stage --known-people FILE` takes the station's people__list_people
output (id, slug, name, email, identifiers.emails) and matches every staged
person by email first, then by normalised name (diacritics stripped). A match
yields an UPDATE payload — same slug/id, merged identifiers.emails,
`doc_append` (one `## Imported from Claude Code (<date>)` section) and NO
`doc`; the caller fetches the dossier, merges with --people-doc-merge and
upserts the merged text. A name shared by two store entries is `ambiguous`:
listed under "Needs your call" in review.md, never in people.json. Without
--known-people the manifest says `known_people: "not checked"` and every
payload is `existing: null`; the copy of the list taken at staging is reused
by every re-stage and by --commit.

Personal sessions are HELD: a summary with `personal: true` (or an owner
`--hold`) contributes nothing to memory, notes, entities or people; review.md
lists it by date and reason only, the manifest likewise, and `--commit` never
lands it. A roll-up written with a held session in it, or without an included
one, is STALE: staged with a placeholder body, refused by --commit until the
coordinator re-runs that project's roll-up.

The memory file and the overview are single files over every landed project,
so they are rebuilt from APPROVED SNAPSHOTS — <data-dir>/approved/<slug>.json,
written by --commit for each project it lands — never from projects/<slug>.json,
which the coordinator may rewrite between two commits (committing B used to land
A's unreviewed re-run: PR #4127 review). A landed project whose roll-up changed
since its approval is named in review.md as "changed since approval — bring in
<slug> to refresh" and keeps its approved text until it is brought in again.
The approved People export (<data-dir>/people.json) is written with the inputs
it came from (approved/people-inputs.json); --forget, --forget-session and
--hold shrink it to the citations still approved, and only a commit grows it —
and a commit grows it only with the projects it lands: their citations come
from the entities the owner just reviewed, every other landed project's from
the approved inputs, never from a later entities.json (an entities re-run
between two commits used to add citations nobody had seen: PR #4127 review).
people-inputs.json carries a per-project fingerprint of the people it approved
(`people_hashes`); a landed project whose live entities no longer match it is
named in review.md's People section as "changed since approval — bring in
<slug> to refresh", and `--people-json --projects <slug>` prints a landed
project's payloads from the approved inputs.

`--forget` takes a known slug (or a unique part of one) — never a path: `../x`,
`/abs`, `a/b`, `..`, `~`, an empty or an unknown value is refused — and every
path it would delete must resolve inside notes/claude-import/, data/claude-import/
or the memory dir (symlinks followed, and never a symlink itself), or nothing
is deleted.

The memory dir is util_paths.memory_dir() (the core's relocated tree); a memory
dir under the stock Claude home is refused unless passed explicitly — the import
never writes into Claude Code's own home. `--stage` only resolves it (so a bad
install fails before the owner is asked); nothing is written there until --commit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402
from _common import (  # noqa: E402
    DUMPS_DIR, ENTITIES_FILE, INDEX_FILE, INDEX_MD, PROJECTS_DIR, REPO, SUMMARIES_DIR,
    ensure_private_dir, now_iso, session_key, split_csv, today, write_status,
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

STAGED_DIR = "staged"
STAGED_MEMORY_SUBDIR = "memory"
STAGED_PEOPLE = "people.json"
STAGED_REVIEW = "review.md"
STAGED_MANIFEST = "manifest.json"
STAGED_KNOWN_PEOPLE = "known-people.json"     # the store listing as it was at staging
APPROVED_PEOPLE = "people.json"               # <data-dir>/people.json: the last commit's payloads
APPROVED_DIR = "approved"
APPROVED_PEOPLE_INPUTS = "people-inputs.json"
CHANGED_SINCE_APPROVAL = "changed since approval"
IMPORT_SECTION_HEADING = "## Imported from Claude Code"
PERSONAL_OVERRIDE = "personal_override"       # state.json per session: "include" | "hold"
HELD_REASON_WORDS = 6
HELD_BY_OWNER = "held by you"
STALE_ROLLUP_TEXT = ("(roll-up pending regeneration — a session was held, included or forgotten "
                     "after it was written; nothing from it is shown until it is re-run)")
REVIEW_MAX_ITEMS = 12
REVIEW_FOOTER = ("Reply 'bring it in' to save this to your Sutando, 'bring in <slug>' "
                 "for one project, or 'forget <slug>' to drop one.")


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


def staged_dir(data_dir: Path) -> Path:
    return data_dir / STAGED_DIR


def staged_notes_dir(data_dir: Path) -> Path:
    return staged_dir(data_dir).joinpath(*NOTES_SUBDIR)


def staged_memory_dir(data_dir: Path) -> Path:
    return staged_dir(data_dir) / STAGED_MEMORY_SUBDIR


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


def _entity_lists(doc) -> dict:
    """An entities document with every list present (junk shapes emptied)."""
    doc = dict(doc) if isinstance(doc, dict) else {}
    for k in ENTITY_LISTS:
        v = doc.get(k)
        doc[k] = v if isinstance(v, list) else []
    return doc


def load_entities(data_dir: Path) -> dict:
    return _entity_lists(_common.load_json(data_dir / ENTITIES_FILE, {}))


def load_inputs(data_dir: Path) -> tuple:
    """(index_doc, summaries, rollups, entities, state) — everything finalize reads."""
    index_doc = _common.load_json(data_dir / INDEX_FILE, {}) or {}
    return (index_doc, load_summaries(data_dir), load_rollups(data_dir),
            load_entities(data_dir), _common.load_state(data_dir))


def input_files(data_dir: Path) -> list:
    files = [data_dir / INDEX_FILE, data_dir / ENTITIES_FILE]
    base = data_dir / SUMMARIES_DIR
    if base.is_dir():
        for slug_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            files += [f for f in sorted(slug_dir.glob("*.json")) if f.name.count(".") == 1]
    pdir = data_dir / PROJECTS_DIR
    if pdir.is_dir():
        files += sorted(pdir.glob("*.json"))
    return files


def inputs_fingerprint(data_dir: Path) -> str:
    """sha256 over the inputs finalize reads (index, merged summaries, roll-ups,
    entities). A staged set whose fingerprint no longer matches was reviewed
    against summaries that have since changed: it is stale, whatever the clocks say."""
    h = hashlib.sha256()
    for f in input_files(data_dir):
        try:
            data = f.read_bytes()
        except OSError:
            continue
        h.update(str(f.relative_to(data_dir)).encode("utf-8") + b"\0")
        h.update(hashlib.sha256(data).digest())
    return h.hexdigest()


def rollup_hash(rollup: dict) -> str:
    return hashlib.sha256(_common.dump_json(rollup).encode("utf-8")).hexdigest()[:16]


def approved_dir(data_dir: Path) -> Path:
    return data_dir / APPROVED_DIR


def approved_path(data_dir: Path, slug: str) -> Path:
    return approved_dir(data_dir) / f"{slug}.json"


def save_approved(data_dir: Path, slug: str, rollup: dict) -> str:
    """Snapshot a roll-up exactly as the owner approved it (what --commit lands).
    The shared memory file and overview are rebuilt from these snapshots, never
    from projects/<slug>.json, which the coordinator may rewrite unreviewed."""
    ensure_private_dir(approved_dir(data_dir))
    h = rollup_hash(rollup)
    _common.write_json(approved_path(data_dir, slug),
                       {"slug": slug, "approved_at": now_iso(), "rollup_hash": h, "rollup": rollup}, private=True)
    return h


def load_approved(data_dir: Path) -> dict:
    """{slug: snapshot document} for every readable approved roll-up on disk."""
    out = {}
    base = approved_dir(data_dir)
    if not base.is_dir():
        return out
    for f in sorted(base.glob("*.json")):
        if f.name == APPROVED_PEOPLE_INPUTS:
            continue
        doc = _common.load_json(f, None)
        if isinstance(doc, dict) and isinstance(doc.get("rollup"), dict):
            out[f.stem] = doc
    return out


def approved_rollups(data_dir: Path, state: dict) -> dict:
    """The roll-ups whose note has landed (state.projects records each commit),
    each as the owner approved it — the snapshot, not the file under projects/."""
    snaps = load_approved(data_dir)
    return {s: snaps[s]["rollup"] for s in state["projects"] if s in snaps}


def changed_since_approval(data_dir: Path, rollups: dict, state: dict) -> list:
    """The landed projects whose roll-up on disk is not the approved one any
    more (or that have no readable snapshot): the shared files keep the
    approved text until the owner brings the project in again."""
    snaps = load_approved(data_dir)
    return sorted(s for s in state["projects"] if s in rollups
                  and (snaps.get(s) or {}).get("rollup_hash") != rollup_hash(rollups[s]))


def session_meta(index_doc: dict, slug: str, uuid: str) -> dict:
    for s in ((index_doc.get("projects") or {}).get(slug) or {}).get("sessions") or []:
        if s.get("uuid") == uuid:
            return s
    return {}


def project_meta(index_doc: dict, slug: str) -> dict:
    return (index_doc.get("projects") or {}).get(slug) or {}


def skipped_empty_by_slug(state: dict) -> dict:
    """{slug: n} of the sessions extract.py skipped as empty (no dump, no summary)."""
    out = {}
    for key, rec in (state.get("sessions") or {}).items():
        if isinstance(rec, dict) and rec.get("skipped_empty"):
            slug = key.rsplit("/", 1)[0]
            out[slug] = out.get(slug, 0) + 1
    return out


def _override(state: dict, slug: str, uuid: str):
    rec = (state.get("sessions") or {}).get(session_key(slug, uuid))
    return rec.get(PERSONAL_OVERRIDE) if isinstance(rec, dict) else None


def held_reason(summary: dict) -> str:
    """The classifier's category, capped at HELD_REASON_WORDS words — never a quote."""
    words = _one_line(summary.get("personal_reason"), 120).split()
    return " ".join(words[:HELD_REASON_WORDS]) or "personal"


def held_sessions(summaries: dict, state: dict) -> dict:
    """{(slug, uuid): reason} — the sessions the import must not land: flagged
    `personal: true` by the summariser unless the owner said `--include`, plus
    the ones the owner put on `--hold`."""
    out = {}
    for key, doc in summaries.items():
        ov = _override(state, *key)
        if ov == "include":
            continue
        if ov == "hold":
            out[key] = HELD_BY_OWNER
        elif doc.get("personal") is True:
            out[key] = held_reason(doc)
    return out


def held_by_slug(held) -> dict:
    out = {}
    for (slug, _u) in held or {}:
        out[slug] = out.get(slug, 0) + 1
    return out


def excluded_by_slug(state, held) -> dict:
    """{slug: n} of the sessions the owner is not promised: skipped as empty, or held."""
    out = skipped_empty_by_slug(state or {})
    for slug, n in held_by_slug(held).items():
        out[slug] = out.get(slug, 0) + n
    return out


def session_day(index_doc: dict, summaries: dict, slug: str, uuid: str) -> str:
    """The day a session started: the index's first_ts, else the summary's date range."""
    day = _day(session_meta(index_doc, slug, uuid).get("first_ts"))
    if day == "?":
        rng = (summaries.get((slug, uuid)) or {}).get("date_range")
        if isinstance(rng, list) and rng:
            day = _day(rng[0])
    return day


def session_total(index_doc: dict, summaries: dict, slugs, all_slugs, state=None, held=None) -> int:
    """Sessions behind `slugs`: the index total once every project is in, else the
    per-project counts (falling back to the summaries on disk) — minus the
    sessions extract.py skipped as empty or the import holds as personal,
    which the owner was never promised."""
    excluded = excluded_by_slug(state, held)
    if set(all_slugs) <= set(slugs):
        indexed = int(index_doc.get("counts", {}).get("sessions") or 0)
        if indexed:
            return max(len(summaries), indexed - sum(excluded.values()))
        return len(summaries)
    total = 0
    for slug in slugs:
        n_sum = sum(1 for (s, _u) in summaries if s == slug)
        indexed = int(project_meta(index_doc, slug).get("session_count") or 0)
        total += max(n_sum, indexed - excluded.get(slug, 0)) if indexed else n_sum
    return total


def project_sessions(index_doc: dict, slug: str, state=None, held=None) -> int:
    """One project's session count as the owner should read it: the index count
    minus the sessions skipped as empty or held as personal."""
    n = int(project_meta(index_doc, slug).get("session_count") or 0)
    return max(0, n - excluded_by_slug(state, held).get(slug, 0))


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


def resolve_selectors(available, selectors, what: str, where: str = "staged/review.md") -> list:
    """`--projects` for the gate: each selector is an exact slug or a unique
    case-insensitive part of one. No match or an ambiguous one refuses — a
    commit never covers more than the owner named. No selectors = everything."""
    available = list(available)
    if not selectors:
        return available
    chosen = []
    for sel in selectors:
        hits = [sel] if sel in available else [s for s in available if sel.lower() in s.lower()]
        if not hits:
            raise SystemExit(f"import-claude-context: no {what} matches {sel!r} "
                             f"({len(available)} available; the slugs are in {where})")
        if len(hits) > 1:
            raise SystemExit(f"import-claude-context: {sel!r} matches {len(hits)} {what}s; "
                             f"name one exactly (the slugs are in {where})")
        if hits[0] not in chosen:
            chosen.append(hits[0])
    return chosen


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


def _sessions_list(slug: str, rollup: dict, index_doc: dict, summaries: dict, held=None) -> str:
    uuids = rollup.get("sessions")
    if not isinstance(uuids, list) or not uuids:
        uuids = [u for (s, u) in summaries if s == slug]
    rows = []
    for u in uuids:
        if not isinstance(u, str) or (slug, u) in (held or {}):
            continue
        meta = session_meta(index_doc, slug, u)
        title = meta.get("title") or (summaries.get((slug, u)) or {}).get("title") or "(untitled)"
        rows.append(f"- {_day(meta.get('first_ts'))} — {_one_line(title, 100)} ({u[:8]})")
    return "\n".join(rows)


def _note_hash(rollup: dict) -> str:
    return hashlib.sha256((rollup.get("note_markdown") or "").encode("utf-8")).hexdigest()[:16]


def _note_body(rollup: dict) -> str:
    if rollup.get("stale_rollup"):
        return f"_{STALE_ROLLUP_TEXT}_"
    return (rollup.get("note_markdown") or "").strip() or "_(no roll-up text)_"


def render_note(slug: str, rollup: dict, index_doc: dict, summaries: dict, run_kind: str, held=None) -> str:
    """The full text of a NEW notes/claude-import/<slug>.md."""
    name = display_name(slug, rollup, index_doc)
    cwd = project_meta(index_doc, slug).get("cwd")
    text = _frontmatter(f"Claude Code history — {name}")
    text += header_line(index_doc, [slug], run_kind) + "\n\n"
    text += f"# {name}\n\n"
    if cwd:
        text += f"Project dir: `{cwd}` (Claude Code slug `{slug}`)\n\n"
    text += _note_body(rollup) + "\n"
    sessions = _sessions_list(slug, rollup, index_doc, summaries, held)
    if sessions:
        text += f"\n## Sessions\n{sessions}\n"
    return text


def render_update_block(slug: str, rollup: dict, index_doc: dict, summaries: dict, held=None) -> str:
    """What a re-run with a changed roll-up appends to an existing note."""
    block = f"\n\n## Update {today()}\n\n{_note_body(rollup)}\n"
    sessions = _sessions_list(slug, rollup, index_doc, summaries, held)
    if sessions:
        block += f"\n### Sessions\n{sessions}\n"
    return block


def note_outcome(ndir: Path, slug: str, rollup: dict, state: dict) -> str:
    """What a commit does to notes/claude-import/<slug>.md: created | updated | unchanged."""
    if not (ndir / f"{slug}.md").is_file():
        return "created"
    prev = state["projects"].get(slug) or {}
    return "unchanged" if prev.get("note_hash") == _note_hash(rollup) else "updated"


def write_project_note(ndir: Path, slug: str, rollup: dict, index_doc: dict, summaries: dict,
                       state: dict, run_kind: str, staged_note=None, held=None) -> str:
    """Returns 'created' | 'updated' | 'unchanged'. A new note is the staged file
    moved into place when one is given (what the owner reviewed lands verbatim)."""
    path = ndir / f"{slug}.md"
    result = note_outcome(ndir, slug, rollup, state)
    if result == "unchanged":
        return result
    if result == "updated":
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(render_update_block(slug, rollup, index_doc, summaries, held))
    elif staged_note is not None and Path(staged_note).is_file():
        shutil.move(str(staged_note), str(path))
    else:
        path.write_text(render_note(slug, rollup, index_doc, summaries, run_kind, held), encoding="utf-8")
    state["projects"][slug] = {"note_hash": _note_hash(rollup), "note_written_at": now_iso()}
    return result


def render_overview(rollups: dict, index_doc: dict, n_sessions: int, run_kind: str, state=None, held=None) -> str:
    slugs = sorted(rollups, key=lambda s: (-project_sessions(index_doc, s, state, held), s))
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
        line += f" · {project_sessions(index_doc, slug, state, held)} sessions ({_day(p.get('first_ts'))} → {_day(p.get('last_ts'))})"
        if thread:
            line += f" · open: {thread}"
        text += line + "\n"
    return text


# ------------------------------------------------------------------ entity merge

# An email inside a display name ("Cyrus (cyrus@x.com)", "Cyrus <cyrus@x.com>") is an
# identifier, not a name: `_norm_name` drops it and `_emails_of` keeps it for the email merge.
_NAME_EMAIL_RE = re.compile(r"\s*[\(<\[]\s*[^\s()<>\[\]]+@[^\s()<>\[\]]+\s*[\)>\]]")


def _name_embedded_emails(value) -> list:
    return [m.strip("()<>[] ") for m in _NAME_EMAIL_RE.findall(value)] if isinstance(value, str) else []


def _norm_name(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(_NAME_EMAIL_RE.sub("", value).split())


def _emails_of(p: dict) -> list:
    """Every email an entry carries (`email`, `emails`, `identifiers.emails`),
    first-seen casing, case-insensitively unique, in order."""
    ids = p.get("identifiers") if isinstance(p.get("identifiers"), dict) else {}
    raw = [p.get("email")]
    raw += list(p.get("emails") or []) if isinstance(p.get("emails"), list) else []
    raw += list(ids.get("emails") or []) if isinstance(ids.get("emails"), list) else []
    raw += _name_embedded_emails(p.get("name"))
    out, seen = [], set()
    for v in raw:
        if isinstance(v, str) and "@" in v and v.strip() and v.strip().lower() not in seen:
            seen.add(v.strip().lower())
            out.append(v.strip())
    return out


def _union_citations(entries) -> list:
    out, seen = [], set()
    for e in entries:
        for c in e.get("citations") or []:
            if not isinstance(c, dict):
                continue
            key = (c.get("project"), c.get("session"), c.get("quote_or_context") or c.get("context"))
            if key not in seen:
                seen.add(key)
                out.append(c)
    return out


def _clean_entry(p: dict) -> dict:
    """A copy of a singleton entry with the display name cleaned and any email
    the name carried moved into `emails`, so the People payload never shows
    "Cyrus (cyrus@x.com)" as a name and the address is not lost."""
    out = dict(p)
    found = _name_embedded_emails(p.get("name"))
    if found:
        out["name"] = _norm_name(p.get("name"))
        have = {e.lower() for e in _emails_of(p)}
        extra = [e for e in found if e.lower() in have]  # already known via _emails_of
        existing = list(p.get("emails") or []) if isinstance(p.get("emails"), list) else []
        out["emails"] = existing + [e for e in extra if e.lower() not in {x.lower() for x in existing}]
    return out


def _merge_group(entries) -> dict:
    """One person out of several entries: the longest name (first on a tie),
    the most specific (longest) role, the base's other fields or the first
    non-empty one, emails / identifiers / citations unioned in input order."""
    base = max(entries, key=lambda p: len(_norm_name(p.get("name"))))
    out = dict(base)
    out["name"] = _norm_name(base.get("name"))
    roles = [p["role"].strip() for p in entries if isinstance(p.get("role"), str) and p["role"].strip()]
    if roles:
        out["role"] = max(roles, key=len)
    for key in ("company", "relationship"):
        if not (isinstance(out.get(key), str) and out[key].strip()):
            for p in entries:
                if isinstance(p.get(key), str) and p[key].strip():
                    out[key] = p[key]
                    break
    emails, seen = [], set()
    for p in entries:
        for e in _emails_of(p):
            if e.lower() not in seen:
                seen.add(e.lower())
                emails.append(e)
    if emails:
        own = _emails_of(base)
        out["email"] = own[0] if own else emails[0]
        if len(emails) > 1 or any(isinstance(p.get("emails"), list) for p in entries):
            out["emails"] = emails
    ids = {}
    for p in entries:
        d = p.get("identifiers")
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            if isinstance(v, list):
                merged = list(ids.get(k) or [])
                merged += [x for x in v if x not in merged]
                ids[k] = merged
            elif v not in (None, "") and ids.get(k) in (None, ""):
                ids[k] = v
    if ids:
        if emails:
            ids["emails"] = emails
        out["identifiers"] = ids
    out["citations"] = _union_citations(entries)
    return out


def _email_groups(entries) -> list:
    """Indices grouped by shared email (transitively), each group in input order,
    groups in order of their first member."""
    parent = list(range(len(entries)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first = {}
    for i, p in enumerate(entries):
        for e in _emails_of(p):
            k = e.lower()
            if k in first:
                ri, rj = find(first[k]), find(i)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
            else:
                first[k] = i
    groups = {}
    for i in range(len(entries)):
        groups.setdefault(find(i), []).append(i)
    return [groups[r] for r in sorted(groups)]


def merge_people(people) -> tuple:
    """Deterministic de-duplication of the entities pass (people[]) — returns
    (merged list, number of entries absorbed). Pure: the input is not modified.

    1. Entries sharing a non-empty email are one person: longest name, union of
       emails / identifiers / citations, the more specific role.
    2. A single-token name ("Chi") merges into the UNIQUE other entry whose name
       starts with that token and a space ("Chi Wang") when their emails do not
       conflict: the fuller name is kept, citations unioned. Two or more fuller
       matches ("John" vs "John Smith" + "John Doe") leave everything untouched.
    3. Two multi-token names are never merged by name."""
    entries = [p for p in people or [] if isinstance(p, dict)]
    step1 = [_merge_group([entries[i] for i in g]) if len(g) > 1 else _clean_entry(entries[g[0]])
             for g in _email_groups(entries)]
    names = [_norm_name(p.get("name")) for p in step1]
    result = list(step1)
    absorbed = set()
    for i, p in enumerate(step1):
        name = names[i]
        if not name or " " in name:
            continue
        token = name.lower() + " "
        hits = [j for j, other in enumerate(names) if j != i and other.lower().startswith(token)]
        if len(hits) != 1:
            continue
        j = hits[0]
        mine = {e.lower() for e in _emails_of(p)}
        theirs = {e.lower() for e in _emails_of(result[j])}
        if mine and theirs and mine.isdisjoint(theirs):
            continue  # two different addresses: not provably the same person
        result[j] = _merge_group([result[j], p])
        absorbed.add(i)
    merged = [p for i, p in enumerate(result) if i not in absorbed]
    return merged, len(entries) - len(merged)


def merge_companies(companies) -> tuple:
    """Companies with the same name (case-insensitive) are one: first spelling
    kept, first non-empty `what` / `relationship`, citations unioned."""
    out, index = [], {}
    n_in = 0
    for c in companies or []:
        if not isinstance(c, dict):
            continue
        n_in += 1
        key = _norm_name(c.get("name")).lower()
        if key and key in index:
            base = out[index[key]]
            merged = dict(base)
            for k in ("what", "relationship"):
                if not (isinstance(merged.get(k), str) and merged[k].strip()) \
                        and isinstance(c.get(k), str) and c[k].strip():
                    merged[k] = c[k]
            merged["citations"] = _union_citations([base, c])
            out[index[key]] = merged
            continue
        if key:
            index[key] = len(out)
        out.append(dict(c))
    return out, n_in - len(out)


def merge_entities(entities: dict) -> tuple:
    """(a copy of entities with people and companies de-duplicated, merge counts).
    Applied in memory before the People selection and the review digest — never
    written back, so entities.json (and the staged fingerprint) stay as the
    entities pass left them."""
    people, n_people = merge_people(entities.get("people"))
    companies, n_companies = merge_companies(entities.get("companies"))
    out = dict(entities)
    out["people"], out["companies"] = people, companies
    return out, {"people_merged": n_people, "companies_merged": n_companies}


def strip_citations(entities: dict, drop) -> tuple:
    """(a copy of entities without the citations `drop(citation)` selects,
    citations removed, entries dropped). An entry that loses its last citation
    goes with it — an uncited entity has no evidence left; junk entries go too."""
    out, n_cites, n_dropped = dict(entities), 0, 0
    for k in ENTITY_LISTS:
        kept = []
        for item in entities.get(k) or []:
            if not isinstance(item, dict):
                continue
            cites = [c for c in item.get("citations") or [] if isinstance(c, dict)]
            keep = [c for c in cites if not drop(c)]
            n_cites += len(cites) - len(keep)
            if keep or not cites:
                item = dict(item)
                item["citations"] = keep
                kept.append(item)
            else:
                n_dropped += 1
        out[k] = kept
    return out, n_cites, n_dropped


def drop_held_citations(entities: dict, held) -> dict:
    """Entities as if the held sessions had never been summarised."""
    if not held:
        return entities
    return strip_citations(entities, lambda c: (_citation_project(c), _citation_session(c)) in held)[0]


# ------------------------------------------------------------------ stale roll-ups

def stale_rollups(rollups: dict, summaries: dict, held, state: dict, index_doc: dict) -> dict:
    """{slug: why} for the roll-ups that no longer match their sessions: one
    written with a held session in it (its narrative may carry that session),
    one written without a session the owner has since included, or one listing
    a session whose summary was forgotten. Judged on the `sessions` list the
    roll-up itself wrote; a new session the roll-up never saw is not stale."""
    out = {}
    skipped = {k for k, r in (state.get("sessions") or {}).items() if isinstance(r, dict) and r.get("skipped_empty")}
    for slug, r in rollups.items():
        listed = {u for u in (r.get("sessions") or []) if isinstance(u, str)} if isinstance(r.get("sessions"), list) else set()
        have = {u for (s, u) in summaries if s == slug}
        held_here = {u for (s, u) in (held or {}) if s == slug}
        included = {u for u in have - held_here if _override(state, slug, u) == "include"}
        indexed = {s.get("uuid") for s in project_meta(index_doc, slug).get("sessions") or [] if isinstance(s, dict)}
        gone = {u for u in (listed & indexed) - have if session_key(slug, u) not in skipped}
        if listed & held_here:
            out[slug] = "written with a held session"
        elif included - listed:
            out[slug] = "written without an included session"
        elif gone:
            out[slug] = "lists a forgotten session"
    return out


def redact_rollup(rollup: dict) -> dict:
    """A stale roll-up as the renderers may see it: name, dir, status and the
    session list survive; every sentence the model wrote is withheld."""
    return {"project": rollup.get("project"), "name": rollup.get("name"), "cwd": rollup.get("cwd"),
            "status": rollup.get("status"), "sessions": rollup.get("sessions"),
            "what_it_is": "(roll-up pending regeneration)", "note_markdown": "", "stale_rollup": True}


def apply_stale(rollups: dict, stale) -> dict:
    return {s: (redact_rollup(r) if s in stale else r) for s, r in rollups.items()}


# ------------------------------------------------------------------------ people

def _citation_project(c) -> str:
    return c.get("project") if isinstance(c, dict) else ""


def _citation_session(c) -> str:
    return c.get("session") if isinstance(c, dict) else ""


def _person_citations(p: dict, projects=None) -> list:
    cites = [c for c in p.get("citations") or [] if isinstance(c, dict)]
    if projects is not None:
        cites = [c for c in cites if _citation_project(c) in projects]
    return cites


def store_name_key(value) -> str:
    """How two spellings of a name compare against the People store: the email
    stripped, diacritics folded (NFKD), casefolded, whitespace collapsed."""
    text = unicodedata.normalize("NFKD", _norm_name(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(text.casefold().split())


def load_known_people(source) -> list:
    """The store listing for --known-people: a path to a JSON array, or the
    array itself. Only dict entries count; anything else is a usage error."""
    if source is None:
        return None
    doc = source
    if isinstance(source, (str, Path)):
        path = Path(source).expanduser()
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise SystemExit(f"import-claude-context: cannot read --known-people {path}: {e}")
    if isinstance(doc, dict):
        doc = doc.get("people") if isinstance(doc.get("people"), list) else [doc]
    if not isinstance(doc, list):
        raise SystemExit("import-claude-context: --known-people must be a JSON array of people")
    return [k for k in doc if isinstance(k, dict)]


def _known_ref(k: dict) -> dict:
    return {"id": k.get("id"), "slug": k.get("slug"), "name": _one_line(k.get("name"), 80)}


def match_known(p: dict, known) -> tuple:
    """(existing, ambiguous): the store entry a staged person IS — by any email
    first, then by normalised name — or the entries that share their name."""
    if not known:
        return None, []
    mine = {e.lower() for e in _emails_of(p)}
    if mine:
        for k in known:
            if mine & {e.lower() for e in _emails_of(k)}:
                return dict(_known_ref(k), matched_on="email", _store=k), []
    key = store_name_key(p.get("name"))
    hits = [k for k in known if key and store_name_key(k.get("name")) == key]
    if len(hits) == 1:
        return dict(_known_ref(hits[0]), matched_on="name", _store=hits[0]), []
    return None, [_known_ref(k) for k in hits]


def people_candidates(entities: dict, cap: int = PEOPLE_CAP, min_citations: int = PEOPLE_MIN_CITATIONS,
                      projects=None, known=None) -> tuple:
    """([(person, citations, existing)], below_floor, [(person, citations, matches)]).
    The named people with >= min_citations citations, most-cited first: every
    one the store already has (`existing`), then at most `cap` NEW ones; a
    person whose name two store entries share is returned separately as
    ambiguous and never as a candidate. `projects` restricts the citations that
    count, and that a payload may quote, to those slugs: people are approved
    only through the projects they were seen in."""
    people = [p for p in entities.get("people") or [] if isinstance(p, dict) and p.get("name")]
    scored = [(p, _person_citations(p, projects)) for p in people]
    kept = [(p, c) for p, c in scored if len(c) >= min_citations]
    kept.sort(key=lambda t: (-len(t[1]), str(t[0].get("name"))))
    existing, new, ambiguous = [], [], []
    for p, c in kept:
        match, hits = match_known(p, known)
        if match:
            existing.append((p, c, match))
        elif hits:
            ambiguous.append((p, c, hits))
        else:
            new.append((p, c, None))
    return existing + new[:cap], len(scored) - len(kept), ambiguous


def _dated_citations(cites, index_doc: dict) -> list:
    dated = []
    for c in cites:
        meta = session_meta(index_doc, _citation_project(c) or "", _citation_session(c) or "")
        dated.append((meta.get("last_ts") or "", meta.get("title") or "", c))
    dated.sort(key=lambda t: t[0], reverse=True)
    return dated


def _interaction_lines(dated) -> list:
    return [f"- {_day(ts)} — {_one_line(title, 60) or 'session'}: "
            f"{_one_line(c.get('quote_or_context') or c.get('context'), 160)}" for ts, title, c in dated[:8]]


def _citation_lines(dated) -> list:
    out = []
    for ts, title, c in dated:
        sess = (_citation_session(c) or "")[:8]
        out.append(f"- \"{_one_line(c.get('quote_or_context') or c.get('context'), 160)}\" — "
                   f"Claude Code session {sess or '?'} ({_one_line(_citation_project(c), 60) or '?'}), {_day(ts)}")
    return out


def render_person_doc(p: dict, dated) -> str:
    """The full dossier of a NEW person (Sutando's preferred section order)."""
    name = _one_line(p["name"], 80)
    emails = _emails_of(p)
    company = _one_line(p.get("company"), 80)
    role = _one_line(p.get("role"), 80)
    doc = [f"# {name}", "", "## Contact",
           f"- Email: {emails[0] if emails else 'unknown'}",
           f"- Company: {company or 'unknown'} · Role: {role or 'unknown'}",
           "", "## Who they are",
           " · ".join(x for x in (role, company) if x) or "(from Claude Code sessions only)",
           "", "## Your relationship", _one_line(p.get("relationship"), 200) or "(not stated)",
           "", "## Recent interactions", *_interaction_lines(dated),
           "", "## Claims and citations", *_citation_lines(dated),
           "", "_Imported by import-claude-context from the owner's Claude Code history._"]
    return "\n".join(doc) + "\n"


def render_import_section(p: dict, dated) -> str:
    """What an EXISTING person's dossier gains: one dated section, replaceable
    by heading, so a re-import of the same day never stacks up."""
    lines = [f"{IMPORT_SECTION_HEADING} ({today()})", ""]
    context = " · ".join(x for x in (_one_line(p.get("role"), 80), _one_line(p.get("company"), 80)) if x)
    relationship = _one_line(p.get("relationship"), 200)
    if context or relationship:
        lines += ["; ".join(x for x in (relationship, context) if x), ""]
    lines += ["Recent interactions:", *_interaction_lines(dated), "", "Claims and citations:", *_citation_lines(dated)]
    return "\n".join(lines) + "\n"


def people_doc_merge(existing_doc: str, section: str) -> str:
    """The dossier with `section` merged in: a `## ` section under the same
    heading is replaced in place, else the section is appended. Idempotent."""
    heading = next((ln.strip() for ln in section.splitlines() if ln.startswith("## ")), None)
    lines = existing_doc.splitlines()
    body = section.rstrip("\n").splitlines()
    if heading:
        for i, ln in enumerate(lines):
            if ln.strip() != heading:
                continue
            j = i + 1
            while j < len(lines) and not lines[j].startswith("#"):
                j += 1
            lines[i:j] = body + ([""] if j < len(lines) else [])
            return "\n".join(lines).rstrip("\n") + "\n"
    base = existing_doc.rstrip("\n")
    return (base + "\n\n" if base else "") + "\n".join(body) + "\n"


def people_payloads(entities: dict, index_doc: dict, cap: int = PEOPLE_CAP,
                    min_citations: int = PEOPLE_MIN_CITATIONS, projects=None, known=None) -> list:
    """The people__upsert_person payloads. A person the store has gets an UPDATE
    — the store's slug/id, merged `identifiers.emails`, `doc_append`, no `doc`
    (the caller merges with --people-doc-merge); a new person gets the dossier."""
    payloads = []
    for p, cites, existing in people_candidates(entities, cap, min_citations, projects, known)[0]:
        dated = _dated_citations(cites, index_doc)
        emails = _emails_of(p)
        payload = {
            "name": _one_line(p["name"], 80),
            "source": "claude-import",
            "last_interaction_at": dated[0][0] if dated and dated[0][0] else None,
            "existing": {k: v for k, v in existing.items() if k != "_store"} if existing else None,
        }
        if existing:
            store = existing["_store"]
            for key in ("id", "slug"):
                if store.get(key):
                    payload[key] = store[key]
            merged, seen = [], set()
            for e in _emails_of(store) + emails:
                if e.lower() not in seen:
                    seen.add(e.lower())
                    merged.append(e)
            if merged:
                payload["email"] = merged[0]
                payload["identifiers"] = {"emails": merged}
            payload["doc_append"] = render_import_section(p, dated)
        else:
            payload["doc"] = render_person_doc(p, dated)
            if emails:
                payload["email"] = emails[0]
                payload["identifiers"] = {"emails": emails}
        payloads.append(payload)
    return payloads


def load_people_inputs(data_dir: Path) -> dict:
    """approved/people-inputs.json — the entities (approved citations only), the
    store listing and the per-project `people_hashes` the export was computed
    from; {} when there is none or it is not a document."""
    doc = _common.load_json(approved_dir(data_dir) / APPROVED_PEOPLE_INPUTS, None)
    return doc if isinstance(doc, dict) else {}


def project_people_hash(entities: dict, slug: str) -> str:
    """What the digest shows of one project's people, as a fingerprint: every
    person with a citation in `slug` — name, relationship, role, company — with
    that project's citations (session and text), sorted. No email (an
    identifier, not reviewed text) and no other project's citation, so the
    value survives the approved-inputs union and changes only when the
    entities pass changed something the owner has not seen for that project."""
    rows = []
    for p in entities.get("people") or []:
        if not isinstance(p, dict):
            continue
        cites = sorted((_one_line(_citation_session(c), 80),
                        _one_line(c.get("quote_or_context") or c.get("context"), 400))
                       for c in _person_citations(p, {slug}))
        if cites:
            rows.append([store_name_key(p.get("name")),
                         *(_one_line(p.get(k), 200) for k in ("relationship", "role", "company")), cites])
    rows.sort()
    return hashlib.sha256(_common.dump_json(rows).encode("utf-8")).hexdigest()[:16]


def _same_person(a: dict, b: dict) -> bool:
    """merge_people's rule across two passes: a shared email is the same person,
    two addresses that never meet are not, and otherwise the same normalised
    name is — the whole name this time: both passes describe the same people,
    so an identical name is the same person, not a coincidence."""
    ea, eb = {e.lower() for e in _emails_of(a)}, {e.lower() for e in _emails_of(b)}
    if ea & eb:
        return True
    if ea and eb:
        return False
    key = store_name_key(a.get("name"))
    return bool(key) and key == store_name_key(b.get("name"))


def _same_entity(kind: str, a: dict, b: dict) -> bool:
    if kind == "people":
        return _same_person(a, b)
    if kind == "companies":
        key = store_name_key(a.get("name"))
        return bool(key) and key == store_name_key(b.get("name"))
    return {k: v for k, v in a.items() if k != "citations"} == {k: v for k, v in b.items() if k != "citations"}


def _absorb(base: dict, other: dict) -> dict:
    """`base` exactly as the owner reviewed it — every field it has, and none it
    lacks — plus the emails, identifiers and citations `other` carries."""
    merged = _merge_group([base, other])
    ids = ("email", "emails", "identifiers", "citations")
    out = {k: v for k, v in base.items() if k not in ids}
    out.update({k: merged[k] for k in ids if k in merged})
    out["name"] = _norm_name(base.get("name"))
    own = _emails_of(base)
    if own:
        out["email"] = own[0]
    return out


def union_entities(primary: dict, secondary: dict) -> dict:
    """`primary`'s entries as they are, each gaining the citations (a person:
    the emails and identifiers too) of the `secondary` entry that is the same
    person (_same_person), company (name) or item (every other field equal);
    a secondary entry with no counterpart is appended as it is."""
    out = {k: v for k, v in primary.items() if k not in ENTITY_LISTS}
    for kind in ENTITY_LISTS:
        items = [dict(p) for p in primary.get(kind) or [] if isinstance(p, dict)]
        for s in secondary.get(kind) or []:
            if not isinstance(s, dict):
                continue
            i = next((i for i, p in enumerate(items) if _same_entity(kind, p, s)), None)
            if i is None:
                items.append(dict(s))
            elif kind == "people":
                items[i] = _absorb(items[i], s)
            else:
                items[i] = dict(items[i], citations=_union_citations([items[i], s]))
        out[kind] = items
    return out


def approved_entities_after(data_dir: Path, entities: dict, chosen, landed) -> dict:
    """The entities the People export is computed from once `chosen` lands: the
    citations of the projects being committed now, from the live pass the owner
    just reviewed, plus — for every other landed project — the citations as
    they were approved (people-inputs.json), never the live ones: an entities
    re-run between two commits must not add to an approved project's payload
    without a digest showing it (PR #4127 review). A person in both keeps the
    fields reviewed now and gains the approved citations; a landed project whose
    approved inputs are gone contributes nothing until it is brought in again."""
    chosen, keep = set(chosen), set(landed) - set(chosen)
    live = strip_citations(entities, lambda c: _citation_project(c) not in chosen)[0]
    prev = _entity_lists(load_people_inputs(data_dir).get("entities"))
    return union_entities(live, strip_citations(prev, lambda c: _citation_project(c) not in keep)[0])


def approved_people_entities(data_dir: Path, slugs):
    """The approved inputs' entities when every slug in `slugs` has landed with
    them — a landed project's payloads come from what the owner approved, never
    from a later entities pass — else None (compute from the live inputs)."""
    inputs = load_people_inputs(data_dir)
    if not slugs or not set(slugs) <= set(inputs.get("projects") or []):
        return None
    return _entity_lists(inputs.get("entities"))


def people_changed_since_approval(data_dir: Path, entities: dict, state: dict) -> list:
    """The landed projects whose people, as the live entities cite them, are not
    the ones the owner approved — the entities pass added or changed a person or
    a citation since, or the project's approved inputs are gone. Their approved
    citations stay in the export; the live ones wait for "bring in <slug>"."""
    hashes = load_people_inputs(data_dir).get("people_hashes")
    hashes = hashes if isinstance(hashes, dict) else {}
    return sorted(s for s in state["projects"] if hashes.get(s) != project_people_hash(entities, s))


def save_people_export(data_dir: Path, people: list, entities: dict, projects, known) -> None:
    """people.json (the approved payloads) plus the inputs they came from — the
    entities cut to the approved projects' citations, the store listing and a
    people fingerprint per project — so a later forget/hold can shrink the
    export without a second review and a later stage can tell a project whose
    people changed since it was approved."""
    _common.write_json(data_dir / APPROVED_PEOPLE, people, private=True)
    ensure_private_dir(approved_dir(data_dir))
    cut = strip_citations(entities, lambda c: _citation_project(c) not in projects)[0]
    _common.write_json(approved_dir(data_dir) / APPROVED_PEOPLE_INPUTS,
                       {"projects": sorted(projects), "known_people": known, "entities": cut,
                        "people_hashes": {s: project_people_hash(cut, s) for s in sorted(projects)}},
                       private=True)


def refresh_people_export(data_dir: Path) -> int:
    """After --forget / --forget-session / --hold: the approved People export
    keeps only the citations still approved — a landed project, a summary still
    on disk, not held — and loses the people that fall under the floor. It only
    ever shrinks (a re-grown export needs a commit); an export whose inputs are
    gone is retired. Returns how many payloads went; 0 when nothing was exported."""
    path = data_dir / APPROVED_PEOPLE
    if not path.is_file():
        return 0
    before = len(_common.load_json(path, []) or [])
    src = approved_dir(data_dir) / APPROVED_PEOPLE_INPUTS
    doc = load_people_inputs(data_dir)
    index_doc, all_summaries, _r, _e, state = load_inputs(data_dir)
    approved = set(approved_rollups(data_dir, state))
    if not doc or not approved:
        path.unlink()
        if src.is_file():
            src.unlink()
        return before
    held = held_sessions(all_summaries, state)

    def gone(c):
        key = (_citation_project(c), _citation_session(c))
        return key[0] not in approved or key in held or key not in all_summaries

    entities = strip_citations(_entity_lists(doc.get("entities")), gone)[0]
    known = doc.get("known_people")
    people = people_payloads(entities, index_doc, projects=approved, known=load_known_people(known))
    save_people_export(data_dir, people, entities, approved, known)
    return before - len(people)


# ------------------------------------------------------------------------ review

def _dedupe_lines(lines) -> list:
    seen, out = set(), []
    for ln in lines:
        key = " ".join(ln.lower().split())
        if key and key not in seen:
            seen.add(key)
            out.append(ln)
    return out


def _thread_lines(slug: str, rollup: dict, entities: dict) -> list:
    lines = [_one_line(rollup.get("top_open_thread"), 160)]
    for t in rollup.get("open_threads") or []:
        if isinstance(t, dict):
            t = t.get("thread") or t.get("what")
        lines.append(_one_line(t, 160))
    for t in entities.get("open_threads") or []:
        if not isinstance(t, dict) or t.get("project") != slug:
            continue
        line = _one_line(t.get("thread"), 160)
        action = _one_line(t.get("owner_action"), 120)
        if line and action:
            line += f" (you: {action})"
        lines.append(line)
    return _dedupe_lines(lines)


def _decision_lines(slug: str, rollup: dict, entities: dict) -> list:
    lines = []
    source = list(rollup.get("key_decisions") or [])
    source += [d for d in entities.get("decisions") or [] if isinstance(d, dict) and d.get("project") == slug]
    for d in source:
        if isinstance(d, dict):
            line = _one_line(d.get("decision"), 160)
            why = _one_line(d.get("why"), 120)
            if line and why:
                line += f" — because {why}"
        else:
            line = _one_line(d, 160)
        lines.append(line)
    return _dedupe_lines(lines)


def _bullets(title: str, lines) -> str:
    if not lines:
        return f"{title}: none recorded.\n"
    shown = lines[:REVIEW_MAX_ITEMS]
    text = f"{title}:\n" + "".join(f"- {ln}\n" for ln in shown)
    if len(lines) > len(shown):
        text += f"- …and {len(lines) - len(shown)} more\n"
    return text


def _paragraph(rollup: dict) -> str:
    if rollup.get("stale_rollup"):
        return STALE_ROLLUP_TEXT
    for key in ("summary", "what_it_is"):
        text = _one_line(rollup.get(key), 900)
        if text:
            return text
    body = (rollup.get("note_markdown") or "").strip()
    return _one_line(body.split("\n\n", 1)[0] if body else "", 900) or "(no roll-up text)"


def _person_why(p: dict) -> str:
    role_company = " · ".join(x for x in (_one_line(p.get("role"), 60), _one_line(p.get("company"), 60)) if x)
    return "; ".join(x for x in (_one_line(p.get("relationship"), 120), role_company) if x) \
        or "mentioned in your sessions"


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


def _people_section(candidates, ambiguous, below_floor: int, merged, known_checked: bool) -> str:
    existing = [(p, c, m) for p, c, m in candidates if m]
    new = [(p, c) for p, c, m in candidates if not m]
    text = "## People\n\n"
    if not known_checked:
        text += ("_Store not checked (People tool unavailable): nobody here is matched against your "
                 "People store, so nothing is upserted until it can be read._\n\n")
    else:
        text += f"Already in your People store — will gain new interactions ({len(existing)}):\n"
        for p, cites, m in existing:
            text += (f"- {_one_line(p['name'], 80)} — matched on {m.get('matched_on')} "
                     f"({_n(len(cites), 'citation')})\n")
        text += "- none\n" if not existing else ""
        text += "\n"
    text += f"New — will be added ({len(new)}):\n"
    for p, cites in new:
        text += f"- {_one_line(p['name'], 80)} — {_person_why(p)} ({_n(len(cites), 'citation')})\n"
    text += "- none\n" if not new else ""
    if ambiguous:
        text += f"\nNeeds your call ({len(ambiguous)}) — not upserted until you say who they are:\n"
        for a in ambiguous:
            names = ", ".join(f"{h.get('name') or '?'} ({h.get('slug') or h.get('id') or '?'})" for h in a["matches"])
            text += (f"- {a['name']} ({_n(a['citations'], 'citation')}) — your store has "
                     f"{len(a['matches'])} entries with this name: {names}\n")
    if below_floor:
        text += (f"\n_Left out: {below_floor} with a single mention only "
                 f"({PEOPLE_MIN_CITATIONS} citations needed)._\n")
    n_merged = (merged or {}).get("people_merged") or 0
    if n_merged:
        text += f"\n_{n_merged} duplicate people entries were merged first (same email, or a first name and its full name)._\n"
    return text


def _changed_section(changed, approved: dict, index_doc: dict, kept: str) -> str:
    """The landed projects whose roll-up (or people) changed since the owner
    approved it: named (by their approved name) with the reply that refreshes
    them; `kept` says which shared file keeps the approved version meanwhile."""
    if not changed:
        return ""
    text = f"Changed since approval ({len(changed)}) — kept {kept} as you approved them:\n"
    for slug in changed:
        name = display_name(slug, approved.get(slug) or {}, index_doc)
        text += f"- {name} (`{slug}`) — {CHANGED_SINCE_APPROVAL} — bring in {slug} to refresh\n"
    return text + "\n"


def published_field_diffs(published, approved_entities) -> list:
    """[(name, [diff, …], citations)] for each person in the post-union PUBLISHED
    set whose reviewable fields (role, company, relationship, name, or a wider
    email set) differ from the person the approved export was built from
    (`approved/people-inputs.json`, matched by `_same_person`). A person with no
    approved counterpart is new — the "New" list already carries them; one whose
    fields equal the approved entry has nothing to review and is left out. This
    is what makes the commit's change to `people.json` visible in the digest."""
    prev = [q for q in (approved_entities or {}).get("people") or [] if isinstance(q, dict)]
    rows = []
    for p, cites, _existing in published:
        match = next((q for q in prev if _same_person(p, q)), None)
        if match is None:
            continue
        diffs = []
        for label in ("role", "company", "relationship"):
            old, new = _one_line(match.get(label), 60), _one_line(p.get(label), 60)
            if old != new:
                diffs.append(f"{label}: {old or '—'} → {new or '—'}")
        old_name, new_name = _norm_name(match.get("name")), _norm_name(p.get("name"))
        if store_name_key(old_name) != store_name_key(new_name):
            diffs.append(f"name: {old_name} → {new_name}")
        gained = [e for e in _emails_of(p) if e.lower() not in {x.lower() for x in _emails_of(match)}]
        if gained:
            diffs.append(f"+{_n(len(gained), 'email')}")
        if diffs:
            rows.append((_one_line(p.get("name"), 80), diffs, len(cites)))
    return rows


def _people_diff_section(rows) -> str:
    """The published field changes for people already in the approved export."""
    if not rows:
        return ""
    text = f"\nUpdates to people already in your export ({len(rows)}) — the commit publishes these:\n"
    for name, diffs, n in rows:
        text += f"- {name} — {'; '.join(diffs)} (now {_n(n, 'citation')})\n"
    return text


def _held_section(held_rows) -> str:
    """The held sessions by date and reason only — never a title, never a quote."""
    rows = list(held_rows or [])
    text = f"## Held back as personal ({len(rows)})\n\n"
    if not rows:
        return text + ("None. Say 'hold <date>' to hold a session the classifier missed; "
                       "held sessions are never quoted.\n")
    days = {}
    for r in rows:
        days[r["date"]] = days.get(r["date"], 0) + 1
    items = [f"{r['date']} · {r['reason']}" + (f" (`{r['session'][:8]}`)" if days[r["date"]] > 1 else "")
             for r in rows]
    return text + (", ".join(items) + "\n\nSay 'include <date>' to add one, 'include personal' for all, "
                   "or 'hold <date>' to hold one the classifier missed; held sessions are never quoted.\n")


def render_review(*, slugs, rollups: dict, index_doc: dict, entities: dict, candidates, below_floor: int,
                  outcomes: dict, memory_bytes: int, n_memory_projects: int, n_sessions: int,
                  run_kind: str, merged=None, state=None, held=None, held_rows=None, stale=None,
                  known_checked: bool = False, ambiguous=None, changed=None, approved=None,
                  people_changed=None, people_diffs=None) -> str:
    """The digest the owner reads before anything lands — the one place
    transcript-derived text is shown to them."""
    first, last = date_range(index_doc, slugs)
    text = ("# Claude Code import — review before it lands\n\n"
            f"Staged {today()} ({run_kind} run): {n_sessions} sessions across {len(slugs)} "
            f"projects, {first} → {last}. Nothing below has been written to your memory, "
            f"notes or People store yet.\n\n## Projects\n\n")
    note_words = {"created": "note: new", "updated": "note: update to the existing note",
                  "unchanged": "note: unchanged (nothing new)"}
    for slug in slugs:
        r = rollups[slug]
        p = project_meta(index_doc, slug)
        text += f"### {display_name(slug, r, index_doc)} (`{slug}`)\n\n"
        meta = [f"{project_sessions(index_doc, slug, state, held)} sessions ({_day(p.get('first_ts'))} → {_day(p.get('last_ts'))})",
                f"status: {_one_line(r.get('status'), 20) or 'unknown'}",
                note_words.get(outcomes.get(slug), "note: new")]
        if p.get("cwd"):
            meta.insert(0, f"dir `{p['cwd']}`")
        if slug in (stale or {}):
            meta.append(f"roll-up: stale ({stale[slug]}) — re-run before it can land")
        text += " · ".join(meta) + "\n\n" + _paragraph(r) + "\n\n"
        text += _bullets("Open threads", _thread_lines(slug, r, entities))
        text += _bullets("Decisions", _decision_lines(slug, r, entities)) + "\n"
    text += _people_section(candidates, ambiguous, below_floor, merged, known_checked)
    text += _people_diff_section(people_diffs or [])
    if people_changed:
        text += "\n" + _changed_section(people_changed, approved or {}, index_doc, "in your People export").rstrip("\n") + "\n"
    text += (f"\n## Memory\n\n{memory_bytes:,} B summary (cap {MEMORY_LIMIT:,} B) covering "
             f"{n_memory_projects} projects for the agent's core memory, plus one MEMORY.md row "
             f"if the index budget allows.\n\n")
    text += _changed_section(changed, approved or {}, index_doc, "in memory and the overview")
    text += _held_section(held_rows)
    text += f"\n---\n{REVIEW_FOOTER}\n"
    return text


# ------------------------------------------------------------------------- stage

def load_manifest(data_dir: Path) -> dict:
    doc = _common.load_json(staged_dir(data_dir) / STAGED_MANIFEST, None)
    if not isinstance(doc, dict) or not isinstance(doc.get("projects"), list):
        return {}
    doc["projects"] = [s for s in doc["projects"] if isinstance(s, str)]
    return doc


def staged_known_people(data_dir: Path):
    """The store listing copied at staging (None when the store was not checked)."""
    path = staged_dir(data_dir) / STAGED_KNOWN_PEOPLE
    return load_known_people(path) if path.is_file() else None


def held_rows(index_doc: dict, summaries: dict, held, slugs=None) -> list:
    """The held sessions as the manifest and the review carry them: project,
    uuid, day and reason — no title, no summary text. `slugs` keeps the rows of
    those projects plus any project that has no roll-up to be staged under."""
    rows = []
    for (slug, uuid), reason in (held or {}).items():
        if slugs is not None and slug not in slugs:
            continue
        rows.append({"project": slug, "session": uuid, "date": session_day(index_doc, summaries, slug, uuid),
                     "reason": reason, "held": "personal"})
    rows.sort(key=lambda r: (r["date"], r["project"], r["session"]))
    return rows


def prepare_inputs(data_dir: Path) -> dict:
    """Everything a stage or commit reasons over, with the held sessions taken
    out (summaries, entity citations, counts) and the stale roll-ups redacted."""
    index_doc, all_summaries, rollups, entities, state = load_inputs(data_dir)
    held = held_sessions(all_summaries, state)
    stale = stale_rollups(rollups, all_summaries, held, state, index_doc)
    entities, merged = merge_entities(drop_held_citations(entities, held))
    return {"index": index_doc, "all_summaries": all_summaries,
            "summaries": {k: v for k, v in all_summaries.items() if k not in held},
            "rollups": apply_stale(rollups, stale), "raw_rollups": rollups, "entities": entities,
            "merged": merged, "state": state, "held": held, "stale": stale}


def _ambiguous_rows(triples) -> list:
    return [{"name": _one_line(p["name"], 80), "citations": len(c), "matches": hits} for p, c, hits in triples]


def stage(*, data_dir: Path, ws: Path, run_kind: str = "user", projects=None, exact: bool = False,
          known_people=None) -> dict:
    """Render the review set under <data-dir>/staged/. Touches no sink (the
    workspace notes are only looked at to say new / update / unchanged); a fresh
    stage replaces whatever was pending. `known_people` is the station's store
    listing (a path or the parsed list); without it the store is "not checked"."""
    known = load_known_people(known_people)
    inp = prepare_inputs(data_dir)
    index_doc, summaries, rollups, entities, state = (inp["index"], inp["summaries"], inp["rollups"],
                                                      inp["entities"], inp["state"])
    held, stale, merged = inp["held"], inp["stale"], inp["merged"]
    if isinstance(projects, str):
        projects = split_csv(projects)
    if projects and exact:
        slugs = [s for s in rollups if s in set(projects)]
    else:
        slugs = [s for s in rollups if _common.matches_project(s, projects)]
    if not slugs:
        raise SystemExit("import-claude-context: nothing to stage — no project roll-up "
                         + ("matches --projects" if projects else "under projects/ yet"))

    sdir = staged_dir(data_dir)
    if sdir.exists():
        shutil.rmtree(sdir)
    ensure_private_dir(sdir)
    sndir = staged_notes_dir(data_dir)
    sndir.mkdir(parents=True, exist_ok=True)
    smem = staged_memory_dir(data_dir)
    smem.mkdir(parents=True, exist_ok=True)
    if known is not None:
        _common.write_json(sdir / STAGED_KNOWN_PEOPLE, known, private=True)

    ndir = notes_dir(ws)
    outcomes = {}
    for slug in slugs:
        outcomes[slug] = note_outcome(ndir, slug, rollups[slug], state)
        (sndir / f"{slug}.md").write_text(
            render_note(slug, rollups[slug], index_doc, summaries, run_kind, held), encoding="utf-8")

    # The memory file and the overview are single files over every landed project: preview them from
    # the approved snapshots plus the pending set; a landed roll-up that changed since its approval is named, not refreshed.
    approved = approved_rollups(data_dir, state)
    preview = {s: r for s, r in approved.items() if s not in slugs}
    preview.update({s: rollups[s] for s in slugs})
    changed = [s for s in changed_since_approval(data_dir, inp["raw_rollups"], state) if s not in slugs]
    n_preview = session_total(index_doc, summaries, preview, rollups, state, held)
    (sndir / OVERVIEW).write_text(render_overview(preview, index_doc, n_preview, run_kind, state, held),
                                  encoding="utf-8")
    memory_text = render_memory_file(preview, index_doc, n_preview)
    (smem / MEMORY_FILE).write_text(memory_text, encoding="utf-8")

    # The staged people set is exactly what --commit will publish: the selected projects'
    # live citations and fields unioned with every other landed project's approved ones.
    union_projects = set(approved) | set(slugs)
    post_union = approved_entities_after(data_dir, entities, slugs, union_projects)
    candidates, below_floor, ambiguous = people_candidates(post_union, projects=union_projects, known=known)
    people = people_payloads(post_union, index_doc, projects=union_projects, known=known)
    _common.write_json(sdir / STAGED_PEOPLE, people)
    people_diffs = published_field_diffs(candidates, _entity_lists(load_people_inputs(data_dir).get("entities")))
    people_changed = [s for s in people_changed_since_approval(data_dir, entities, state) if s not in slugs]
    n_sessions = session_total(index_doc, summaries, slugs, rollups, state, held)
    rows = held_rows(index_doc, inp["all_summaries"], held,
                     set(slugs) | {s for (s, _u) in held if s not in rollups})
    stale_here = {s: why for s, why in stale.items() if s in slugs}
    amb_rows = _ambiguous_rows(ambiguous)
    (sdir / STAGED_REVIEW).write_text(
        render_review(slugs=slugs, rollups=rollups, index_doc=index_doc, entities=entities,
                      candidates=candidates, below_floor=below_floor, outcomes=outcomes,
                      memory_bytes=len(memory_text.encode("utf-8")), n_memory_projects=len(preview),
                      n_sessions=n_sessions, run_kind=run_kind, merged=merged, state=state,
                      held=held, held_rows=rows, stale=stale_here, known_checked=known is not None,
                      ambiguous=amb_rows, changed=changed, approved=approved, people_changed=people_changed,
                      people_diffs=people_diffs),
        encoding="utf-8")
    tally = {k: sum(1 for v in outcomes.values() if v == k) for k in ("created", "updated", "unchanged")}
    n_existing = sum(1 for p in people if p.get("existing"))
    counts = {
        "sessions": int(n_sessions), "summarized": sum(1 for (s, _u) in summaries if s in set(slugs)),
        "projects": len(slugs), "people": len(people), "people_existing": n_existing,
        "people_new": len(people) - n_existing, "people_ambiguous": len(amb_rows),
        "memory_bytes": len(memory_text.encode("utf-8")),
        "notes_created": tally["created"], "notes_updated": tally["updated"],
        "notes_unchanged": tally["unchanged"], "held": len(rows), "stale_rollups": len(stale_here),
        "changed_since_approval": len(changed), "people_changed_since_approval": len(people_changed), **merged,
    }
    _common.write_json(sdir / STAGED_MANIFEST, {
        "staged_at": now_iso(), "run_kind": run_kind, "projects": slugs,
        "fingerprint": inputs_fingerprint(data_dir), "counts": counts,
        "known_people": "checked" if known is not None else "not checked",
        "people_ambiguous": amb_rows, "held": rows, "stale_rollups": stale_here,
        "changed_since_approval": changed, "people_changed_since_approval": people_changed,
    })
    write_status(data_dir, "staged", **counts)
    return counts


def _restage_or_clear(data_dir: Path, ws: Path, run_kind: str, remaining) -> list:
    """Keep exactly `remaining` pending (re-rendered against the current inputs,
    with the store listing copied at the first staging) or clear the staging
    area when nothing is left."""
    remaining = [s for s in remaining if (data_dir / PROJECTS_DIR / f"{s}.json").is_file()]
    if remaining:
        known = staged_known_people(data_dir)
        stage(data_dir=data_dir, ws=ws, run_kind=run_kind, projects=remaining, exact=True, known_people=known)
    else:
        shutil.rmtree(staged_dir(data_dir), ignore_errors=True)
    return remaining


# ------------------------------------------------------------------------ commit

def commit(*, data_dir: Path, ws: Path, memory_dir: Path, projects=None) -> dict:
    """Move the staged set (or a --projects subset) into the real sinks. Only
    after the owner's explicit yes; refuses when nothing is staged or the staged
    set no longer matches the summaries it was rendered from."""
    manifest = load_manifest(data_dir)
    pending = manifest.get("projects") or []
    if not pending:
        raise SystemExit("import-claude-context: nothing is staged — run finalize.py --stage, show the "
                         "owner staged/review.md, and --commit only on their yes")
    if manifest.get("fingerprint") != inputs_fingerprint(data_dir):
        raise SystemExit(f"import-claude-context: the staged set (staged {manifest.get('staged_at') or '?'}) "
                         "is stale — the summaries, roll-ups or entities changed since it was rendered; "
                         "run finalize.py --stage again and have the owner review the new digest")
    if isinstance(projects, str):
        projects = split_csv(projects)
    chosen = resolve_selectors(pending, projects, "staged project")
    run_kind = manifest.get("run_kind") or "user"

    inp = prepare_inputs(data_dir)
    index_doc, summaries, rollups, entities, state = (inp["index"], inp["summaries"], inp["rollups"],
                                                      inp["entities"], inp["state"])
    held, merged = inp["held"], inp["merged"]
    missing = [s for s in chosen if s not in rollups]
    if missing:
        raise SystemExit(f"import-claude-context: {len(missing)} staged project(s) have no roll-up any more; "
                         "run finalize.py --stage again")
    stale = [s for s in chosen if s in inp["stale"]]
    if stale:
        raise SystemExit(f"import-claude-context: {len(stale)} staged project(s) have a stale roll-up "
                         "(a session was held, included or forgotten after it was written; the review "
                         "names them) — re-run that project's roll-up over its non-personal sessions, "
                         "stage again and have the owner review the new digest")
    known = staged_known_people(data_dir)
    sndir = staged_notes_dir(data_dir)
    ndir = notes_dir(ws)
    ndir.mkdir(parents=True, exist_ok=True)
    outcomes = {"created": 0, "updated": 0, "unchanged": 0}
    stamp = now_iso()
    for slug in chosen:
        staged_note = sndir / f"{slug}.md"
        outcomes[write_project_note(ndir, slug, rollups[slug], index_doc, summaries, state, run_kind,
                                    staged_note=staged_note, held=held)] += 1
        if staged_note.is_file():
            staged_note.unlink()
        save_approved(data_dir, slug, rollups[slug])
        for (s, u) in summaries:          # the held ones are not in here: they never land
            if s != slug:
                continue
            rec = state["sessions"].setdefault(session_key(s, u), {})
            rec.setdefault("summarized_at", stamp)
            if rec.get("extracted_at") and rec["summarized_at"] < rec["extracted_at"]:
                rec["summarized_at"] = stamp

    committed = approved_rollups(data_dir, state)
    n_sessions = session_total(index_doc, summaries, committed, rollups, state, held)
    (ndir / OVERVIEW).write_text(render_overview(committed, index_doc, n_sessions, run_kind, state, held),
                                 encoding="utf-8")
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_text = render_memory_file(committed, index_doc, n_sessions)
    (memory_dir / MEMORY_FILE).write_text(memory_text, encoding="utf-8")
    row_written, row_reason = guard_memory_row(memory_dir, memory_row(len(committed)))
    print(f"import-claude-context: MEMORY.md row — {row_reason}", file=sys.stderr)
    # people.json publishes exactly the reviewed set: verbatim from staged/people.json on a
    # full commit (fingerprint-guarded), else a subset re-derives its slice the way --stage did.
    approved_entities = approved_entities_after(data_dir, entities, chosen, committed)
    if set(chosen) == set(pending):
        people = _common.load_json(staged_dir(data_dir) / STAGED_PEOPLE, [])
    else:
        people = people_payloads(approved_entities, index_doc, projects=set(committed), known=known)
    save_people_export(data_dir, people, approved_entities, set(committed), known)
    _common.save_state(data_dir, state)

    remaining = _restage_or_clear(data_dir, ws, run_kind, [s for s in pending if s not in chosen])
    counts = {
        "sessions": int(n_sessions), "summarized": sum(1 for (s, _u) in summaries if s in committed),
        "projects": len(committed), "committed": len(chosen), "staged_remaining": len(remaining),
        "changed_since_approval": len(changed_since_approval(data_dir, inp["raw_rollups"], state)),
        "people_changed_since_approval": len(people_changed_since_approval(data_dir, entities, state)),
        "notes_created": outcomes["created"], "notes_updated": outcomes["updated"],
        "notes_unchanged": outcomes["unchanged"], "people": len(people),
        "people_existing": sum(1 for p in people if p.get("existing")),
        "held": sum(1 for (s, _u) in held if s in committed), **merged,
        "memory_bytes": len(memory_text.encode("utf-8")), "memory_row": bool(row_written),
    }
    write_status(data_dir, "staged" if remaining else "done", **counts)
    return counts


def discard(*, data_dir: Path, ws: Path, projects=None) -> dict:
    """Drop the staged set (or a --projects subset). The sinks are untouched."""
    manifest = load_manifest(data_dir)
    pending = manifest.get("projects") or []
    if not pending:
        raise SystemExit("import-claude-context: nothing is staged — nothing to discard")
    if isinstance(projects, str):
        projects = split_csv(projects)
    chosen = resolve_selectors(pending, projects, "staged project")
    remaining = _restage_or_clear(data_dir, ws, manifest.get("run_kind") or "user",
                                  [s for s in pending if s not in chosen])
    if not remaining:
        write_status(data_dir, "discarded", discarded=len(chosen), staged_remaining=0)
    return {"discarded": len(chosen), "staged_remaining": len(remaining)}


def finalize(*, data_dir: Path, ws: Path, memory_dir: Path, run_kind: str = "user") -> dict:
    """Stage and commit in one go — for in-process callers that already hold the
    owner's yes (tests). The CLI never does this: it stages by default and
    commits only on --commit."""
    stage(data_dir=data_dir, ws=ws, run_kind=run_kind)
    return commit(data_dir=data_dir, ws=ws, memory_dir=memory_dir)


# ------------------------------------------------------------------------ forget

_SLUG_PATH_MARKS = ("/", "\\", "..")


def canonical_slug(token) -> bool:
    """A slug as Claude Code writes one — a single path component: never a
    separator, `..`, `.`, a `~` or absolute form, or an empty value."""
    return (isinstance(token, str) and bool(token) and token == token.strip() and token != "."
            and not any(m in token for m in _SLUG_PATH_MARKS)
            and not token.startswith("~") and not os.path.isabs(token))


def known_slugs(data_dir: Path) -> list:
    """Every project slug the import knows: the index, state.json (projects and
    session keys), a pending manifest, and the roll-up / summary / dump /
    approved artefacts on disk — never anything derived from an argument."""
    index_doc = _common.load_json(data_dir / INDEX_FILE, {}) or {}
    state = _common.load_state(data_dir)
    slugs = set(index_doc.get("projects") or {}) | set(state["projects"])
    slugs |= {k.rsplit("/", 1)[0] for k in state["sessions"] if "/" in k}
    slugs |= set(load_manifest(data_dir).get("projects") or [])
    slugs |= set(load_rollups(data_dir)) | set(load_approved(data_dir))
    for sub in (SUMMARIES_DIR, DUMPS_DIR):
        d = data_dir / sub
        if d.is_dir():
            slugs |= {p.name for p in d.iterdir() if p.is_dir()}
    return sorted(s for s in slugs if canonical_slug(s))


def resolve_forget_slug(data_dir: Path, token) -> str:
    """`--forget <slug>`: an exact known slug or a unique part of one, checked
    before any path is built from it — `../x`, `/abs`, `a/b`, `..`, `~`, an
    empty or an unknown value is refused and deletes nothing (PR #4127 review:
    `--forget ../outside` used to delete notes/outside.md)."""
    token = token.strip() if isinstance(token, str) else ""
    if not canonical_slug(token):
        raise SystemExit("import-claude-context: --forget takes a project slug (or a unique part of one), "
                         "never a path; nothing was deleted")
    try:
        return resolve_selectors(known_slugs(data_dir), [token], "project", where=INDEX_MD)[0]
    except SystemExit as e:
        raise SystemExit(f"{e.code}; nothing was deleted")


def guard_owned(targets, roots) -> None:
    """Every path --forget deletes or rewrites must resolve (symlinks followed)
    inside one of the import's own directories and may not itself be a symlink;
    one escape refuses the whole command before anything is touched."""
    real_roots = [os.path.realpath(str(r)) for r in roots]
    for t in targets:
        real = os.path.realpath(str(t))
        inside = False
        for r in real_roots:
            try:
                inside = inside or os.path.commonpath([r, real]) == r
            except ValueError:
                continue
        if os.path.islink(str(t)) or not inside:
            raise SystemExit(f"import-claude-context: refusing --forget: {Path(t).name} is a symlink or resolves "
                             "outside notes/claude-import, data/claude-import and the memory dir; nothing was deleted")


def forget(*, data_dir: Path, ws: Path, memory_dir: Path, slug: str, run_kind: str = "user") -> dict:
    slug = resolve_forget_slug(data_dir, slug)
    ndir = notes_dir(ws)
    note = ndir / f"{slug}.md"
    summaries_dir, dumps_dir = data_dir / SUMMARIES_DIR / slug, data_dir / DUMPS_DIR / slug
    rollup, snapshot = data_dir / PROJECTS_DIR / f"{slug}.json", approved_path(data_dir, slug)
    guard_owned([note, summaries_dir, dumps_dir, rollup, snapshot, ndir / OVERVIEW, memory_dir / MEMORY_FILE],
                [ndir, data_dir, memory_dir])
    removed = {"note": 0, "summaries": 0, "dumps": 0, "rollup": 0, "approved": 0, "state_sessions": 0,
               "entity_citations": 0, "entities_dropped": 0, "people_revoked": 0, "memory_row_removed": 0,
               "staged": 0}
    if note.is_file():
        note.unlink()
        removed["note"] = 1
    for key, d in (("summaries", summaries_dir), ("dumps", dumps_dir)):
        if d.is_dir():
            removed[key] = sum(1 for _ in d.rglob("*") if _.is_file())
            shutil.rmtree(d)
    for key, f in (("rollup", rollup), ("approved", snapshot)):
        if f.is_file():
            f.unlink()
            removed[key] = 1

    state = _common.load_state(data_dir)
    prefix = slug + "/"
    for key in [k for k in state["sessions"] if k.startswith(prefix)]:
        del state["sessions"][key]
        removed["state_sessions"] += 1
    state["projects"].pop(slug, None)
    _common.save_state(data_dir, state)

    entities_path = data_dir / ENTITIES_FILE
    if entities_path.is_file():
        entities, n_cites, n_dropped = strip_citations(load_entities(data_dir), lambda c: _citation_project(c) == slug)
        removed["entity_citations"], removed["entities_dropped"] = n_cites, n_dropped
        _common.write_json(entities_path, entities)
    removed["people_revoked"] = refresh_people_export(data_dir)

    index_doc = _common.load_json(data_dir / INDEX_FILE, {}) or {}
    all_summaries = load_summaries(data_dir)
    held = held_sessions(all_summaries, state)
    summaries = {k: v for k, v in all_summaries.items() if k not in held}
    rollups = approved_rollups(data_dir, state)
    n_sessions = session_total(index_doc, summaries, rollups, rollups, state, held)
    if rollups:
        if ndir.is_dir():
            (ndir / OVERVIEW).write_text(render_overview(rollups, index_doc, n_sessions, run_kind, state, held),
                                         encoding="utf-8")
        if memory_dir.is_dir():
            (memory_dir / MEMORY_FILE).write_text(
                render_memory_file(rollups, index_doc, n_sessions), encoding="utf-8")
    else:
        for p in (ndir / OVERVIEW, memory_dir / MEMORY_FILE):
            if p.is_file():
                p.unlink()
        removed["memory_row_removed"] = int(remove_memory_row(memory_dir))

    # A pending review is re-rendered without this project (its citations are
    # gone from the entities too), so a later "bring it in" is not stale.
    manifest = load_manifest(data_dir)
    pending = manifest.get("projects") or []
    staged_left = 0
    if pending:
        removed["staged"] = int(slug in pending)
        staged_left = len(_restage_or_clear(data_dir, ws, manifest.get("run_kind") or run_kind,
                                            [s for s in pending if s != slug]))
    write_status(data_dir, "forgot", projects=len(rollups), summarized=len(summaries),
                 staged_remaining=staged_left)
    return removed


def purge_dumps(data_dir: Path) -> int:
    d = data_dir / DUMPS_DIR
    if not d.is_dir():
        return 0
    n = sum(1 for p in d.rglob("*") if p.is_file())
    shutil.rmtree(d)
    return n


# ---------------------------------------------------------- sessions (held / forget)

def session_pool(index_doc: dict, summaries: dict) -> list:
    """Every (slug, uuid) the import knows: the index's sessions plus any summary
    the index no longer lists."""
    pool = []
    for slug, p in (index_doc.get("projects") or {}).items():
        for s in p.get("sessions") or []:
            if isinstance(s, dict) and isinstance(s.get("uuid"), str):
                pool.append((slug, s["uuid"]))
    pool += [k for k in summaries if k not in set(pool)]
    return pool


def resolve_session(pool, token, index_doc: dict, summaries: dict, what: str = "session") -> tuple:
    """`<date|uuid>` for the session commands: a uuid (a prefix of 8+ chars
    will do) or a YYYY-MM-DD day. One match, or a refusal — a day shared by two
    sessions is refused with both uuids so the owner can name one."""
    t = (token or "").strip().lower()
    if not t:
        raise SystemExit("import-claude-context: name a session by its date (YYYY-MM-DD) or uuid")
    hits = [k for k in pool if k[1].lower() == t or (len(t) >= 8 and k[1].lower().startswith(t))]
    if not hits:
        hits = [k for k in pool if session_day(index_doc, summaries, *k) == t]
    if not hits:
        raise SystemExit(f"import-claude-context: no {what} matches {token!r}")
    if len(hits) > 1:
        raise SystemExit(f"import-claude-context: {token!r} matches {len(hits)} {what}s — name one by uuid: "
                         + ", ".join(u for _s, u in hits))
    return hits[0]


def _rerender_pending(data_dir: Path, ws: Path) -> int:
    """Re-render a pending review against the new state; how many projects stay staged."""
    manifest = load_manifest(data_dir)
    pending = manifest.get("projects") or []
    if not pending:
        return 0
    return len(_restage_or_clear(data_dir, ws, manifest.get("run_kind") or "user", pending))


def _stale_after(data_dir: Path, keys) -> int:
    index_doc, all_summaries, rollups, _e, state = load_inputs(data_dir)
    stale = stale_rollups(rollups, all_summaries, held_sessions(all_summaries, state), state, index_doc)
    return len({s for (s, _u) in keys if s in stale})


def include_sessions(*, data_dir: Path, ws: Path, token=None, all_held: bool = False) -> dict:
    """The owner's "include <date>" / "include personal": lift the hold on one
    session (or every held one). The override is recorded in state.json so a
    re-stage, and a later re-summarisation, keep the owner's call."""
    index_doc, all_summaries, _r, _e, state = load_inputs(data_dir)
    held = held_sessions(all_summaries, state)
    if all_held:
        keys = sorted(held)
        if not keys:
            raise SystemExit("import-claude-context: nothing is held")
    else:
        keys = [resolve_session(sorted(held), token, index_doc, all_summaries, "held session")]
    for key in keys:
        state["sessions"].setdefault(session_key(*key), {})[PERSONAL_OVERRIDE] = "include"
    _common.save_state(data_dir, state)
    return {"included": len(keys), "held_remaining": len(held) - len(keys),
            "stale_rollups": _stale_after(data_dir, keys), "staged_remaining": _rerender_pending(data_dir, ws)}


def hold_session(*, data_dir: Path, ws: Path, token) -> dict:
    """The owner's "hold <date>": hold a summarised session the classifier let
    through. A session that already landed cannot be held — its note is on disk;
    `--forget <slug>` / `--forget-session` are the undo for that."""
    index_doc, all_summaries, _r, _e, state = load_inputs(data_dir)
    held = held_sessions(all_summaries, state)
    key = resolve_session([k for k in all_summaries if k not in held], token, index_doc, all_summaries)
    rec = state["sessions"].setdefault(session_key(*key), {})
    if rec.get("summarized_at"):
        raise SystemExit(f"import-claude-context: that session has already landed (summarized_at "
                         f"{rec['summarized_at']}); `--forget-session {key[1]}` removes what the import "
                         "keeps of it and `--forget <slug>` removes its project's note")
    rec[PERSONAL_OVERRIDE] = "hold"
    _common.save_state(data_dir, state)
    return {"held": 1, "held_total": len(held) + 1, "stale_rollups": _stale_after(data_dir, [key]),
            "people_revoked": refresh_people_export(data_dir),
            "staged_remaining": _rerender_pending(data_dir, ws)}


def forget_session(*, data_dir: Path, ws: Path, token) -> dict:
    """Delete one session's summary (and partials), dumps, state entry and
    entity citations — the import keeps nothing of it, and the approved People
    export loses its citations. A landed project's note is not rewritten
    (`--forget <slug>` does that); its roll-up reads as stale until re-run."""
    index_doc, all_summaries, _r, _e, state = load_inputs(data_dir)
    slug, uuid = resolve_session(session_pool(index_doc, all_summaries), token, index_doc, all_summaries)
    removed = {"summaries": 0, "dumps": 0, "state_entry": 0}
    sdir = data_dir / SUMMARIES_DIR / slug
    for f in (sorted(sdir.glob(f"{uuid}*.json")) if sdir.is_dir() else []):
        f.unlink()
        removed["summaries"] += 1
    ddir = data_dir / DUMPS_DIR / slug
    for f in (sorted(ddir.glob(f"{uuid}.*.txt")) if ddir.is_dir() else []):
        f.unlink()
        removed["dumps"] += 1
    rec = state["sessions"].pop(session_key(slug, uuid), None)
    removed["state_entry"] = int(rec is not None)
    removed["landed"] = bool(isinstance(rec, dict) and rec.get("summarized_at"))
    _common.save_state(data_dir, state)
    entities_path = data_dir / ENTITIES_FILE
    removed["entity_citations"] = removed["entities_dropped"] = 0
    if entities_path.is_file():
        entities, n_cites, n_dropped = strip_citations(
            load_entities(data_dir), lambda c: (_citation_project(c), _citation_session(c)) == (slug, uuid))
        removed["entity_citations"], removed["entities_dropped"] = n_cites, n_dropped
        if n_cites:
            _common.write_json(entities_path, entities)
    removed["people_revoked"] = refresh_people_export(data_dir)
    removed["stale_rollups"] = _stale_after(data_dir, [(slug, uuid)])
    removed["staged_remaining"] = _rerender_pending(data_dir, ws)
    return removed


def held_json(data_dir: Path) -> list:
    index_doc, all_summaries, _r, _e, state = load_inputs(data_dir)
    return held_rows(index_doc, all_summaries, held_sessions(all_summaries, state))


# --------------------------------------------------------------------------- cli

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", default=None, help="workspace root (default: sutando-config.sh workspace)")
    ap.add_argument("--data-dir", default=None, help="default <workspace>/data/claude-import")
    ap.add_argument("--memory-dir", default=None, help="default util_paths.memory_dir()")
    ap.add_argument("--run-kind", default="user", choices=["onboarding", "user"])
    ap.add_argument("--projects", default=None, metavar="A,B",
                    help="slugs for --stage (any substring), --commit / --discard / --people-json "
                         "(exact slug or a unique part of one)")
    action = ap.add_mutually_exclusive_group()
    action.add_argument("--stage", action="store_true",
                        help="render the review set under <data-dir>/staged/ (the default)")
    action.add_argument("--commit", action="store_true",
                        help="move the staged set into the sinks — only on the owner's explicit yes")
    action.add_argument("--discard", action="store_true", help="drop the staged set (or the --projects subset)")
    action.add_argument("--people-json", action="store_true")
    action.add_argument("--people-doc-merge", action="store_true",
                        help="print --existing-doc with the --append section merged in (same heading replaced)")
    action.add_argument("--held-json", action="store_true", help="the held sessions: project, uuid, date, reason")
    action.add_argument("--include", default=None, metavar="DATE|UUID", help="lift the hold on one session")
    action.add_argument("--include-personal", action="store_true", help="lift the hold on every held session")
    action.add_argument("--hold", default=None, metavar="DATE|UUID", help="hold a session the classifier missed")
    action.add_argument("--forget-session", default=None, metavar="DATE|UUID",
                        help="delete that session's summary, dumps and state entry")
    action.add_argument("--forget", default=None, metavar="SLUG")
    ap.add_argument("--known-people", default=None, metavar="FILE",
                    help="the station's people__list_people output (JSON array) for --stage / --people-json")
    ap.add_argument("--existing-doc", default=None, metavar="FILE", help="--people-doc-merge: the current dossier")
    ap.add_argument("--append", default=None, metavar="FILE", help="--people-doc-merge: the payload's doc_append")
    ap.add_argument("--purge-dumps", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(_common.absorb_dash_values(argv, ("--forget", "--projects")))

    data_dir = _common.data_dir(a.workspace, a.data_dir)
    projects = split_csv(a.projects)
    if a.people_doc_merge:
        if not (a.existing_doc and a.append):
            raise SystemExit("import-claude-context: --people-doc-merge needs --existing-doc FILE and --append FILE")
        existing_doc = Path(a.existing_doc).read_text(encoding="utf-8")
        print(people_doc_merge(existing_doc, Path(a.append).read_text(encoding="utf-8")), end="")
        return 0
    if a.held_json:
        print(json.dumps(held_json(data_dir), ensure_ascii=False, indent=2))
        return 0
    if a.people_json:
        staged_people = staged_dir(data_dir) / STAGED_PEOPLE
        approved = data_dir / APPROVED_PEOPLE
        if not projects and staged_people.is_file():
            payloads = _common.load_json(staged_people, [])
        elif not projects and approved.is_file():
            payloads = _common.load_json(approved, [])
        else:
            known = load_known_people(a.known_people) if a.known_people else staged_known_people(data_dir)
            inp = prepare_inputs(data_dir)
            only = set(resolve_selectors(inp["rollups"], projects, "project")) if projects else None
            entities = approved_people_entities(data_dir, only)
            payloads = people_payloads(inp["entities"] if entities is None else entities, inp["index"],
                                       projects=only, known=known)
        print(json.dumps(payloads, ensure_ascii=False, indent=2))
        return 0
    if a.purge_dumps and not (a.forget or a.stage or a.commit or a.discard):
        n = purge_dumps(data_dir)
        print(json.dumps({"purged_files": n}) if a.json else f"purged {n} dump files")
        return 0

    ws = _common.workspace_root(a.workspace)
    if a.discard:
        r = discard(data_dir=data_dir, ws=ws, projects=projects)
        print(json.dumps(r, sort_keys=True) if a.json else
              f"discarded {r['discarded']} staged projects, {r['staged_remaining']} still staged")
        return 0
    if a.include or a.include_personal or a.hold or a.forget_session:
        if a.include or a.include_personal:
            r = include_sessions(data_dir=data_dir, ws=ws, token=a.include, all_held=a.include_personal)
            verb = f"included {r['included']} held session(s), {r['held_remaining']} still held"
        elif a.hold:
            r = hold_session(data_dir=data_dir, ws=ws, token=a.hold)
            verb = f"held 1 session ({r['held_total']} held in all)"
        else:
            r = forget_session(data_dir=data_dir, ws=ws, token=a.forget_session)
            verb = f"forgot 1 session ({r['summaries']} summary files, {r['dumps']} dumps)"
        print(json.dumps(r, sort_keys=True) if a.json else
              f"{verb}; {r['stale_rollups']} roll-up(s) now stale, {r['staged_remaining']} projects staged")
        return 0
    memory_dir = resolve_memory_dir(a.memory_dir)
    if a.forget:
        r = forget(data_dir=data_dir, ws=ws, memory_dir=memory_dir, slug=a.forget, run_kind=a.run_kind)
        if a.purge_dumps:
            r["purged_files"] = purge_dumps(data_dir)
        print(json.dumps(r, sort_keys=True) if a.json else
              f"forgot {a.forget}: " + ", ".join(f"{k}={v}" for k, v in r.items()))
        return 0
    if a.commit:
        c = commit(data_dir=data_dir, ws=ws, memory_dir=memory_dir, projects=projects)
        if a.purge_dumps:
            c["purged_files"] = purge_dumps(data_dir)
        if a.json:
            print(json.dumps(c, sort_keys=True))
        else:
            print(f"committed {c['committed']} projects ({c['staged_remaining']} still staged): "
                  f"{c['summarized']}/{c['sessions']} sessions summarised, {c['projects']} projects in, "
                  f"notes +{c['notes_created']}/~{c['notes_updated']}, {c['people']} people payloads, "
                  f"memory {c['memory_bytes']} B, MEMORY.md row {'written' if c['memory_row'] else 'skipped'}")
        return 0
    # default: --stage. Nothing is written to a sink; the memory dir was only
    # resolved so a bad install fails here rather than after the owner said yes.
    c = stage(data_dir=data_dir, ws=ws, run_kind=a.run_kind, projects=projects, known_people=a.known_people)
    if a.purge_dumps:
        c["purged_files"] = purge_dumps(data_dir)
    if a.json:
        print(json.dumps(c, sort_keys=True))
    else:
        print(f"staged {c['projects']} projects ({c['summarized']}/{c['sessions']} sessions summarised, "
              f"{c['people']} people ({c['people_existing']} already in the store), {c['held']} held as "
              f"personal, memory {c['memory_bytes']} B) — nothing saved yet; show the owner "
              f"staged/review.md and run --commit only on their yes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
