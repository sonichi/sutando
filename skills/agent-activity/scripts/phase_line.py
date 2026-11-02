#!/usr/bin/env python3
"""The phase word and status line for a task's live status, written to a sidecar the publisher reads.

    <workspace>/state/agent-activity.phase.jsonl   one JSON record per line:
      rules: {"task_id", "at", "seq", "step", "phase", "phase_source": "rules", "private"?}
      model: {"task_id", "at", "for_seq", "phase", "phase_source": "model", "line"?, "line_source"?}

The activity hook writes a `rules` record on every tool call it binds to a task: the phase from the
tool alone, with no model, under a fresh `seq`. It never waits: every lock here is a non-blocking
attempt, and a contended one skips the write. When PHASE_MODEL is `vertex` (default `off`) the hook
also starts `enrich` detached, at most once per task per DEBOUNCE_S; the enricher answers for the
newest rules record and goes again while a newer one arrives, and its answer counts only while that
record is still the newest (`for_seq`).

What may leave the machine, and only for an owner-tier task that has not touched a connected
account: the task line and the task's `working` step lines (the agent's own tool descriptions).
They are normalised (NFKC, zero-width characters removed), passed through the shared chat-secret
filter, redacted (ids, links, paths, numbers, the task's people, mid-sentence names become
placeholders), and then must pass an allowlist: ASCII words, short numbers and our placeholders,
nothing else. Anything outside it skips the call. The model's line passes the same allowlist and
the grounding check; a bad phase drops the record, a bad line drops only the line.

    python3 skills/agent-activity/scripts/phase_line.py enrich --task <task id> [--workspace W]
    python3 skills/agent-activity/scripts/phase_line.py latest --task <task id> [--workspace W]
    python3 skills/agent-activity/scripts/phase_line.py status --task <task id> [--workspace W]
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2] / "src"))  # lint-workspace-resolution: allow-repo-root (engine src, not a data root)
from file_lock import try_lock_fd, unlock_fd  # noqa: E402

SIDECAR = "agent-activity.phase.jsonl"
MANIFEST = HERE.parent / "manifest.json"
DEBOUNCE_S = 5.0
TIMEOUT_S = 2.0
TOKEN_TTL_S = 300.0
MARKER_KEEP_S = 7 * 86400
WINDOW = 8
LINE_MAX = 60
TAIL_BYTES = 256 * 1024
SIDECAR_MAX_BYTES = 4 * 1024 * 1024
RERUNS = 2

# The owner-facing phase words; one shared list with the backend's room.task.progress enum.
PHASES = ("Understanding the request", "Thinking", "Reading files", "Searching code", "Searching the web",
          "Running a command", "Editing code", "Running tests", "Reviewing changes", "Writing a reply",
          "Asking someone", "Waiting")
MODEL_PHASES = tuple(p for p in PHASES if p != "Thinking")

_TEST = re.compile(r"\bpytest\b|\.test\.(py|ts|js)\b|\bnpm (run )?test\b|\bcargo test\b|\bgo test\b"
                   r"|\bvitest\b|\bmake test\b|\bunittest\b")
_REVIEW = re.compile(r"\bgh pr (diff|view|review|checks)\b|\bgit diff\b|\bgh api \S*/(pulls|reviews)\b")
_REPLY = re.compile(r"room_ops\.py (say|send|mention)\b|room\.message\.send|/results/|\bgh pr comment\b")
_WAIT = re.compile(r"^\s*(sleep|until|while)\b|\bgh run watch\b")
_SEARCH = re.compile(r"^\s*(grep|rg|find|git (grep|log|show))\b")
_READ = re.compile(r"^\s*(cat|sed -n|head|tail|less|wc|ls)\b")
# Tools that reach the owner's connected accounts (mail, calendar, files): a task that used one is
# never summarised by the model.
_PRIVATE_TOOL = re.compile(r"^mcp__(sutando-station__composio|claude_ai_(Gmail|Google|Gcal|Drive|Calendar|Outlook|Notion|Slack))",
                           re.I)


def rules_phase(tool_name: str, tool_input) -> str:
    """The phase for one tool call, from the tool and its arguments alone."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    if tool_name == "Read":
        return "Reading files"
    if tool_name in ("Grep", "Glob"):
        return "Searching code"
    if tool_name in ("WebSearch", "WebFetch"):
        return "Searching the web"
    if tool_name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
        return "Editing code"
    if tool_name in ("SendMessage", "Agent", "Workflow", "AskUserQuestion"):
        return "Asking someone"
    if tool_name == "Monitor":
        return "Waiting"
    if tool_name.startswith("mcp__") and "message" in tool_name:
        return "Writing a reply"
    if tool_name == "Bash":
        cmd = str(inp.get("command") or "")
        if inp.get("run_in_background"):
            return "Waiting" if _WAIT.search(cmd) else "Running a command"
        for rx, phase in ((_TEST, "Running tests"), (_REVIEW, "Reviewing changes"), (_REPLY, "Writing a reply"),
                          (_WAIT, "Waiting"), (_SEARCH, "Searching code"), (_READ, "Reading files")):
            if rx.search(cmd):
                return phase
        return "Running a command"
    return "Running a command" if tool_name else "Thinking"


