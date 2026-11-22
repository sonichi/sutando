#!/usr/bin/env python3
"""The models the core's runtime itself offers, read from its own model picker, by exact name.

A recovery card's Switch buttons are this list and nothing else: the entries the runtime's
picker showed (Claude Code's `/model` menu, Codex's model list), labelled as the runtime labels
them with the model and version they run, minus the one in use and the one that hit its limit.
The executor accepts a switch only to a key in the same list, so the allowlist is the list shown.

The list is recorded whenever the monitor sees the picker on the core's pane (a picker a person
opened, or one this module opens). Only on the owner's Refresh options tap (`request_probe`)
does `probe_step` type `/model` into a core that sits idle after a limit refused its turn; it
reads the picker on the next tick and dismisses it with Escape. Nothing is chosen; no setting
changes.

Record: state/runtime-models.<runtime>.json  {"v": 1, "runtime", "at", "entries": [entry...]}
  entry: {"key", "label", "model", "arg", "current"}
    key    a slug of the runtime's own label, the click id's argument
    label  the runtime's wording, with the model it runs when the row names none ("Haiku 4.5")
    model  the "<Family> <version>" the row runs, else null
    arg    what `scripts/switch-model.sh` sends so the CLI sets that row's model, else null
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_default import status_path, status_read_path, write_status  # noqa: E402

VERSION = 1
MAX_ENTRIES = 12

_PICKER_HEAD = {"claude": re.compile(r"^\s*Select model\s*$", re.M),
                "codex": re.compile(r"^\s*Select Model and Effort\s*$", re.M)}
_ROW = re.compile(r"^\s*(?:[❯›»>]\s*)?(\d{1,2})\.\s+(.+?)\s*$")
_MODEL = re.compile(r"\b(Opus|Sonnet|Haiku|Fable)\s+(\d+(?:\.\d+)*)\b")
_FAMILY = re.compile(r"^(Opus|Sonnet|Haiku|Fable)\b")
_CODEX_ID = re.compile(r"^(gpt-[\w.-]+|o\d[\w.-]*|codex[\w.-]*)")
_KEY_JUNK = re.compile(r"[^a-z0-9]+")


def _key(label: str) -> str:
    return _KEY_JUNK.sub("-", label.lower()).strip("-")[:24] or "model"


def _claude_entry(text: str) -> Dict:
    parts = re.split(r"\s{2,}", text.strip(), maxsplit=1)
    label, desc = parts[0], (parts[1] if len(parts) > 1 else "")
    own = _MODEL.search(label)
    runs = own or _MODEL.search(desc)
    model = f"{runs.group(1)} {runs.group(2)}" if runs else None
    if own or not model:
        shown = label
    elif _FAMILY.match(label):
        shown = model
    else:
        shown = f"{label.split(' (')[0]} ({model})"
    if label.lower().startswith("default"):
        arg = "default"
    elif model:
        fam, ver = runs.group(1).lower(), runs.group(2).replace(".", "-")
        arg = f"claude-{fam}-{ver}" + ("[1m]" if "1m" in label.lower() else "")
    else:
        arg = None
    return {"key": _key(label), "label": shown, "model": model, "arg": arg, "current": False}


def _codex_entry(text: str) -> Dict:
    current = "(current)" in text
    head = text.replace("(current)", "").strip()
    m = _CODEX_ID.match(head)
    name = m.group(1) if m else head.split("  ")[0].strip()
    return {"key": _key(name), "label": name, "model": name, "arg": None, "current": current}


def parse_picker(pane: Optional[str], runtime: Optional[str]) -> List[Dict]:
    """The rows of the runtime's model picker on `pane`, in its order; [] when none is up."""
    head = _PICKER_HEAD.get(runtime or "claude")
    if not pane or head is None:
        return []
    m = head.search(pane)
    if m is None:
        return []
    rows: List[Dict] = []
    for line in pane[m.end():].splitlines():
        hit = _ROW.match(line)
        if hit:
            rows.append((_codex_entry if runtime == "codex" else _claude_entry)(hit.group(2)))
        elif rows and line.strip() and not line.startswith(" " * 6):
            break
    seen, out = set(), []
    for row in rows[:MAX_ENTRIES]:
        if row["key"] not in seen:
            seen.add(row["key"])
            out.append(row)
    return out


def record(workspace: Path, runtime: str, entries: List[Dict], now: Optional[float] = None) -> None:
    """The one writer of a runtime's list."""
    write_status(f"runtime-models.{runtime}.json",
                 {"v": VERSION, "runtime": runtime, "at": time.time() if now is None else now,
                  "entries": entries[:MAX_ENTRIES]}, workspace=Path(workspace))


