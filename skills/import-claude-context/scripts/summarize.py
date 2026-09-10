#!/usr/bin/env python3
"""Session summaries, project roll-ups and the entities pass — one script,
Gemini Flash called directly, in parallel, with a schema-enforced JSON reply.

Replaces the model work of SKILL.md steps 5–7 (one Claude Code haiku subagent
per session: 43 sessions took more than ten minutes on a fresh install, most of
it agent start-up and tool round-trips). The haiku procedure stays in SKILL.md
as the fallback for a Mac where no Gemini credential resolves — this script
then exits 3 and writes nothing.

Credential: `credential_resolver.resolve_credential("gemini-text")` — the
platform-managed key (`<workspace>/state/auth/managed-credentials.json`) first,
then the BYO env chain (GEMINI_API_KEY). The key goes into the
`x-goog-api-key` header only; it is never printed, logged or written.

Stages (`--stage sessions|projects|entities|all`, default all), all resumable:
  sessions   every session in index.json that has chunk files under
             dumps/<slug>/ (state.json `skipped_empty` and dump-less sessions
             are left alone) -> summaries/<slug>/<uuid>.json (0600), in the
             exact shape prompts/session-summary.md describes. Skipped when the
             summary is newer than the session's `extracted_at`, unless --force.
             A session longer than --max-chars is sent as ordered PARTS
             (whole chunk files grouped up to the cap), each part summarised
             with the session prompt into a partial `<uuid>.<n>.json`, then ONE
             merge request (same schema) writes the merged `<uuid>.json`;
             only the merged file is read downstream, as with the haiku path.
  projects   one request per slug: prompts/project.md + that project's session
             JSONs (+ the previous projects/<slug>.json, run kind `incremental`,
             when one exists) -> projects/<slug>.json incl. `note_markdown`.
             Skipped when the roll-up is newer than every summary of the slug.
             A project whose summaries exceed --group-max-chars is rolled up in
             ordered PARTS (partial roll-ups, kept in memory) and ONE merge
             request (same schema) writes the file: on the live data one request
             over 39 gtm sessions (191k tokens in) never finished — Flash
             enumerated `open_threads`/`key_decisions` until MAX_TOKENS, 45 KB of
             half-duplicate items, and then hit the timeout on every retry.
  entities   prompts/entities.md over the merged summaries, in GROUPS of at most
             --entities-group-chars (50k: project-aligned, 3–4 sessions each,
             all in parallel), each answering the entities schema; the groups
             (+ the previous entities.json when one exists) are merged in CODE
             — finalize.py's people/company merge (shared email, first name ->
             full name, company by name) and exact de-duplication of
             deals/decisions/open_threads (30 threads kept, round-robin over
             the groups) -> entities.json. The entities reply grows with the
             sessions in the request (measured: 4 gtm sessions -> 19k output
             tokens in 79 s; 9 or 12 sessions -> cut at 32k after 130–260 s,
             every retry the same), so the groups stay small and no model
             merge is attempted (its reply would be the biggest of the run).
  Roll-ups and entities both read only the summaries, so under `--stage all`
  the two stages run at the same time.
After each stage (and every few sessions) progress.py's recount rewrites
status.json, so the desktop sees summarizing -> rolling-up -> staged counts.

The JSON schemas below are authoritative: every field that
prompts/{session-summary,project,entities}.md state and finalize.py reads is
declared, and `session`/`project`/`cwd`/`sessions`/`generated_at` are filled
from the index after the reply rather than trusted from the model.

Requests: `generationConfig` = JSON mime type + responseSchema + temperature
0.2 + maxOutputTokens per stage (sessions 32k, projects 16k, entities 32k, plus
the thinking budget — a reply cut at MAX_TOKENS is an error, never a
half-written file). Thinking per stage: OFF for sessions (extraction; 43/43
came back fine and the run is measured in wall time), 2048 tokens for the
roll-ups and the entities pass — measured on the live data, a roll-up part
over 9 sessions without thinking ignored the prompt's list caps (55 open
threads, 73 PR refs, 8k output tokens; the 12-session parts overran 16k and
were cut), with a 2048 budget it kept 15/15/20 (5k tokens, same wall time).
`--thinking-budget N` overrides every stage (-1 = the model's dynamic default). 429 / 5xx / timeouts retry with
exponential backoff (up to 5 attempts, Retry-After honoured); a 400 or a
blocked / unparseable reply is an error — counted, the run goes on. Per-request
timeout 240 s. Output is counts, seconds and token usage only — never a title,
a path under ~/.claude or transcript text. Exit 0 = every request succeeded,
1 = some errored (the run is resumable), 3 = no Gemini credential.

Stdlib only (urllib, json, concurrent.futures): nothing to install.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common  # noqa: E402
from _common import (  # noqa: E402
    DUMPS_DIR, ENTITIES_FILE, INDEX_FILE, PROJECTS_DIR, SKILL_DIR, SUMMARIES_DIR,
    matches_project, now_iso, session_key, split_csv,
)
import progress as progress_mod  # noqa: E402
from credential_resolver import resolve_credential  # noqa: E402
from util_paths import write_private_text  # noqa: E402

PROMPTS_DIR = SKILL_DIR / "prompts"
DEFAULT_MODEL = "gemini-2.5-flash"  # gemini-2.5-flash-lite 404s for at least one live key: not a default
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_CONCURRENCY = 16
DEFAULT_MAX_CHARS = 3_000_000
DEFAULT_GROUP_MAX_CHARS = 200_000
DEFAULT_ENTITIES_GROUP_CHARS = 50_000
DEFAULT_TIMEOUT = 240
MAX_ATTEMPTS = 5
MAX_OUTPUT_TOKENS = {"sessions": 32768, "projects": 16384, "entities": 32768}
THINKING_BUDGET = {"sessions": 0, "projects": 2048, "entities": 2048}
PROGRESS_EVERY = 8
STAGES = ("sessions", "projects", "entities")
EXIT_NO_CREDENTIAL = 3

_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
_TRANSCRIPT_MARK = "=== TRANSCRIPT ==="
_SUMMARIES_MARK = "=== SESSION SUMMARIES (JSON, oldest first) ==="
_PREVIOUS_MARK = "=== PREVIOUS ROLL-UP (JSON) ==="
_PREVIOUS_ENTITIES_MARK = "=== PREVIOUS ENTITIES (JSON) ==="
_PARTIALS_MARK = "=== PARTIAL SUMMARIES (JSON, in order) ==="
_PARTIAL_ROLLUPS_MARK = "=== PARTIAL ROLL-UPS (JSON, oldest sessions first) ==="


# ------------------------------------------------------------------ schemas

def _s(type_, **kw):
    d = {"type": type_}
    d.update(kw)
    return d


def _obj(props: dict, required=None) -> dict:
    return {"type": "OBJECT", "properties": props,
            "required": list(required if required is not None else props),
            "propertyOrdering": list(props)}


def _arr(items) -> dict:
    return {"type": "ARRAY", "items": items}


STR = _s("STRING")

CITATION = _obj({"project": STR, "session": STR, "quote_or_context": STR})

SESSION_SCHEMA = _obj({
    "session": STR,
    "project": STR,
    "title": STR,
    "date_range": _arr(STR),
    "summary": STR,
    "timeline": _arr(_obj({"when": STR, "what": STR})),
    "tasks": _arr(_obj({"task": STR, "outcome": _s("STRING", enum=["done", "partial", "abandoned", "unknown"]),
                        "detail": STR})),
    "prs_commits": _arr(_obj({"ref": STR, "what": STR})),
    "decisions": _arr(_obj({"decision": STR, "why": STR})),
    "errors_fixes": _arr(_obj({"error": STR, "fix": STR, "verified": _s("BOOLEAN")})),
    "artifacts": _arr(STR),
    "loose_ends": _arr(STR),
    "people": _arr(_obj({"name": STR, "role_or_relationship": STR, "context": STR, "email": STR})),
    "companies": _arr(_obj({"name": STR, "context": STR})),
})

PROJECT_SCHEMA = _obj({
    "project": STR,
    "name": STR,
    "cwd": STR,
    "what_it_is": STR,
    "status": _s("STRING", enum=["active", "paused", "done", "unknown"]),
    "summary": STR,
    "top_open_thread": STR,
    "open_threads": _arr(STR),
    "key_decisions": _arr(_obj({"decision": STR, "why": STR})),
    "accomplished": _arr(STR),
    "prs_commits": _arr(_obj({"ref": STR, "what": STR})),
    "people": _arr(_obj({"name": STR, "role_or_relationship": STR})),
    "companies": _arr(STR),
    "sessions": _arr(STR),
    "note_markdown": STR,
})

ENTITIES_SCHEMA = _obj({
    "generated_at": STR,
    "people": _arr(_obj({"name": STR, "email": STR, "company": STR, "role": STR, "relationship": STR,
                         "citations": _arr(CITATION)})),
    "companies": _arr(_obj({"name": STR, "what": STR, "relationship": STR, "citations": _arr(CITATION)})),
    "deals": _arr(_obj({"name": STR, "counterparty": STR, "stage": STR, "amount": STR,
                        "citations": _arr(CITATION)})),
    "decisions": _arr(_obj({"decision": STR, "why": STR, "project": STR, "citations": _arr(CITATION)})),
    "open_threads": _arr(_obj({"thread": STR, "project": STR, "owner_action": STR, "citations": _arr(CITATION)})),
})

SESSION_MERGE_PROMPT = """You are merging the partial summaries of ONE past Claude Code session for the owner's Sutando assistant. The session was too long for one request, so consecutive parts were summarised separately; the partials are appended below, in order.