# ---- the sidecar --------------------------------------------------------------------------------

def sidecar_path(workspace) -> Path:
    return Path(workspace) / "state" / SIDECAR


@contextlib.contextmanager
def _try_locked(path: Path):
    """Yield True holding the lock, or False at once when someone else holds it: never waits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    got = False
    try:
        got = try_lock_fd(fd)
        yield got
    finally:
        try:
            if got:
                unlock_fd(fd)
        finally:
            os.close(fd)


def write(workspace, rec: dict) -> bool:
    """Append one record (rotated at the cap); False instead of raising or waiting."""
    try:
        path = sidecar_path(workspace)
        line = (json.dumps(dict(rec, at=round(time.time(), 3)), separators=(",", ":")) + "\n").encode()
        with _try_locked(path.with_name(SIDECAR + ".lock")) as got:
            if not got:
                return False
            if path.exists() and path.stat().st_size + len(line) > SIDECAR_MAX_BYTES:
                os.replace(path, path.with_name(SIDECAR + ".1"))
            with open(path, "ab") as f:
                f.write(line)
        return True
    except Exception:  # noqa: BLE001 - status bookkeeping must never break a tool call
        return False


def records(workspace, task_id: str) -> list:
    """The task's records from the sidecar's tail (the newest are all a reader needs)."""
    out = []
    try:
        with open(sidecar_path(workspace), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            data = f.read()
    except OSError:
        return out
    if size > TAIL_BYTES:
        data = data[data.find(b"\n") + 1:]
    for raw in data.decode("utf-8", "replace").splitlines():
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("task_id") == task_id:
            out.append(r)
    return out


def latest(workspace, task_id: str) -> "dict | None":
    """{phase, phase_source, step, seq, line?} for the task's newest rules record: the model's answer
    only when it was made for that very record. None when the task has no record."""
    recs = records(workspace, task_id)
    rules = [r for r in recs if r.get("phase_source") == "rules" and r.get("seq")]
    if not rules:
        return None
    now = rules[-1]
    out = {"phase": now["phase"], "phase_source": "rules", "step": now.get("step"), "seq": now["seq"]}
    for r in reversed(recs):
        if r.get("phase_source") == "model" and r.get("for_seq") == now["seq"]:
            out.update(phase=r["phase"], phase_source="model")
            if r.get("line"):
                out.update(line=r["line"], line_source="model")
            break
    return out


# ---- switch, debounce, spawn (called from the hook) ---------------------------------------------

def setting(key: str, default: str = "") -> str:
    try:
        from skill_config import config_value
        return config_value(key, MANIFEST) or default
    except Exception:  # noqa: BLE001 - an unreadable setting is the default
        return default


def model_on() -> bool:
    return setting("PHASE_MODEL", "off").lower() == "vertex" and bool(setting("PHASE_MODEL_PROJECT"))


# What a connected-account step may be called anywhere it is shown beyond this machine: the kind
# of account only, never what was in it.
_ACCOUNT_KINDS = (("mail", re.compile(r"gmail|outlook|mail", re.I)), ("calendar", re.compile(r"cal", re.I)),
                  ("contacts", re.compile(r"contact|people", re.I)),
                  ("files", re.compile(r"drive|docs|sheets|dropbox|notion|onedrive", re.I)),
                  ("messages", re.compile(r"slack|teams|discord|telegram|whatsapp", re.I)))
ACCOUNT_LINES = {"mail": "Checking your email", "calendar": "Looking at your calendar",
                 "contacts": "Looking at your contacts", "files": "Looking at your files",
                 "messages": "Checking your messages", "app": "Using a connected app"}


def account_kind(tool_name: str) -> str:
    return next((k for k, rx in _ACCOUNT_KINDS if rx.search(tool_name or "")), "app")


def on_step(workspace, task_id: str, tool_name: str, tool_input, step: "str | None", spawn=None) -> str:
    """The hook's one call per bound tool call: the rules record now, the model detached and debounced.
    A task's first record says so (`first`), and a connected-account step leaves a `.private` marker
    beside the debounce stamps, outside the sidecar's bounded tail; `prompt_for` reads both."""
    phase = rules_phase(tool_name, tool_input)
    rec = {"task_id": task_id, "seq": secrets.token_hex(6), "step": step, "phase": phase, "phase_source": "rules"}
    seen = _marker(workspace, task_id, "seen")
    first = not seen.exists() and _touch(seen)
    if first:
        rec["first"] = True
        _prune(seen.parent)
    if _PRIVATE_TOOL.match(tool_name or ""):
        rec["private"] = True
        rec["account"] = account_kind(tool_name)
        _touch(_marker(workspace, task_id, "private"))  # unwritten: prompt_for's other checks fail closed
    if not write(workspace, rec) and first:
        with contextlib.suppress(OSError):
            seen.unlink()  # the first record never landed: the next step is the first again
    if model_on() and _debounced(workspace, task_id):
        (spawn or _spawn)(workspace, task_id)
    return phase


