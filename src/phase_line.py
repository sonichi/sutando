#!/usr/bin/env python3
"""The phase word and status line for a task's live status, written to a sidecar the publisher reads.

    <workspace>/state/agent-activity.phase.jsonl   one JSON record per line:
      {"task_id", "at", "step", "phase", "phase_source": "rules"|"model", "line"?, "line_source"?}

The activity hook writes a `rules` record on every tool call it binds to a task: the phase from the
tool alone, with no model. When PHASE_MODEL is `vertex` (default `off`) it also starts this module's
`enrich` detached, at most once per task per DEBOUNCE_S: a Gemini call on our Vertex project that
refines the phase and writes a model line for the same step. The hook never waits for it.

What leaves the machine is the task goal and the task's own recent step lines, redacted (ids,
links, paths, emails, phones, tokens and the task's people become placeholders) and then scanned;
any hit skips the call. A model line is kept only when it passes the length cap, the leak scan and
the grounding check (every PR placeholder it names was in the input); placeholders for people are
rewritten to roles, PR numbers are put back. Anything that fails leaves the rules record standing.

    python3 src/phase_line.py enrich --task <task id> [--workspace W]
    python3 src/phase_line.py latest --task <task id> [--workspace W]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from file_lock import locked_file  # noqa: E402

SIDECAR = "agent-activity.phase.jsonl"
MANIFEST = Path(__file__).resolve().parents[1] / "skills" / "agent-activity" / "manifest.json"  # lint-workspace-resolution: allow-repo-root (the skill's manifest, not a data root)
DEBOUNCE_S = 5.0
TIMEOUT_S = 2.0
WINDOW = 8
LINE_MAX = 60
SIDECAR_MAX_BYTES = 4 * 1024 * 1024

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


def write(workspace, rec: dict) -> bool:
    """Append one record (locked, rotated at the cap); False instead of raising."""
    try:
        path = sidecar_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(dict(rec, at=round(time.time(), 3)), separators=(",", ":")) + "\n").encode()
        with locked_file(path.with_name(SIDECAR + ".lock")):
            if path.exists() and path.stat().st_size + len(line) > SIDECAR_MAX_BYTES:
                os.replace(path, path.with_name(SIDECAR + ".1"))
            with open(path, "ab") as f:
                f.write(line)
        return True
    except Exception:  # noqa: BLE001 - status bookkeeping must never break a tool call
        return False


def records(workspace, task_id: str) -> list:
    out = []
    try:
        lines = sidecar_path(workspace).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for raw in lines:
        try:
            r = json.loads(raw)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("task_id") == task_id:
            out.append(r)
    return out


def latest(workspace, task_id: str) -> "dict | None":
    """{phase, phase_source, line?, step} for the task's newest step: the model's answer when it is
    for that step, else the rules record. None when the task has no record."""
    recs = records(workspace, task_id)
    rules = [r for r in recs if r.get("phase_source") == "rules"]
    if not rules:
        return None
    now = rules[-1]
    out = {"phase": now["phase"], "phase_source": "rules", "step": now.get("step")}
    for r in reversed(recs):
        if r.get("phase_source") == "model" and r.get("step") == now.get("step") and r.get("at", 0) >= now.get("at", 0):
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


def on_step(workspace, task_id: str, tool_name: str, tool_input, step: "str | None", spawn=None) -> str:
    """The hook's one call per bound tool call: the rules record now, the model detached and debounced."""
    phase = rules_phase(tool_name, tool_input)
    write(workspace, {"task_id": task_id, "step": step, "phase": phase, "phase_source": "rules"})
    if model_on() and _debounced(workspace, task_id):
        (spawn or _spawn)(workspace, task_id)
    return phase