Project slug: `<slug>` (working dir `<cwd>`)
Session: `<uuid>` — title from the index: "<title>"

Output: exactly ONE JSON object in the same schema as the partials — no prose, no code fences.

Rules
- Facts only from the partials; never invent names, numbers, PR ids, dates or outcomes.
- `date_range` = the first timestamp of the first part and the last timestamp of the last part.
- `timeline` in chronological order; every list de-duplicated (same fact once, fullest wording kept); at most 25 items per list, 200 characters per item.
- `summary` is 2–4 sentences over the whole session: what the owner was doing, what came out of it, where it ended.
- Keep `[STORED-IN-KEYCHAIN-…]` placeholders verbatim; never write anything that looks like a credential.
"""

PROJECT_MERGE_PROMPT = """You are merging the partial roll-ups of ONE project of the owner's past Claude Code sessions for their Sutando assistant. The project had too many sessions for one request, so consecutive groups of sessions were rolled up separately; the partial roll-ups are appended below, oldest sessions first.

Project slug: `<slug>` (working dir `<cwd>`, <N> sessions, <first date> → <last date>)

Output: exactly ONE JSON object in the same schema as the partials — no prose, no code fences.

Rules
- Only facts present in the partials; empty rather than invented.
- `status`, `top_open_thread` and `open_threads` reflect the NEWEST state (the last partial wins when they disagree); `accomplished`, `key_decisions`, `prs_commits`, `people` and `companies` are the union, de-duplicated, fullest wording kept.
- Lists: at most 20 items each, 200 characters per item, most important first.
- `summary` is 3–6 sentences across all sessions; `note_markdown` is one readable note for a human, 200–600 words, in this order: what the project is, what was accomplished (with PR/commit refs as written), key decisions and why, open threads, people and companies involved. Plain markdown, no top-level `#` heading, no secrets.
"""


# ------------------------------------------------------------------ prompts

def prompt_body(name: str) -> str:
    """The model-facing part of prompts/<name>.md: everything after the first `---` rule."""
    text = (PROMPTS_DIR / name).read_text(encoding="utf-8")
    _, sep, rest = text.partition("\n---\n")
    return rest.strip() + "\n" if sep else text


def _replace_block(text: str, starts_with: str, replacement: str) -> str:
    """Replace the paragraph that starts with `starts_with` (its line plus the
    following non-blank lines) with `replacement`; append it when not found."""
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith(starts_with):
            j = i + 1
            while j < len(lines) and lines[j].strip():
                j += 1
            return "\n".join(lines[:i] + [replacement] + lines[j:])
    return text.rstrip("\n") + "\n\n" + replacement + "\n"


def _drop_block(text: str, starts_with: str) -> str:
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith(starts_with):
            j = i + 1
            while j < len(lines) and lines[j].strip():
                j += 1
            return "\n".join(lines[:i] + lines[j:])
    return text


def _fill(text: str, fields: dict) -> str:
    for k, v in fields.items():
        text = text.replace(f"<{k}>", str(v if v is not None else ""))
    return text


def session_prompt(slug: str, uuid: str, title: str, cwd: str, n_chunks: int, part=None) -> str:
    """prompts/session-summary.md adapted for a direct call: the transcript is
    appended to the message instead of read from files, the reply is the JSON."""
    text = prompt_body("session-summary.md")
    where = (f"The transcript ({n_chunks} chunk file(s), in order) is appended after the line "
             f"`{_TRANSCRIPT_MARK}` at the end of this message.")
    if part:
        where += (f" This is PART {part[0]} of {part[1]} of the session; summarise this part on its own "
                  f"— the parts are merged afterwards.")
    text = _replace_block(text, "Chunk files (", where)
    text = _replace_block(text, "Output:", "Output: exactly ONE JSON object in the schema below — "
                                           "no prose, no code fences.")
    text = _drop_block(text, "Multi-chunk sessions (map-reduce)")
    return _fill(text, {"slug": slug, "uuid": uuid, "title": title, "cwd": cwd})


def project_prompt(slug: str, cwd: str, n: int, first: str, last: str, incremental: bool, part=None) -> str:
    text = prompt_body("project.md")
    where = (f"The session summaries are appended after the line `{_SUMMARIES_MARK}` at the end of this "
             f"message, oldest first.")
    if part:
        where += (f" This is PART {part[0]} of {part[1]} of the project's sessions ({part[2]} sessions in this part); "
                  f"roll up these sessions only — the parts are merged afterwards.")
    text = _replace_block(text, "Session summaries (", where)
    if incremental:
        text = _replace_block(text, "Run kind:", "Run kind: `incremental`. The previous roll-up of this project is "
                                                 f"appended after the line `{_PREVIOUS_MARK}`; carry its facts "
                                                 "forward and update `status`, `top_open_thread` and `open_threads` "
                                                 "to the newest state shown by the session summaries.")
    else:
        text = _replace_block(text, "Run kind:", "Run kind: `full` — every session of the project is listed.")
    text = _replace_block(text, "Output:", "Output: exactly ONE JSON object in the schema below — no prose, no code fences.")
    return _fill(text, {"slug": slug, "cwd": cwd, "N": n, "first date": first, "last date": last})


def entities_prompt(has_previous: bool) -> str:
    text = prompt_body("entities.md")
    inputs = (f"Inputs are appended at the end of this message: "
              + (f"the previous entities pass after the line `{_PREVIOUS_ENTITIES_MARK}` — merge into it, "
                 f"never drop an existing citation — then " if has_previous else "")
              + f"the session summaries after the line `{_SUMMARIES_MARK}`.")
    text = _replace_block(text, "Inputs (", inputs)
    text = _replace_block(text, "Output:", "Output: exactly ONE JSON object in the schema below — no prose, no code fences.")
    return text


# ------------------------------------------------------------------ Gemini

class GeminiError(Exception):
    """A request that failed for good: status (HTTP code or 0) + a short kind."""

    def __init__(self, status: int, kind: str):
        super().__init__(f"{kind} ({status})")
        self.status = status
        self.kind = kind


class _Retryable(Exception):
    def __init__(self, status: int, kind: str, retry_after: float = 0.0):
        super().__init__(kind)
        self.status = status
        self.kind = kind
        self.retry_after = retry_after


def _sleep(seconds: float) -> None:  # patched by tests
    time.sleep(seconds)


def _http_post(url: str, key: str, payload: dict, timeout: float) -> dict:
    """POST the JSON payload; returns the decoded reply. Raises `_Retryable`
    for 429/5xx/transport trouble and `GeminiError` for the rest. The key is
    sent as the `x-goog-api-key` header and appears in no error text."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "x-goog-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except Exception:  # noqa: BLE001 — the body is not reported anyway
            pass
        if e.code in _RETRY_STATUSES:
            ra = e.headers.get("Retry-After") if e.headers else None
            try:
                retry_after = float(ra) if ra else 0.0
            except ValueError:
                retry_after = 0.0
            raise _Retryable(e.code, f"http {e.code}", retry_after) from None
        raise GeminiError(e.code, f"http {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        kind = "timeout" if "timed out" in str(e).lower() or isinstance(e, TimeoutError) else "transport"
        raise _Retryable(0, kind) from None
    except ValueError:
        raise _Retryable(0, "bad reply body") from None


def _extract_json(reply: dict):
    fb = reply.get("promptFeedback") or {}
    if fb.get("blockReason"):
        raise GeminiError(0, "prompt blocked")
    cands = reply.get("candidates") or []
    if not cands:
        raise GeminiError(0, "no candidates")
    cand = cands[0]
    finish = cand.get("finishReason")
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    if not text.strip():
        raise GeminiError(0, f"empty reply ({finish or 'no finish reason'})")
    try:
        doc = json.loads(text)
    except ValueError:
        if finish == "MAX_TOKENS":
            raise GeminiError(0, "reply truncated (max tokens)") from None
        raise _Retryable(0, "unparseable reply") from None
    if not isinstance(doc, dict):
        raise GeminiError(0, "reply is not an object")
    return doc


class Gemini:
    """One credential + model; `call()` does the retries and the token accounting."""

    def __init__(self, key: str, model: str = DEFAULT_MODEL, timeout: float = DEFAULT_TIMEOUT,
                 thinking_override="stage"):
        self._key = key
        self.model = model
        self.url = ENDPOINT.format(model=model)
        self.timeout = timeout
        # "stage" = THINKING_BUDGET per stage; None = the model's dynamic default; an int = every stage
        self.thinking_override = thinking_override
        self.usage = {"prompt_tokens": 0, "candidate_tokens": 0, "thoughts_tokens": 0, "total_tokens": 0}
        self.requests = 0
        self.retries = {"429": 0, "5xx": 0, "transport": 0, "other": 0}

    def thinking_budget(self, stage: str):
        if self.thinking_override == "stage":
            return THINKING_BUDGET[stage]
        return self.thinking_override

    def payload(self, prompt: str, schema: dict, stage: str) -> dict:
        budget = self.thinking_budget(stage)
        # Thoughts count against maxOutputTokens (measured: a dynamic budget
        # spent 15.7k of a 16k cap thinking and the JSON was cut), so the
        # budget is added on top of the stage's answer cap.
        gen = {"responseMimeType": "application/json", "responseSchema": schema,
               "temperature": 0.2, "maxOutputTokens": MAX_OUTPUT_TOKENS[stage] + max(0, budget or 0)}
        if budget is not None and budget >= 0:
            gen["thinkingConfig"] = {"thinkingBudget": int(budget)}
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen,
            # Transcripts about security work or blunt language must not be
            # dropped by the default filters: the summary is the owner's own data.
            "safetySettings": [{"category": c, "threshold": "BLOCK_NONE"} for c in (
                "HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")],
        }

    def _account(self, reply: dict) -> None:
        u = reply.get("usageMetadata") or {}
        self.usage["prompt_tokens"] += int(u.get("promptTokenCount") or 0)
        self.usage["candidate_tokens"] += int(u.get("candidatesTokenCount") or 0)
        self.usage["thoughts_tokens"] += int(u.get("thoughtsTokenCount") or 0)
        self.usage["total_tokens"] += int(u.get("totalTokenCount") or 0)

    def call(self, prompt: str, schema: dict, stage: str) -> dict:
        payload = self.payload(prompt, schema, stage)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                self.requests += 1
                reply = _http_post(self.url, self._key, payload, self.timeout)
                self._account(reply)
                return _extract_json(reply)
            except _Retryable as e:
                bucket = ("429" if e.status == 429 else "5xx" if 500 <= e.status < 600
                          else "transport" if e.kind in ("timeout", "transport") else "other")
                self.retries[bucket] += 1
                if attempt >= MAX_ATTEMPTS:
                    raise GeminiError(e.status, f"{e.kind} after {attempt} attempts") from None
                delay = max(e.retry_after, 2.0 ** (attempt - 1)) + random.uniform(0, 1)
                _sleep(min(delay, 60.0))
        raise GeminiError(0, "unreachable")  # pragma: no cover