def _marker_dir(workspace) -> Path:
    return Path(workspace) / "state" / "phase-line"


def _marker(workspace, task_id: str, kind: str) -> Path:
    return _marker_dir(workspace) / f"{re.sub(r'[^A-Za-z0-9._~-]', '_', task_id)}.{kind}"


def _touch(path: Path) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o644))
        return True
    except OSError:
        return False


def _prune(d: Path) -> None:
    """Drop markers, stamps and locks untouched for MARKER_KEEP_S: a task that old has left the
    sidecar's tail, so its prompt is refused with or without them."""
    cutoff = time.time() - MARKER_KEEP_S
    with contextlib.suppress(OSError):
        for p in d.iterdir():
            with contextlib.suppress(OSError):
                if p.stat().st_mtime < cutoff:
                    p.unlink()


def _debounced(workspace, task_id: str) -> bool:
    """True at most once per task per DEBOUNCE_S; contention reads as not yet."""
    try:
        stamp = _marker(workspace, task_id, "last")
        with _try_locked(stamp.with_suffix(".lock")) as got:
            if not got:
                return False
            last = float(stamp.read_text()) if stamp.exists() else 0.0
            if time.time() - last < DEBOUNCE_S:
                return False
            stamp.write_text(str(time.time()))
        return True
    except Exception:  # noqa: BLE001
        return False


def _spawn(workspace, task_id: str) -> None:
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "enrich", "--task", task_id,
                      "--workspace", str(workspace)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# ---- what may be sent ---------------------------------------------------------------------------

_ZERO_WIDTH = re.compile("[­᠎​-‏‪-‮⁠-⁤﻿]")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+")
_PR_URL = re.compile(r"(?i)https?://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)\S*")
_PR_NUM = re.compile(r"(?:\bPR(?:\s+#?|\s*#)|(?<![\w&])#)(\d{1,6})\b")  # never a bare PR1: that is ours
_MATRIX = re.compile(r"[@!$+#][A-Za-z0-9._=/+-]+(?::[A-Za-z0-9.-]+)?")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_SLASHED = re.compile(r"\S*[/\\]\S*")
_DOTTED = re.compile(r"\b\S+\.[A-Za-z0-9]{2,}\S*")
_ASSIGN = re.compile(r"\S+=\S*")
_LONGNUM = re.compile(r"\d[\d ().-]{5,}\d|\d{7,}")
_MIXED = re.compile(r"\b(?=[A-Za-z_-]*\d)(?=\d*[A-Za-z])[A-Za-z0-9_-]{6,}\b")
_LONGWORD = re.compile(r"\b[A-Za-z]{25,}\b")
try:
    from chat_secret_filter import SLACK_TOKEN_PATTERN as _SLACK  # the one Slack token family
except ImportError:
    _SLACK = re.compile(r"(?!)")
