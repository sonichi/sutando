#!/usr/bin/env python3
"""Per-session cleaned + redacted dialog dumps, chunked for a cheap summariser.

Adds NO transcript parsing of its own. The dialog stream comes from
session-recap's extract.py (`dump --root <slug dir> --session <uuid>
--filter dialog --max-chars 0`: user + assistant text only — tool I/O,
attachments and thinking never enter), harness noise is stripped with
context_resume's NOISE_BLOCK_RE / NOISE_LINE_RE, secrets are redacted with
secret_scanner.scan_and_redact, and the result is written as <=120k-char
chunks split only at `[ts] USER:` / `[ts] ASSISTANT:` turn boundaries to
`<out-dir>/dumps/<slug>/<uuid>.<n>.txt` — 0600 files in 0700 dirs.

Flags:
  --projects a,b       slugs or slug substrings
  --session uuid       one session (prefix allowed)
  --new                only sessions whose (mtime,size) changed since their
                       last extraction (or never extracted)
  --max-chars-total N  soft cap for the run, default 8,000,000 chars — newest
                       sessions first; the run stops once the cap is reached
  --max-chunk-chars N  default 120,000
  --min-chars N        a session whose cleaned dialog is shorter than this
                       (default 400 chars) — or yields no turn at all — is not
                       extracted: no dump, counted as `skipped_empty`
  --json               print the counts as JSON (counts only)

Records per-session extraction in state.json (extracted_at, mtime, size,
chunks, chars; `skipped_empty: true` for the sessions that had nothing worth
summarising, so a `--new` re-run leaves them alone until the file changes) and
writes status.json (phase + counts). `extracted` counts only the sessions
that wrote at least one chunk — the fresh-install run of 2026-09-10 reported
47 extracted while 43 dump files existed: four aborted sessions (no assistant
turn) went through the loop with an empty chunk list and were counted anyway,
and a fifth survived at 178 bytes, enough to pass an emptiness test and
produce a useless summary.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402
from _common import (  # noqa: E402
    DUMPS_DIR, INDEX_FILE, REPO, matches_project, now_iso, session_key, split_csv,
    write_status,
)
import index as index_mod  # noqa: E402
from context_resume import NOISE_BLOCK_RE, NOISE_LINE_RE  # noqa: E402
from secret_scanner import scan_and_redact  # noqa: E402
from util_paths import write_private_text  # noqa: E402

DEFAULT_MAX_TOTAL = 8_000_000
DEFAULT_MAX_CHUNK = 120_000
DEFAULT_MIN_CHARS = 400
_HEADER_ALLOWANCE = 200
TRUNCATED_MARK = " […turn truncated to fit one chunk]"

TURN_HEADER_RE = re.compile(r"^(\[[^\]\n]*\] (?:USER|ASSISTANT): )(.*)$")
RECAP_EXTRACT = REPO / "skills" / "session-recap" / "scripts" / "extract.py"

# Key shapes detect-secrets has no plugin for, so secret_scanner leaves them in
# place. Applied after scan_and_redact with the same placeholder format so
# downstream readers see one convention. Candidates for upstreaming.
_EXTRA_SECRET_PATTERNS = {
    "Google API Key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
}


def redact(text: str) -> tuple:
    """(redaction count, redacted text) — secret_scanner first, local shapes after."""
    hits, out = scan_and_redact(text)
    n = len(hits)
    for name, pat in _EXTRA_SECRET_PATTERNS.items():
        out, k = pat.subn(f"[STORED-IN-KEYCHAIN-{name}]", out)
        n += k
    return n, out


def load_recap_extract():
    spec = importlib.util.spec_from_file_location("session_recap_extract", RECAP_EXTRACT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def dump_session(recap, slug_dir: Path, uuid: str) -> str:
    """The `[ts] USER:/ASSISTANT:` stream for one session, via --root."""
    argv = ["extract.py", "dump", "--root", str(slug_dir), "--session", uuid,
            "--filter", "dialog", "--max-chars", "0"]
    saved = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = argv
        with contextlib.redirect_stdout(buf):
            recap.main()
    finally:
        sys.argv = saved
    return buf.getvalue()


# ------------------------------------------------------------------ turns/chunks

def parse_turns(dump: str) -> list:
    """[(header, body)] — header is the `[ts] ROLE: ` prefix; text before the
    first header (never expected) gets header None and is dropped by clean."""
    turns = []
    header = None
    body = []
    for line in dump.split("\n"):
        m = TURN_HEADER_RE.match(line)
        if m:
            if header is not None:
                turns.append((header, "\n".join(body)))
            header, body = m.group(1), [m.group(2)]
        else:
            body.append(line)
    if header is not None:
        turns.append((header, "\n".join(body)))
    return turns


def clean_turn(header: str, body: str):
    """Strip the harness noise from one turn; None when nothing human is left."""
    body = NOISE_BLOCK_RE.sub("", body)
    lines = [ln for ln in body.split("\n") if not NOISE_LINE_RE.match(ln)]
    body = "\n".join(lines).strip()
    if not body:
        return None
    return header + body


def clean_dump(dump: str) -> str:
    kept = []
    for header, body in parse_turns(dump):
        turn = clean_turn(header, body)
        if turn:
            kept.append(turn)
    return "\n".join(kept) + ("\n" if kept else "")


def chunk_turns(text: str, max_chars: int = DEFAULT_MAX_CHUNK) -> list:
    """Greedy packing of whole turns; a chunk never starts mid-turn. A single
    turn longer than the cap is truncated (a paste, not a conversation)."""
    body_limit = max(1000, max_chars - _HEADER_ALLOWANCE)
    turns = [h + b for h, b in parse_turns(text)]
    chunks = []
    cur = []
    cur_len = 0
    for turn in turns:
        if len(turn) > body_limit:
            turn = turn[: body_limit - len(TRUNCATED_MARK)] + TRUNCATED_MARK
        if cur and cur_len + len(turn) + 1 > body_limit:
            chunks.append("\n".join(cur) + "\n")
            cur, cur_len = [], 0
        cur.append(turn)
        cur_len += len(turn) + 1
    if cur:
        chunks.append("\n".join(cur) + "\n")
    return chunks


def chunk_header(slug: str, uuid: str, n: int, total: int) -> str:
    return f"# claude-import · project {slug} · session {uuid} · chunk {n}/{total}\n\n"


def write_chunks(dumps_root: Path, slug: str, uuid: str, chunks: list) -> list:
    slug_dir = _common.ensure_private_dir(dumps_root / slug)
    for old in slug_dir.glob(f"{uuid}.*.txt"):
        old.unlink()
    paths = []
    total = len(chunks)
    for n, body in enumerate(chunks, start=1):
        path = slug_dir / f"{uuid}.{n}.txt"
        write_private_text(path, chunk_header(slug, uuid, n, total) + body)
        paths.append(path)
    return paths


# ------------------------------------------------------------------------ extract

def _select_sessions(index_doc: dict, projects, session, new_only, state, root: Path) -> tuple:
    """[(slug, session record, stat)] newest first, plus the unchanged count."""
    wanted = []
    unchanged = 0
    for slug, p in (index_doc.get("projects") or {}).items():
        if not matches_project(slug, projects):
            continue
        for s in p.get("sessions") or []:
            if session and not s["uuid"].startswith(session):
                continue
            path = root / slug / s["file"]
            try:
                st = os.stat(path)
            except OSError:
                continue  # gone since the index — the next index run drops it
            if new_only:
                rec = state["sessions"].get(session_key(slug, s["uuid"])) or {}
                if (rec.get("extracted_mtime_ns"), rec.get("extracted_size")) == (st.st_mtime_ns, st.st_size):
                    unchanged += 1
                    continue
            wanted.append((slug, s, st))
    if session and not wanted and not unchanged:
        raise SystemExit(f"import-claude-context: no indexed session starts with {session!r}")
    wanted.sort(key=lambda t: (t[1].get("last_ts") or "", t[2].st_mtime_ns), reverse=True)
    return wanted, unchanged


def _drop_dumps(dumps_root: Path, slug: str, uuid: str) -> None:
    """Remove a session's chunk files (a session that turned out empty on a
    re-extraction must not keep the dumps of an earlier version)."""
    for old in (dumps_root / slug).glob(f"{uuid}.*.txt"):
        old.unlink()


def extract(root=None, *, out_dir, projects=None, session=None, new_only=False,
            max_chars_total=DEFAULT_MAX_TOTAL, max_chunk_chars=DEFAULT_MAX_CHUNK,
            min_chars=DEFAULT_MIN_CHARS) -> dict:
    root = Path(root) if root else index_mod.default_root()
    out_dir = Path(out_dir)
    _common.refuse_inside(root, out_dir)
    if isinstance(projects, str):
        projects = split_csv(projects)

    index_doc = _common.load_json(out_dir / INDEX_FILE, None)
    if not index_doc:
        index_doc = index_mod.index(root, out_dir=out_dir, projects=projects)["index"]
    state = _common.load_state(out_dir)
    wanted, unchanged = _select_sessions(index_doc, projects, session, new_only, state, root)

    recap = load_recap_extract()
    dumps_root = _common.ensure_private_dir(out_dir / DUMPS_DIR)
    counts = {"extracted": 0, "skipped_empty": 0, "chunks": 0, "chars": 0, "redactions": 0,
              "errors": 0, "skipped_unchanged": unchanged, "budget_exhausted": False, "remaining": 0}
    for i, (slug, s, st) in enumerate(wanted):
        if counts["chars"] >= max_chars_total:
            counts["budget_exhausted"] = True
            counts["remaining"] = len(wanted) - i
            break
        try:
            raw = dump_session(recap, root / slug, s["uuid"])
        except SystemExit:
            counts["errors"] += 1
            continue
        cleaned = clean_dump(raw)
        n_redactions, redacted = redact(cleaned)
        chunks = chunk_turns(redacted, max_chunk_chars)
        dialog_chars = len(redacted.strip())
        rec = state["sessions"].setdefault(session_key(slug, s["uuid"]), {})
        rec.update({
            "mtime_ns": st.st_mtime_ns, "size": st.st_size,
            "extracted_at": now_iso(), "extracted_mtime_ns": st.st_mtime_ns,
            "extracted_size": st.st_size, "redactions": n_redactions,
        })
        if not chunks or dialog_chars < min_chars:
            # Nothing a summariser could work with (an aborted or never-answered
            # session, or a couple of lines). No dump; remembered in state so
            # --new does not retry it; never counted as extracted.
            _drop_dumps(dumps_root, slug, s["uuid"])
            rec.update({"skipped_empty": True, "chunks": 0, "chars": dialog_chars})
            counts["skipped_empty"] += 1
            continue
        rec.pop("skipped_empty", None)
        paths = write_chunks(dumps_root, slug, s["uuid"], chunks)
        chars = sum(len(c) for c in chunks)
        rec.update({"chunks": len(paths), "chars": chars})
        counts["extracted"] += 1
        counts["chunks"] += len(paths)
        counts["chars"] += chars
        counts["redactions"] += n_redactions
    _common.save_state(out_dir, state)
    write_status(out_dir, "extracted", **{k: v for k, v in counts.items() if k != "budget_exhausted"},
                 budget_exhausted=counts["budget_exhausted"])
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=None)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--projects", default=None)
    ap.add_argument("--session", default=None)
    ap.add_argument("--new", action="store_true")
    ap.add_argument("--max-chars-total", type=int, default=DEFAULT_MAX_TOTAL)
    ap.add_argument("--max-chunk-chars", type=int, default=DEFAULT_MAX_CHUNK)
    ap.add_argument("--min-chars", type=int, default=DEFAULT_MIN_CHARS,
                    help="sessions with less cleaned dialog than this are skipped, not extracted")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(_common.absorb_dash_values(argv, ("--projects", "--session")))

    out_dir = _common.data_dir(a.workspace, a.out_dir)
    c = extract(a.root, out_dir=out_dir, projects=split_csv(a.projects), session=a.session,
                new_only=a.new, max_chars_total=a.max_chars_total,
                max_chunk_chars=a.max_chunk_chars, min_chars=a.min_chars)
    if a.json:
        print(json.dumps(c, sort_keys=True))
    else:
        print(f"extracted {c['extracted']} sessions into {c['chunks']} chunks "
              f"({c['chars'] / 1e6:.1f}M chars, {c['redactions']} redactions); "
              f"{c['skipped_unchanged']} unchanged skipped, {c['skipped_empty']} empty skipped, "
              f"{c['errors']} errors"
              + (f"; budget reached, {c['remaining']} sessions left" if c["budget_exhausted"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