def resolve_key(workspace: Path):
    """(key, source) via the shared resolver — managed tier first, then env."""
    managed = workspace / "state" / "auth" / "managed-credentials.json"
    cred = resolve_credential("gemini-text", managed_path=managed)
    return (cred.key, cred.source) if cred.key else ("", "none")


# ------------------------------------------------------------------ inputs

def _iso_epoch(ts) -> float:
    if not ts:
        return 0.0
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


_CHUNK_RE = re.compile(r"^(?P<uuid>[^.]+)\.(?P<n>\d+)\.txt$")


def chunk_files(data_dir: Path, slug: str, uuid: str) -> list:
    d = data_dir / DUMPS_DIR / slug
    if not d.is_dir():
        return []
    found = []
    for f in d.iterdir():
        m = _CHUNK_RE.match(f.name)
        if m and m.group("uuid") == uuid:
            found.append((int(m.group("n")), f))
    return [f for _, f in sorted(found)]


def summary_path(data_dir: Path, slug: str, uuid: str, part=None) -> Path:
    name = f"{uuid}.{part}.json" if part else f"{uuid}.json"
    return data_dir / SUMMARIES_DIR / slug / name


def list_sessions(data_dir: Path, index_doc: dict, state: dict, projects, session, force: bool) -> tuple:
    """([(slug, session record, chunk paths)] to summarise, counts of the rest)."""
    todo, counts = [], {"skipped_empty": 0, "no_dumps": 0, "skipped_current": 0}
    for slug, p in (index_doc.get("projects") or {}).items():
        if not matches_project(slug, projects):
            continue
        for s in p.get("sessions") or []:
            uuid = s.get("uuid") or ""
            if session and not uuid.startswith(session):
                continue
            rec = state["sessions"].get(session_key(slug, uuid)) or {}
            if rec.get("skipped_empty"):
                counts["skipped_empty"] += 1
                continue
            chunks = chunk_files(data_dir, slug, uuid)
            if not chunks:
                counts["no_dumps"] += 1
                continue
            out = summary_path(data_dir, slug, uuid)
            if not force and out.is_file() and _mtime(out) > _iso_epoch(rec.get("extracted_at")):
                counts["skipped_current"] += 1
                continue
            todo.append((slug, s, chunks))
    if session and not todo and not any(counts.values()):
        raise SystemExit(f"import-claude-context: no indexed session starts with {session!r}")
    return todo, counts