_CRED = re.compile(r"\b(?:(?:sk|pk|rk)-[A-Za-z0-9_-]{6,}|(?:ghp|gho|ghu|ghs|ghr|github_pat|npm)_[A-Za-z0-9_]{6,}"
                   r"|(?:AKIA|ASIA)[A-Z0-9]{12,}|AIza[A-Za-z0-9_-]{20,}"
                   r"|ya29\.[A-Za-z0-9_-]{6,}|glpat-[A-Za-z0-9_-]{6,})|" + _SLACK.pattern)
_FILTERED = re.compile(r"\[(?:REDACTED|STORED-IN-KEYCHAIN)-[^\]]*\]")
_PLACEHOLDER = re.compile(r"<[a-z]+>|\bPR\d+\b|\bPerson\d+\b")
# Words that may open a sentence (the goal, a step, after a full stop): imperative verbs and function
# words, never a common given name. Any other capitalised word may be a name and is masked.
_STARTERS = frozenset("""A About Add After Again All Also An And Any Apply Archive Are As Ask At Be Before Both Build
But By Can Cancel Check Clean Clone Close Comment Commit Compare Configure Confirm Copy Count Could Create Debug Delete
Deploy Diagnose Diff Disable Do Does Done Download Draft Dump Each Edit Enable Every Explain Export Extract Fetch Filter
Find First Fix For Format From Generate Get Grep Has Have Help Here Hey Hi How If Import In Inspect Install Investigate
Is It Its Just Keep Kill Last Let Lint List Load Locate Log Look Make Measure Merge Migrate Move My New Next No None Not
Note Notify Now Of Ok Okay On Once Only Open Or Our Parse Please Poll Post Prepare Print Probe Prune Publish Pull Push
Read Rebase Record Refactor Remove Rename Render Reply Request Rerun Reset Resolve Restore Retry Revert Review Run Save
Scan Schedule Search Second Send Set Should Show So Some Sort Start Still Stop Summarize Sync Tail Tell Test Thank
Thanks That The Then There These They This Those To Trace Track Try Update Upload Use Verify Wait Was Watch We Were
What When Where Which While Who Why With Without Would Write Yes You Your""".split())
# Words that may stand mid-sentence capitalised; any other is treated as a possible name.
_TERMS = frozenset("""PR PRs CI API APIs JSON YAML README CLI SDK MCP UI UX HTTP HTTPS SQL OK ID IDs URL URLs Git GitHub
Python TypeScript JavaScript Rust Go Node Docker Matrix Vertex Gemini Claude Codex Cloud Run Terraform macOS Linux
Mac Windows iOS Android Chrome Safari Slack Discord Telegram Linear Notion Gmail Google Calendar Drive Commons Room
Stand Sutando Electron Tauri Synapse Caddy Cinny Space Flash Lite Haiku Sonnet Opus LFM Qwen Phi Gemma TTS STT
I AM PM UTC PT Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July
August September October November December""".split())


def normalise(text: str) -> str:
    return _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text or ""))


class Redactor:
    """What must not leave the machine becomes a placeholder; PR numbers are kept locally to put back."""

    def __init__(self, people=()):
        self.prs: dict[str, str] = {}
        self.people = {}
        for p in people:
            for part in [p] + re.split(r"[\s._-]+", p or ""):
                part = normalise(part).strip()
                if len(part) >= 2:
                    self.people.setdefault(part.lower(), f"Person{len(self.people) + 1}")

    def _pr(self, num: str) -> str:
        for k, v in self.prs.items():
            if v == num:
                return k
        key = f"PR{len(self.prs) + 1}"
        self.prs[key] = num
        return key

    def __call__(self, text: str) -> str:
        try:
            from chat_secret_filter import filter_chat_secrets
            text = filter_chat_secrets(normalise(text)).text
        except Exception:  # noqa: BLE001 - without the shared filter, the allowlist still decides
            text = normalise(text)
        text = _PR_URL.sub(lambda m: self._pr(m.group(1)), text)
        text = _PR_NUM.sub(lambda m: self._pr(m.group(1)), text)
        for rx, rep in ((_FILTERED, "<secret>"), (_CRED, "<secret>"), (_URL, "<link>"), (_EMAIL, "<email>"),
                        (_MATRIX, "<id>"), (_SLASHED, "<path>"),
                        (_ASSIGN, "<secret>"), (_DOTTED, "<file>"), (_LONGNUM, "<number>"), (_MIXED, "<id>"),
                        (_LONGWORD, "<id>")):
            text = rx.sub(rep, text)
        for name, ph in sorted(self.people.items(), key=lambda kv: -len(kv[0])):
            text = re.sub(rf"(?i)(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", ph, text)
        return _mask_names(text)

    def restore(self, line: str) -> str:
        return re.sub(r"\bPR\d+\b", lambda m: f"PR #{self.prs[m.group(0)]}" if m.group(0) in self.prs else m.group(0), line)