def _debounced(workspace, task_id: str) -> bool:
    """True at most once per task per DEBOUNCE_S; the enricher reads the newest step when it runs."""
    try:
        d = Path(workspace) / "state" / "phase-line"
        d.mkdir(parents=True, exist_ok=True)
        stamp = d / f"{re.sub(r'[^A-Za-z0-9._~-]', '_', task_id)}.last"
        with locked_file(stamp.with_suffix(".lock")):
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


# ---- redaction and the leak gate ----------------------------------------------------------------

_URL = re.compile(r"https?://\S+")
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/(\d+)\S*")
_PR_NUM = re.compile(r"(?:\bPR(?:\s+#?|\s*#)|(?<![\w&])#)(\d{1,6})\b")  # never a bare PR1: that is ours
_MXID = re.compile(r"@[A-Za-z0-9._=/+-]+:[A-Za-z0-9.-]+")
_ROOM = re.compile(r"![A-Za-z0-9._~=+-]+:[A-Za-z0-9.-]+")
_EVENT = re.compile(r"\$[A-Za-z0-9_+/=-]{16,}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PATH = re.compile(r"(?<![\w])(?:~|/(?:Users|home|private|tmp|var|opt|etc))/\S*")
_TOKEN = re.compile(r"\b(?:sk-|ghp_|gho_|github_pat_|xox[abp]-|AKIA|AIza)[A-Za-z0-9_-]{8,}|\b[A-Za-z0-9_-]{32,}\b")
_PHONE = re.compile(r"\+?\d[\d ().-]{8,}\d")
_LEAK = (_URL, _MXID, _ROOM, _EVENT, _EMAIL, _PATH, _TOKEN)


class Redactor:
    """Replaces what must not leave the machine; keeps a local table so PR numbers can be put back."""

    def __init__(self, people=()):
        self.prs: dict[str, str] = {}
        self.people = {}
        for p in people:
            p = (p or "").strip()
            if len(p) >= 3:
                self.people.setdefault(p.lower(), f"Person{len(self.people) + 1}")

    def _pr(self, num: str) -> str:
        for k, v in self.prs.items():
            if v == num:
                return k
        key = f"PR{len(self.prs) + 1}"
        self.prs[key] = num
        return key

    def __call__(self, text: str) -> str:
        text = _PR_URL.sub(lambda m: self._pr(m.group(1)), text or "")
        text = _PR_NUM.sub(lambda m: self._pr(m.group(1)), text)
        for rx, rep in ((_URL, "<link>"), (_EMAIL, "<email>"), (_MXID, "<user>"), (_ROOM, "<room>"),
                        (_EVENT, "<event>"), (_PATH, "<path>"), (_TOKEN, "<token>"), (_PHONE, "<phone>")):
            text = rx.sub(rep, text)
        for name, ph in self.people.items():
            text = re.sub(rf"(?i)\b{re.escape(name)}\b", ph, text)
        return text

    def restore(self, line: str) -> str:
        return re.sub(r"\bPR(\d+)\b", lambda m: f"PR #{self.prs[m.group(0)]}" if m.group(0) in self.prs else m.group(0), line)


def leaks(text: str) -> bool:
    return any(rx.search(text) for rx in _LEAK)


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
_BAD_LINE = re.compile(r"https?://|[/\\]\w|@\w|![A-Za-z0-9]|`|<(link|email|user|room|event|path|token|phone)>"
                       r"|\.(py|ts|js|md|json|sh|ya?ml)\b|[0-9a-f]{12,}")
_PERSON = re.compile(r"\b(Person|Peer|Agent|User)\d+\b")
_ROLE = {"Person": "a teammate", "Peer": "an agent", "Agent": "an agent", "User": "a user"}
FALLBACK_LINE = "Working on the request"


def validate_line(raw, prompt_text: str) -> "str | None":
    """The model's line as it may be shown (placeholder text), or None to keep the rules line."""
    line = re.sub(r"\s+", " ", str(raw or "")).strip().rstrip(".")
    line = _PERSON.sub(lambda m: _ROLE[m.group(1)], line)
    if not line or line == FALLBACK_LINE or len(line) > LINE_MAX or _BAD_LINE.search(line):
        return None
    if any(m.group(0) not in prompt_text for m in re.finditer(r"\bPR\d+\b", line)):
        return None
    return line


def gemini(project: str, model: str, system: str, user: str, timeout: float = TIMEOUT_S) -> dict:
    """One Vertex generateContent call with the host's gcloud credentials; {} on any failure."""
    try:
        token = subprocess.run(["gcloud", "auth", "print-access-token"], capture_output=True, text=True,
                               timeout=10).stdout.strip()
        if not token:
            return {}
        url = (f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/global/publishers/google/"
               f"models/{model}:generateContent")
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": 80, "responseMimeType": "application/json",
                                     "responseSchema": SCHEMA, "thinkingConfig": {"thinkingLevel": "minimal"}}}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())
        return json.loads(out["candidates"][0]["content"]["parts"][0]["text"])
    except Exception:  # noqa: BLE001 - no answer is the rules answer
        return {}