def read(workspace: Path, runtime: Optional[str]) -> Optional[Dict]:
    if runtime not in _PICKER_HEAD:
        return None
    try:
        data = json.loads(status_read_path(f"runtime-models.{runtime}.json", Path(workspace)).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != VERSION or not isinstance(data.get("entries"), list):
        return None
    return data


def entries(workspace: Path, runtime: Optional[str]) -> List[Dict]:
    data = read(workspace, runtime)
    return [e for e in (data or {}).get("entries") or [] if isinstance(e, dict) and e.get("key")]


def entry(workspace: Path, runtime: Optional[str], key: str) -> Optional[Dict]:
    return next((e for e in entries(workspace, runtime) if e["key"] == key), None)


def _same(row: Dict, model: Optional[str]) -> bool:
    """Whether `row` runs `model` (an alias, a claude-* id or a display name)."""
    if not model:
        return False
    low = str(model).lower().replace("[1m]", "")
    runs = (row.get("model") or "").lower()
    if not runs:
        return low == row.get("key")
    fam, _sp, ver = runs.partition(" ")
    if low in (fam, runs):
        return True
    m = re.match(r"^claude-([a-z]+)-(\d+(?:-\d+)*)", low)
    return bool(m) and m.group(1) == fam and m.group(2).replace("-", ".") == ver


def offer(workspace: Path, runtime: Optional[str], current: Optional[str], limited: Optional[str]) -> List[Dict]:
    """The rows a card may offer as switches: every row the runtime showed that names a model it
    can be switched to, but the one in use and the one that hit its limit."""
    return [e for e in entries(workspace, runtime)
            if e.get("arg") and e.get("arg") != "default" and not _same(e, current) and not _same(e, limited)]


def summary(workspace: Path, runtime: Optional[str], current: Optional[str], limited: Optional[str]) -> Optional[str]:
    """One card line naming every model the runtime offers, the one in use and the capped one marked."""
    rows = entries(workspace, runtime)
    if not rows:
        return None
    names = []
    for e in rows:
        alias = e.get("arg") == "default"  # it follows whatever the runtime defaults to
        marks = [m for m, on in (("in use", e.get("current") or (not alias and _same(e, current))),
                                 ("hit its limit", not alias and _same(e, limited))) if on]
        names.append(e["label"] + (f" ({', '.join(marks)})" if marks else ""))
    return "This runtime offers: " + " · ".join(names) + "."


def _probe_path(workspace: Path, session: str) -> Path:
    return status_path(f"runtime-models-probe.{re.sub(r'[^A-Za-z0-9._-]', '_', session)}.json", Path(workspace))


def request_probe(workspace: Path, session: str, now: Optional[float] = None) -> None:
    """The owner asked for the current options: the next tick opens the picker to read them."""
    write_status(_probe_path(workspace, session).name,
                 {"session": session, "requested_at": time.time() if now is None else now},
                 workspace=Path(workspace))


def probe_step(workspace: Path, session: str, runtime: Optional[str], seat: bool, state: str,
               limit_refused: bool, pane: Optional[str], send, now: Optional[float] = None) -> str:
    """One monitor tick. "ours": the picker this module opened is up; it was read and dismissed,
    and the caller treats the tick as no gate. "read": a picker someone else opened was read.
    "probing": `/model` was typed, on the owner's request only. "": nothing done."""
    at = time.time() if now is None else now
    rt = runtime or "claude"
    rows = parse_picker(pane, rt)
    probe = _probe_path(workspace, session)
    try:
        pending = json.loads(probe.read_text()) if probe.exists() else None
    except (OSError, ValueError):
        pending = None
    if rows:
        record(workspace, rt, rows, at)
        if pending and pending.get("sent_at") and not pending.get("dismissed"):
            send("Escape")
            write_status(probe.name, dict(pending, dismissed=at), workspace=Path(workspace))
            return "ours"
        return "read"
    if not pending or pending.get("sent_at") or not pending.get("requested_at"):
        return ""
    if rt != "claude" or seat or state != "blocked-human" or not limit_refused:
        # Nothing to open here: the pane's own menu is read every tick; the request is spent.
        probe.unlink()
        return ""
    if send(("/model", "Enter")):
        write_status(probe.name, dict(pending, sent_at=at), workspace=Path(workspace))
        return "probing"
    return ""