def _mask_names(text: str) -> str:
    """A capitalised word that is not a known term may be a name, wherever it stands: masked, unless
    it is a sentence starter (`_STARTERS`) at a sentence start. A line, the goal and each `[working]`
    step each begin a sentence."""
    out = []
    for line in text.split("\n"):
        words, start = [], True
        for tok in line.split(" "):
            core = tok.strip(",.;:!?()\"")
            bare = core[:-2] if core.endswith("'s") else core
            if bare and bare[0].isupper() and bare not in _TERMS and not _PLACEHOLDER.fullmatch(bare) \
                    and not (start and bare in _STARTERS):
                tok = tok.replace(bare, "<name>", 1)
            words.append(tok)
            if tok in ("[working]", ""):
                continue
            start = tok[-1] in ".!?:"
        out.append(" ".join(words))
    return "\n".join(out)


_PIECE = r"(?:<[a-z]+>|PR\d{1,3}|Person\d{1,3}|\[working\]|[A-Za-z]{1,24}|\d{1,6}|[,.;:!?()\"'&+%-])"
_ALLOWED = re.compile(rf"{_PIECE}+")


def allowed(text: str) -> bool:
    """The allowlist every outbound prompt and every kept line must pass: ASCII words, short numbers,
    our placeholders and plain punctuation, nothing else."""
    if not text.isascii() or _CRED.search(text):
        return False
    for t in text.split():
        bare = _PLACEHOLDER.sub("", t)
        if not _ALLOWED.fullmatch(t) or re.search(r"[A-Za-z][0-9]|[0-9][A-Za-z]", bare) or len(bare) > 30:
            return False
    return True


# ---- the model line -----------------------------------------------------------------------------

SYSTEM = (
    "You write the live status of an AI agent for its owner. Read the task goal and the agent's recent steps "
    "(oldest first; the last step is now). Answer JSON only: {\"phase\", \"line\"}.\n"
    "- phase: exactly one of " + json.dumps(list(MODEL_PHASES)) + ": what the agent is doing at the last step.\n"
    "- line: at most 60 characters, present tense, plain words, saying what the agent is doing and toward what. "
    "Name the higher-level action the recent steps add up to, using the task goal. Keep placeholders such as PR3 "
    "exactly as written. Refer to people by role (\"a reviewer\", \"the owner\"), never by placeholder. No paths, "
    "URLs, ids, code or quotes. Mention only things in the goal or the steps. If the steps don't say what the "
    "agent is doing, use \"Working on the request\".")
SCHEMA = {"type": "object", "properties": {"phase": {"type": "string", "enum": list(MODEL_PHASES)},
                                           "line": {"type": "string"}}, "required": ["phase", "line"]}
_PERSON = re.compile(r"\b(Person|Peer|Agent|User)\d+\b")
_ROLE = {"Person": "a teammate", "Peer": "an agent", "Agent": "an agent", "User": "a user"}
FALLBACK_LINE = "Working on the request"


def validate_line(raw, prompt_text: str, people=()) -> "str | None":
    """The model's line as it may be kept (placeholder text), or None to keep only the phase. No
    capitalised word but a known term, a sentence starter or an opening verb in -ing; no person the
    task names, in any case."""
    line = re.sub(r"\s+", " ", normalise(str(raw or ""))).strip().rstrip(".")
    line = _PERSON.sub(lambda m: _ROLE[m.group(1)], line)
    if not line or line == FALLBACK_LINE or len(line) > LINE_MAX or "<" in line or not allowed(line):
        return None
    if any(m.group(0) not in prompt_text for m in re.finditer(r"\bPR\d+\b", line)):
        return None
    for i, w in enumerate(re.findall(r"[A-Za-z]+", line)):
        opener = i == 0 and (w in _STARTERS or (w.endswith("ing") and len(w) > 4))
        if w[0].isupper() and w not in _TERMS and not opener:
            return None  # a capitalised word the input did not let through: possibly a name
    names = [k for k in Redactor(people).people if len(k) >= 3]
    if any(re.search(rf"(?i)(?<![A-Za-z]){re.escape(n)}(?![A-Za-z])", line) for n in names):
        return None
    return line