def _task_context(workspace, task_id: str) -> "tuple[str, list[str]]":
    """(goal text, people names) from the task file's headers and body, via the shared lookup."""
    try:
        from local_task_protocol import find_archived_task, parse_task_headers_lenient
        path = find_archived_task(Path(workspace) / "tasks", task_id)
        if path is None:
            return "", []
        text = path.read_text(encoding="utf-8", errors="replace")
        hd = parse_task_headers_lenient(text).headers
        body = text.split("\ntask:", 1)[1] if "\ntask:" in text else ""
        goal = []
        for ln in body.split("===SUTANDO SYSTEM", 1)[0].splitlines():
            if goal and re.match(r"^[a-z_]+: ", ln):
                break  # a task-mid file's headers follow its task line: they are not the goal
            goal.append(ln)
        goal = "\n".join(goal)
        people = [hd.get("sender_name", "")]
        people += [m.split(":", 1)[0].lstrip("@") for m in re.split(r"[,\s]+", hd.get("room_members", "")) if m]
        return goal.strip()[:300], people
    except Exception:  # noqa: BLE001
        return "", []


def _steps(workspace, task_id: str) -> list:
    """The task's newest owner-audience step lines from the activity log, oldest first."""
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
        if isinstance(t, dict) and t.get("id") == task_id and r.get("kind") in ("working", "thinking", "processing") \
                and r.get("audience", "owner") == "owner":
            step = f"[{r['kind']}] {str(r.get('line') or '')[:120]}"
            if not out or out[-1] != step:
                out.append(step)
    return out[-WINDOW:]


def enrich(workspace, task_id: str, call=None) -> "dict | None":
    """The model record written for the task's newest step, or None (the rules record stands)."""
    if not model_on():
        return None
    now = latest(workspace, task_id)
    if now is None:
        return None
    goal, people = _task_context(workspace, task_id)
    red = Redactor(people)
    prompt = ("Task goal: " + red(goal) + "\n\nRecent steps:\n" + "\n".join(red(s) for s in _steps(workspace, task_id))
              + "\n\nJSON:")
    if leaks(prompt):
        return None
    out = (call or gemini)(setting("PHASE_MODEL_PROJECT"), setting("PHASE_MODEL_ID", "gemini-3.5-flash-lite"),
                           SYSTEM, prompt)
    phase = out.get("phase") if isinstance(out, dict) and out.get("phase") in MODEL_PHASES else None
    if phase is None:
        return None
    line = validate_line(out.get("line"), prompt)
    rec = {"task_id": task_id, "step": now.get("step"), "phase": phase, "phase_source": "model"}
    if line:
        rec.update(line=red.restore(line), line_source="model")
    return rec if write(workspace, rec) else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase word and status line for a task's live status.")
    ap.add_argument("cmd", choices=("enrich", "latest"))
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
    print(json.dumps(latest(ws, a.task)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