def split_parts(chunks: list, max_chars: int) -> list:
    """Group whole chunk files, in order, so no group exceeds max_chars (a single
    oversized chunk is its own group). One group = one request."""
    groups, cur, cur_len = [], [], 0
    for f in chunks:
        size = f.stat().st_size
        if cur and cur_len + size > max_chars:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(f)
        cur_len += size
    if cur:
        groups.append(cur)
    return groups


def read_text(paths) -> str:
    return "".join(p.read_text(encoding="utf-8", errors="replace") for p in paths)


# ------------------------------------------------------------------ stages

class Stage:
    def __init__(self, name: str):
        self.name = name
        self.total = 0
        self.done = 0
        self.skipped = 0
        self.errors = 0
        self.requests = 0
        self.parts = 0
        self.seconds = 0.0
        self.extra = {}
        self.error_list = []

    def fail(self, e: Exception) -> None:
        self.errors += 1
        kind = e.kind if isinstance(e, GeminiError) else type(e).__name__
        status = e.status if isinstance(e, GeminiError) else 0
        if len(self.error_list) < 20:
            self.error_list.append({"stage": self.name, "status": status, "kind": kind})

    def as_dict(self) -> dict:
        d = {"total": self.total, "done": self.done, "skipped": self.skipped, "errors": self.errors,
             "requests": self.requests, "seconds": round(self.seconds, 1)}
        if self.parts:
            d["split_parts"] = self.parts
        d.update(self.extra)
        return d