_TOKEN: dict = {}


def _access_token(fresh: bool = False) -> str:
    """The host's gcloud access token, kept in this process's memory (never on disk) for TOKEN_TTL_S;
    the fetch has the call's own budget. '' when gcloud cannot answer in time."""
    now = time.monotonic()
    if not fresh and _TOKEN.get("value") and now < _TOKEN.get("exp", 0.0):
        return _TOKEN["value"]
    _TOKEN.clear()
    value = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True,
                           timeout=TIMEOUT_S).stdout.strip()
    if value:
        _TOKEN.update(value=value, exp=now + TOKEN_TTL_S)
    return value


def gemini(project: str, model: str, system: str, user: str, timeout: float = TIMEOUT_S) -> dict:
    """One Vertex generateContent call with the host's gcloud credentials; {} on any failure. A
    rejected token is dropped and fetched once more."""
    import urllib.error
    import urllib.request
    try:
        url = (f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/"
               f"models/{model}:generateContent")
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": 80, "responseMimeType": "application/json",
                                     "responseSchema": SCHEMA, "thinkingConfig": {"thinkingLevel": "minimal"}}}
        def post(token: str) -> dict:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())

        token = _access_token()
        if not token:
            return {}
        try:
            out = post(token)
        except urllib.error.HTTPError as e:
            token = _access_token(fresh=True) if e.code == 401 else ""
            if not token:
                return {}
            out = post(token)
        return json.loads(out["candidates"][0]["content"]["parts"][0]["text"])
    except Exception:  # noqa: BLE001 - no answer is the rules answer
        return {}


def _task_context(workspace, task_id: str) -> "tuple[str | None, list[str]]":
    """(goal, people) for an owner-tier task, from its file via the shared lookup; (None, []) for any
    other task, which is never sent."""
    try:
        from local_task_protocol import find_archived_task, parse_task_headers_lenient
        path = find_archived_task(Path(workspace) / "tasks", task_id)
        if path is None:
            return None, []
        text = path.read_text(encoding="utf-8", errors="replace")
        hd = parse_task_headers_lenient(text).headers
        if hd.get("access_tier") != "owner" or str(hd.get("collaborator", "")).lower() == "true":
            return None, []
        body = text.split("\ntask:", 1)[1] if "\ntask:" in text else ""
        goal = []
        for ln in body.split("===SUTANDO SYSTEM", 1)[0].splitlines():
            if goal and re.match(r"^[a-z_]+: ", ln):
                break  # a task-mid file's headers follow its task line: they are not the goal
            goal.append(ln)
        people = [hd.get("sender_name", "")]
        people += [m.split(":", 1)[0].lstrip("@") for m in re.split(r"[,\s]+", hd.get("room_members", "")) if m]
        return "\n".join(goal).strip()[:300], people
    except Exception:  # noqa: BLE001
        return None, []


def _steps(workspace, task_id: str) -> list:
    """The task's newest `working` step lines (the agent's own tool descriptions), oldest first."""
    out = []
    try:
        lines = (Path(workspace) / "state" / "agent-activity.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for raw in lines:
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        t = r.get("task") if isinstance(r, dict) else None
        if isinstance(t, dict) and t.get("id") == task_id and r.get("kind") == "working":
            step = f"[working] {str(r.get('line') or '')[:120]}"
            if not out or out[-1] != step:
                out.append(step)
    return out[-WINDOW:]


def _sendable(workspace, task_id: str) -> bool:
    """Whether the task's whole history is known and none of it touched a connected account: no
    `.private` marker, a marker directory that could have held one, and its first record still in
    sight with no private record after it. Anything unknown is not sendable."""
    d = _marker_dir(workspace)
    if not (d.is_dir() and os.access(d, os.W_OK)) or _marker(workspace, task_id, "private").exists():
        return False
    recs = records(workspace, task_id)
    return any(r.get("first") for r in recs) and not any(r.get("private") for r in recs)


def prompt_for(workspace, task_id: str) -> "tuple[str, Redactor] | None":
    """The outbound prompt and its redactor, or None when nothing may be sent for this task."""
    if not _sendable(workspace, task_id):
        return None
    goal, people = _task_context(workspace, task_id)
    if goal is None:
        return None
    red = Redactor(people)
    prompt = ("Task goal: " + red(goal) + "\n\nRecent steps:\n" + "\n".join(red(s) for s in _steps(workspace, task_id))
              + "\n\nJSON:")
    return (prompt, red) if allowed(prompt) else None


def enrich(workspace, task_id: str, call=None) -> "dict | None":
    """The model record written for the task's newest rules record, or None (the rules record stands).
    Goes again while a newer rules record arrives during the call, at most RERUNS more times."""
    if not model_on():
        return None
    rec = None
    for _ in range(1 + RERUNS):
        now = latest(workspace, task_id)
        if now is None:
            return rec
        built = prompt_for(workspace, task_id)
        if built is None:
            return rec
        prompt, red = built
        out = (call or gemini)(setting("PHASE_MODEL_PROJECT"), setting("PHASE_MODEL_ID", "gemini-3.5-flash-lite"),
                               SYSTEM, prompt)
        phase = out.get("phase") if isinstance(out, dict) and out.get("phase") in MODEL_PHASES else None
        if phase is None:
            return rec
        new = {"task_id": task_id, "for_seq": now["seq"], "phase": phase, "phase_source": "model"}
        line = validate_line(out.get("line"), prompt, people=list(red.people))
        if line:
            new.update(line=red.restore(line), line_source="model")
        if write(workspace, new):
            rec = new
        if latest(workspace, task_id)["seq"] == now["seq"]:
            return rec
    return rec


# ---- why a task isn't moving: one status for every open or waiting task -------------------------

BUSY_WINDOW_S = 300.0
PICKUP_WINDOW_S = 3600.0
SUMMARY_TAIL_BYTES = 256 * 1024


def _ts(value, default: "float | None" = None) -> "float | None":
    """A finite epoch from a record field (a number, or an ISO-8601 string); `default` otherwise."""
    if isinstance(value, str) and not value.replace(".", "", 1).isdigit():
        try:
            from datetime import datetime
            value = datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _finished(workspace, task_id: str) -> bool:
    """The task has its result (results/<id>.txt) or a done summary in the summaries log's tail."""
    if (Path(workspace) / "results" / f"{task_id}.txt").exists():
        return True
    try:
        with open(Path(workspace) / "state" / "agent-activity.summaries.jsonl", "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - SUMMARY_TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    needle = json.dumps(task_id)
    for raw in tail.splitlines():
        if needle not in raw:
            continue
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if isinstance(r, dict) and isinstance(r.get("task"), dict) and r["task"].get("id") == task_id:
            return True
    return False


def _recent_other_task(workspace, task_id: str, now: float) -> bool:
    """Another task on this host recorded a step in the last BUSY_WINDOW_S: the agent is busy."""
    try:
        with open(sidecar_path(workspace), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return False
    for raw in lines[1:] if size > TAIL_BYTES else lines:  # a cut first line is not a record
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("task_id") not in (None, task_id) \
                and r.get("phase_source") == "rules" and now - (_ts(r.get("at")) or 0.0) <= BUSY_WINDOW_S:
            return True
    return False


def _picked_up_at(workspace, task_id: str, now: float) -> "float | None":
    """When the activity index saw this task picked up, if recently (the index is never pruned)."""
    try:
        from activity_rows import open_task_index
        started = _ts((open_task_index(Path(workspace)).get(task_id) or {}).get("started")) or 0.0
    except Exception:  # noqa: BLE001
        return None
    return started if started and now - started <= PICKUP_WINDOW_S else None


def status_for(workspace, task_id: str, *, inbox=None, active: "str | None" = None,
               now: "float | None" = None) -> "dict | None":
    """{phase, line, since} saying why the task is where it is, or None for a finished or unknown
    task (its label shows). The line never carries an age: the client renders it from `since`.

    In order: delegated (handoffs), running (the newest phase record), picked up with no step yet,
    queued behind another request, queued. Pickup outranks the queue: a task's file stays in the
    queue while the core works it, and "Queued" must never outlive the wait. `active` is the task this seat is working on now,
    when the caller knows it; otherwise any other task's step in the last few minutes means busy.
    Nothing about another task is ever named."""
    now = time.time() if now is None else now
    if _finished(workspace, task_id):
        return None
    try:
        import handoffs
        delegated = handoffs.delegated_status(workspace, task_id, now=now)
    except Exception:  # noqa: BLE001 - delegated status is optional (src/handoffs.py)
        delegated = None
    if delegated:
        since = min((t for h in handoffs.open_handoffs(workspace, task_id, now=now)
                     if (t := _ts(h.get("at"))) is not None), default=now)
        return {"phase": "Delegated", "line": delegated["line"], "since": since, "waiting": {"reason": "delegated"}}
    st = latest(workspace, task_id)
    if st is not None:
        rules = [r for r in records(workspace, task_id) if r.get("seq") == st["seq"]]
        line = st.get("line") or st.get("step") or st["phase"]
        return {"phase": st["phase"], "line": line, "since": _ts(rules[-1].get("at"), now) if rules else now}
    picked = _picked_up_at(workspace, task_id, now)
    if picked is not None:
        return {"phase": "Understanding the request", "line": "Reading the request", "since": picked}
    try:
        from task_queue import position
        pos = position(Path(workspace), task_id, inbox)
    except Exception:  # noqa: BLE001 - an unreadable queue is no queue line
        pos = {"position": 0}
    if pos.get("position"):
        try:
            from task_queue import pending
            since = next((_ts(t.get("since"), now) for t in pending(Path(workspace), inbox) if t["id"] == task_id), now)
        except Exception:  # noqa: BLE001
            since = now
        busy = (active is not None and active != task_id) or (active is None and _recent_other_task(workspace, task_id, now))
        if busy:
            return {"phase": "Waiting", "line": "Waiting: working on another request", "since": since,
                    "waiting": {"reason": "busy"}}
        ahead = int(pos["position"]) - 1
        return {"phase": "Waiting", "line": f"Queued · {ahead} ahead" if ahead else "Queued · next", "since": since,
                "waiting": {"reason": "queued", "position": int(pos["position"])}}
    return None


def published_status(workspace, task_id: str, *, share_line: bool = False, **kw) -> "dict | None":
    """What the agent publishes to the server ledger: phase, summary, since and, for a waiting task,
    `waiting` {reason, position?}. Off (the default, and the owner's share_live_status default) the
    summary is the phase word or the fixed waiting line ("Queued · next"), never a step's text; with
    `share_line` it is the detail line as `shareable_line` lets it leave. The publisher adds the task's source
    message itself. None when `status_for` says nothing."""
    st = status_for(workspace, task_id, **kw)
    if st is None:
        return None
    waiting = st.get("waiting")
    out = {"phase": st["phase"], "summary": st["phase"], "since": st["since"]}
    if waiting:
        out["waiting"] = dict(waiting)
        if waiting["reason"] in ("queued", "busy"):
            out["summary"] = st["line"]
    if share_line:
        out["summary"] = shareable_line(workspace, task_id, st)
    return out


# The only lines the opt-in share carries: fixed text naming nothing. Free text never leaves, since a
# private read is recognisable only by MCP tool and mail read by shell or browser passes a scrubber.
FIXED_LINES = frozenset({"Reading the request", "Waiting: working on another request", "Queued · next"})
_QUEUED = re.compile(r"Queued · \d{1,4} ahead")


def shareable_line(workspace, task_id: str, status: "dict | None" = None) -> "str | None":
    """The status line as it may leave this machine, for an opt-in share: a connected-account task
    gets only its account kind ("Checking your email"); otherwise our own fixed line, or the phase
    word. Never a step's text or the model's line, which stay local."""
    st = status if status is not None else status_for(workspace, task_id)
    if st is None:
        return None
    private = [r for r in records(workspace, task_id) if r.get("private")]
    if private or _marker(workspace, task_id, "private").exists():
        kind = private[-1].get("account") if private else None
        return ACCOUNT_LINES.get(kind or "app", ACCOUNT_LINES["app"])
    line = str(st.get("line") or "")
    return line if line in FIXED_LINES or _QUEUED.fullmatch(line) else st["phase"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase word and status line for a task's live status.")
    ap.add_argument("cmd", choices=("enrich", "latest", "status", "published"))
    ap.add_argument("--task", required=True)
    ap.add_argument("--workspace")
    a = ap.parse_args(argv)
    if a.workspace:
        ws = Path(a.workspace)
    else:
        from workspace_default import resolve_workspace
        ws = resolve_workspace(migrate=False)
    if a.cmd == "enrich":
        enrich(ws, a.task)
        return 0
    show = {"status": status_for, "published": published_status, "latest": latest}[a.cmd]
    print(json.dumps(show(ws, a.task)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