def _write_private_json(path: Path, doc: dict) -> None:
    _common.ensure_private_dir(path.parent)
    write_private_text(path, _common.dump_json(doc))


def summarise_session(gem: Gemini, data_dir: Path, slug: str, s: dict, cwd: str, chunks: list,
                      max_chars: int) -> dict:
    """One session -> summaries/<slug>/<uuid>.json; returns {"requests": n, "parts": k}."""
    uuid, title = s["uuid"], s.get("title") or ""
    groups = split_parts(chunks, max_chars)
    n_req = 0
    if len(groups) == 1:
        prompt = session_prompt(slug, uuid, title, cwd, len(chunks)) + f"\n{_TRANSCRIPT_MARK}\n" + read_text(chunks)
        doc = gem.call(prompt, SESSION_SCHEMA, "sessions")
        n_req += 1
    else:
        partials = []
        for k, group in enumerate(groups, start=1):
            prompt = (session_prompt(slug, uuid, title, cwd, len(group), part=(k, len(groups)))
                      + f"\n{_TRANSCRIPT_MARK}\n" + read_text(group))
            part_doc = gem.call(prompt, SESSION_SCHEMA, "sessions")
            n_req += 1
            part_doc["session"], part_doc["project"] = uuid, slug
            _write_private_json(summary_path(data_dir, slug, uuid, part=k), part_doc)
            partials.append(part_doc)
        merge = (_fill(SESSION_MERGE_PROMPT, {"slug": slug, "uuid": uuid, "title": title, "cwd": cwd})
                 + f"\n{_PARTIALS_MARK}\n" + "\n".join(json.dumps(p, ensure_ascii=False) for p in partials))
        doc = gem.call(merge, SESSION_SCHEMA, "sessions")
        n_req += 1
    doc["session"], doc["project"] = uuid, slug
    _write_private_json(summary_path(data_dir, slug, uuid), doc)
    return {"requests": n_req, "parts": len(groups) if len(groups) > 1 else 0}


def run_sessions(gem: Gemini, data_dir: Path, index_doc: dict, state: dict, *, projects, session,
                 force: bool, concurrency: int, max_chars: int, recount) -> Stage:
    st = Stage("sessions")
    t0 = time.monotonic()
    todo, counts = list_sessions(data_dir, index_doc, state, projects, session, force)
    st.total = len(todo)
    st.skipped = counts["skipped_current"]
    st.extra = {"skipped_empty": counts["skipped_empty"], "no_dumps": counts["no_dumps"]}
    cwd_of = {slug: (p.get("cwd") or "") for slug, p in (index_doc.get("projects") or {}).items()}
    # Longest sessions first: the tail of a parallel run is set by its biggest request.
    todo.sort(key=lambda t: -sum(f.stat().st_size for f in t[2]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futs = {pool.submit(summarise_session, gem, data_dir, slug, s, cwd_of.get(slug, ""), chunks, max_chars): slug
                for slug, s, chunks in todo}
        finished = 0
        for fut in concurrent.futures.as_completed(futs):
            finished += 1
            try:
                r = fut.result()
                st.done += 1
                st.requests += r["requests"]
                st.parts += r["parts"]
            except Exception as e:  # noqa: BLE001 — counted, the run goes on
                st.fail(e)
            if finished % PROGRESS_EVERY == 0:
                recount()
    st.seconds = time.monotonic() - t0
    recount()
    return st


def merged_summaries(data_dir: Path, slug: str) -> list:
    """[(mtime, uuid, doc)] of the merged summaries of one project, oldest session first."""
    d = data_dir / SUMMARIES_DIR / slug
    if not d.is_dir():
        return []
    out = []
    for f in d.glob("*.json"):
        if f.name.count(".") != 1:
            continue
        doc = _common.load_json(f, None)
        if isinstance(doc, dict):
            out.append((_mtime(f), f.stem, doc))
    out.sort(key=lambda t: ((t[2].get("date_range") or [""])[0] or "", t[1]))
    return out


def _group_by_chars(docs: list, max_chars: int) -> list:
    """Consecutive groups of JSON documents, each at most max_chars of JSON
    (a single oversized document is its own group)."""
    groups, cur, cur_len = [], [], 0
    for d in docs:
        n = len(json.dumps(d, ensure_ascii=False))
        if cur and cur_len + n > max_chars:
            groups.append(cur)
            cur, cur_len = [], 0
        cur.append(d)
        cur_len += n
    if cur:
        groups.append(cur)
    return groups


def roll_up_project(gem: Gemini, data_dir: Path, index_doc: dict, slug: str, force: bool,
                    group_max_chars: int = DEFAULT_GROUP_MAX_CHARS) -> dict:
    """One project -> projects/<slug>.json; returns {"status": done|skipped, "requests": n, "parts": k}."""
    sums = merged_summaries(data_dir, slug)
    if not sums:
        return {"status": "skipped", "requests": 0, "parts": 0}
    out = data_dir / PROJECTS_DIR / f"{slug}.json"
    newest = max(m for m, _, _ in sums)
    if not force and out.is_file() and _mtime(out) >= newest:
        return {"status": "skipped", "requests": 0, "parts": 0}
    previous = _common.load_json(out, None) if out.is_file() else None
    incremental = isinstance(previous, dict)
    meta = (index_doc.get("projects") or {}).get(slug) or {}
    by_uuid = {s.get("uuid"): s for s in meta.get("sessions") or []}
    firsts = [(by_uuid.get(u) or {}).get("first_ts") or (d.get("date_range") or [""])[0] for _, u, d in sums]
    lasts = [(by_uuid.get(u) or {}).get("last_ts") or (d.get("date_range") or ["", ""])[-1] for _, u, d in sums]
    first = min((x for x in firsts if x), default="")[:10]
    last = max((x for x in lasts if x), default="")[:10]
    cwd = meta.get("cwd") or ""
    groups = _group_by_chars([d for _, _, d in sums], group_max_chars)
    n_req = 0
    if len(groups) == 1:
        prompt = project_prompt(slug, cwd, len(sums), first, last, incremental)
        if incremental:
            prompt += f"\n{_PREVIOUS_MARK}\n" + json.dumps(previous, ensure_ascii=False) + "\n"
        prompt += f"\n{_SUMMARIES_MARK}\n" + "\n".join(json.dumps(d, ensure_ascii=False) for d in groups[0])
        doc = gem.call(prompt, PROJECT_SCHEMA, "projects")
        n_req += 1
    else:
        # Too many sessions for one request: partial roll-ups in parallel (the
        # previous roll-up rides with the first part) and one merge request.
        prompts = []
        for k, group in enumerate(groups, start=1):
            prompt = project_prompt(slug, cwd, len(sums), first, last, incremental and k == 1,
                                    part=(k, len(groups), len(group)))
            if incremental and k == 1:
                prompt += f"\n{_PREVIOUS_MARK}\n" + json.dumps(previous, ensure_ascii=False) + "\n"
            prompts.append(prompt + f"\n{_SUMMARIES_MARK}\n" + "\n".join(json.dumps(d, ensure_ascii=False) for d in group))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(prompts)) as pool:
            partials = [f.result() for f in [pool.submit(gem.call, pr, PROJECT_SCHEMA, "projects") for pr in prompts]]
        n_req += len(prompts)
        merge = (_fill(PROJECT_MERGE_PROMPT, {"slug": slug, "cwd": cwd, "N": len(sums), "first date": first,
                                              "last date": last})
                 + f"\n{_PARTIAL_ROLLUPS_MARK}\n" + "\n".join(json.dumps(p, ensure_ascii=False) for p in partials))
        doc = gem.call(merge, PROJECT_SCHEMA, "projects")
        n_req += 1
    doc["project"] = slug
    doc["cwd"] = cwd or doc.get("cwd") or ""
    doc["sessions"] = [u for _, u, _ in sums]
    _write_private_json(out, doc)
    return {"status": "done", "requests": n_req, "parts": len(groups) if len(groups) > 1 else 0}


def run_projects(gem: Gemini, data_dir: Path, index_doc: dict, *, projects, force: bool,
                 concurrency: int, group_max_chars: int, recount) -> Stage:
    st = Stage("projects")
    t0 = time.monotonic()
    slugs = [slug for slug in (index_doc.get("projects") or {}) if matches_project(slug, projects)
             and (data_dir / SUMMARIES_DIR / slug).is_dir()]
    st.total = len(slugs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futs = [pool.submit(roll_up_project, gem, data_dir, index_doc, slug, force, group_max_chars)
                for slug in slugs]
        for fut in concurrent.futures.as_completed(futs):
            try:
                r = fut.result()
                if r["status"] == "done":
                    st.done += 1
                    st.requests += r["requests"]
                    st.parts += r["parts"]
                else:
                    st.skipped += 1
            except Exception as e:  # noqa: BLE001
                st.fail(e)
    st.seconds = time.monotonic() - t0
    recount()
    return st


def _entities_call(gem: Gemini, docs: list, previous) -> dict:
    prompt = entities_prompt(previous is not None)
    if previous is not None:
        prompt += f"\n{_PREVIOUS_ENTITIES_MARK}\n" + json.dumps(previous, ensure_ascii=False) + "\n"
    prompt += f"\n{_SUMMARIES_MARK}\n" + "\n".join(json.dumps(d, ensure_ascii=False) for d in docs)
    return gem.call(prompt, ENTITIES_SCHEMA, "entities")


def _entity_key(kind: str, e: dict) -> str:
    if kind == "decisions":
        return f"{(e.get('project') or '').lower()}|{(e.get('decision') or '').strip().lower()}"
    if kind == "open_threads":
        return f"{(e.get('project') or '').lower()}|{(e.get('thread') or '').strip().lower()}"
    return f"{(e.get('name') or '').strip().lower()}|{(e.get('counterparty') or '').strip().lower()}"


def merge_entity_docs(partials: list) -> dict:
    """Join entity passes in code. People and companies go through finalize.py's
    merge (shared email, bare first name -> its unique full name, companies by
    name — the same rule the review applies); deals, decisions and open threads
    are de-duplicated by key with their citations united; open threads are
    taken round-robin across the partials (each is ordered most important
    first) and capped at 30, as the prompt says."""
    import finalize as finalize_mod  # local: finalize imports the whole sink machinery

    out = {"generated_at": "", "people": [], "companies": [], "deals": [], "decisions": [], "open_threads": []}
    for kind in ("people", "companies"):
        for p in partials:
            out[kind] += [e for e in (p.get(kind) or []) if isinstance(e, dict) and e.get("name")]
    # finalize's people merge never joins two multi-token names (a single model
    # pass has already merged those); across groups the same "Jane Doe" comes
    # back once per group, so identical names (case-insensitive, emails not in
    # conflict) are united first, then finalize folds emails and bare first names.
    by_name = {}
    for e in out["people"]:
        k = finalize_mod._norm_name(e.get("name")).lower()
        mine = {x.lower() for x in finalize_mod._emails_of(e)}
        theirs = {x.lower() for x in finalize_mod._emails_of(by_name[k])} if k in by_name else set()
        if k in by_name and not (mine and theirs and mine.isdisjoint(theirs)):
            by_name[k] = finalize_mod._merge_group([by_name[k], e])
        else:
            by_name.setdefault(k, e)
    out["people"] = list(by_name.values())
    for kind in ("deals", "decisions"):
        seen = {}
        for p in partials:
            for e in p.get(kind) or []:
                if not isinstance(e, dict):
                    continue
                k = _entity_key(kind, e)
                if k in seen:
                    seen[k]["citations"] = finalize_mod._union_citations([seen[k], e])
                else:
                    seen[k] = dict(e)
        out[kind] = list(seen.values())
    seen = {}
    queues = [[e for e in (p.get("open_threads") or []) if isinstance(e, dict)] for p in partials]
    while any(queues) and len(seen) < 30:
        for q in queues:
            if not q:
                continue
            e = q.pop(0)
            k = _entity_key("open_threads", e)
            if k in seen:
                seen[k]["citations"] = finalize_mod._union_citations([seen[k], e])
            elif len(seen) < 30:
                seen[k] = dict(e)
    out["open_threads"] = list(seen.values())
    merged, _counts = finalize_mod.merge_entities(out)
    for kind in ("people", "companies", "deals", "decisions", "open_threads"):
        merged[kind] = [e for e in merged[kind] if e.get("citations")]
    return merged


def run_entities(gem: Gemini, data_dir: Path, index_doc: dict, *, force: bool, concurrency: int,
                 group_max_chars: int = DEFAULT_ENTITIES_GROUP_CHARS, recount) -> Stage:
    st = Stage("entities")
    t0 = time.monotonic()
    out = data_dir / ENTITIES_FILE
    per_project = {slug: merged_summaries(data_dir, slug) for slug in (index_doc.get("projects") or {})}
    per_project = {k: v for k, v in per_project.items() if v}
    all_mtimes = [m for sums in per_project.values() for m, _, _ in sums]
    st.total = 1 if per_project else 0
    if not per_project:
        st.seconds = time.monotonic() - t0
        return st
    if not force and out.is_file() and _mtime(out) >= max(all_mtimes):
        st.skipped = 1
        st.seconds = time.monotonic() - t0
        return st
    previous = _common.load_json(out, None) if out.is_file() else None
    if not isinstance(previous, dict):
        previous = None
    all_docs = [d for sums in per_project.values() for _, _, d in sums]
    total = sum(len(json.dumps(d, ensure_ascii=False)) for d in all_docs)
    if previous is not None:
        total += len(json.dumps(previous, ensure_ascii=False))
    if total <= group_max_chars:
        groups = [all_docs]
    else:
        groups = []
        for sums in per_project.values():
            groups += _group_by_chars([d for _, _, d in sums], group_max_chars)
    try:
        if len(groups) == 1:
            doc = _entities_call(gem, groups[0], previous)
            st.requests += 1
        else:
            # Project-aligned groups in parallel, then the deterministic merge
            # (the previous pass is just one more partial to merge).
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
                futs = [pool.submit(_entities_call, gem, g, None) for g in groups]
                partials = [f.result() for f in futs]
            st.requests += len(groups)
            st.parts = len(groups)
            doc = merge_entity_docs(([previous] if previous is not None else []) + partials)
        doc["generated_at"] = now_iso()
        _write_private_json(out, doc)
        st.done = 1
    except Exception as e:  # noqa: BLE001
        st.fail(e)
    st.seconds = time.monotonic() - t0
    recount()
    return st


# ------------------------------------------------------------------ main

def summarize(*, data_dir: Path, workspace: Path, stage: str = "all", projects=None, session=None,
              force: bool = False, concurrency: int = DEFAULT_CONCURRENCY, max_chars: int = DEFAULT_MAX_CHARS,
              group_max_chars: int = DEFAULT_GROUP_MAX_CHARS,
              entities_group_chars: int = DEFAULT_ENTITIES_GROUP_CHARS, model: str = DEFAULT_MODEL,
              timeout: float = DEFAULT_TIMEOUT, thinking_override="stage") -> dict:
    """Run the stages; returns the counts dict (`gemini: unavailable` when no key)."""
    key, source = resolve_key(workspace)
    if not key:
        return {"gemini": "unavailable", "source": "none", "model": model}
    index_doc = _common.load_json(data_dir / INDEX_FILE, None)
    if not isinstance(index_doc, dict):
        raise SystemExit("import-claude-context: no index.json under the data dir yet — run index.py then extract.py first")
    state = _common.load_state(data_dir)
    gem = Gemini(key, model=model, timeout=timeout, thinking_override=thinking_override)
    del key

    recount_lock = threading.Lock()

    def recount():
        with recount_lock:
            try:
                progress_mod.progress(data_dir)
            except SystemExit:
                pass

    t0 = time.monotonic()
    stages = {}
    if stage in ("sessions", "all"):
        stages["sessions"] = run_sessions(gem, data_dir, index_doc, state, projects=projects, session=session,
                                          force=force, concurrency=concurrency, max_chars=max_chars, recount=recount)

    def do_projects():
        return run_projects(gem, data_dir, index_doc, projects=projects, force=force,
                            concurrency=concurrency, group_max_chars=group_max_chars, recount=recount)

    def do_entities():
        return run_entities(gem, data_dir, index_doc, force=force, concurrency=concurrency,
                            group_max_chars=entities_group_chars, recount=recount)

    if stage == "all":
        # Both read only the summaries: run them side by side.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fp, fe = pool.submit(do_projects), pool.submit(do_entities)
            stages["projects"], stages["entities"] = fp.result(), fe.result()
    elif stage == "projects":
        stages["projects"] = do_projects()
    elif stage == "entities":
        stages["entities"] = do_entities()
    errors = [e for s in stages.values() for e in s.error_list]
    return {
        "gemini": "ok", "source": source, "model": model, "concurrency": concurrency,
        "stages": {k: v.as_dict() for k, v in stages.items()},
        "errors": sum(s.errors for s in stages.values()),
        "error_kinds": errors,
        "requests": gem.requests,
        "retries": gem.retries,
        "usage": gem.usage,
        "seconds_total": round(time.monotonic() - t0, 1),
    }


def human_line(r: dict) -> str:
    if r.get("gemini") != "ok":
        return "no Gemini credential resolves (managed tier or GEMINI_API_KEY): use the haiku fallback"
    bits = []
    for name, s in r["stages"].items():
        bits.append(f"{name} {s['done']}/{s['total']} done, {s['skipped']} skipped, {s['errors']} errors "
                    f"in {s['seconds']}s")
    u = r["usage"]
    return ("; ".join(bits) + f"; {r['requests']} requests ({r['retries']['429']} x 429 retried), "
            f"{u['prompt_tokens']} prompt + {u['candidate_tokens']} candidate tokens, "
            f"{r['seconds_total']}s total [{r['model']}, {r['source']} key]")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=STAGES + ("all",), default="all")
    ap.add_argument("--data-dir", "--out-dir", default=None, help="default <workspace>/data/claude-import")
    ap.add_argument("--workspace", default=None, help="workspace root (default: sutando-config.sh workspace)")
    ap.add_argument("--projects", default=None, help="slugs or slug substrings, comma-separated")
    ap.add_argument("--session", default=None, help="one session uuid (prefix allowed); sessions stage")
    ap.add_argument("--force", action="store_true", help="redo summaries / roll-ups / entities that are current")
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS,
                    help="transcript chars per session request; longer sessions go as ordered parts + one merge request")
    ap.add_argument("--group-max-chars", type=int, default=DEFAULT_GROUP_MAX_CHARS,
                    help="summary-JSON chars per roll-up request; a bigger project goes as parallel parts + one merge request")
    ap.add_argument("--entities-group-chars", type=int, default=DEFAULT_ENTITIES_GROUP_CHARS,
                    help="summary-JSON chars per entities request; groups run in parallel and are merged in code")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per request")
    ap.add_argument("--thinking-budget", type=int, default=None,
                    help="override the per-stage thinking budget (sessions 0, roll-ups and entities 2048): "
                         "0 = off, -1 = the model's dynamic default, else tokens")
    ap.add_argument("--json", action="store_true", help="counts, seconds and token usage as JSON (never content)")
    a = ap.parse_args(_common.absorb_dash_values(argv, ("--projects", "--session")))

    workspace = _common.workspace_root(a.workspace)
    data_dir = _common.data_dir(a.workspace, a.data_dir)
    r = summarize(data_dir=data_dir, workspace=workspace, stage=a.stage, projects=split_csv(a.projects),
                  session=a.session, force=a.force, concurrency=a.concurrency, max_chars=a.max_chars,
                  group_max_chars=a.group_max_chars, entities_group_chars=a.entities_group_chars,
                  model=a.model, timeout=a.timeout,
                  thinking_override="stage" if a.thinking_budget is None else
                  (None if a.thinking_budget < 0 else a.thinking_budget))
    print(json.dumps(r, sort_keys=True) if a.json else human_line(r))
    if r.get("gemini") != "ok":
        return EXIT_NO_CREDENTIAL
    return 1 if r["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
