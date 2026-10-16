#!/usr/bin/env python3
"""The owner's "Getting started" page in AG2 Space: render it, and create or refresh it in one room.

  getting_started.py render --name NAME [--core claude|codex|unknown|auto]   -> the page's HTML
  getting_started.py plan --room ROOM                   -> {"action": "create"|"update", "kind": ...}
  getting_started.py publish --room ROOM --name NAME [--owner MXID] [--core ...] [--summon]
      [--room-commons PATH]
      -> {"ok": true, "action": "created"|"updated"|"unchanged", "kind": "html-<id>", "summoned": bool}

The page is a short AG2 Space tour: eight sections, plus a Claude Code history import section when
the core is Claude Code (`--core auto` asks the engine's config resolver; unknown omits it).

One page per room. The marker at <workspace>/state/tutorial/getting-started.json records the page's
kind per room, so a re-link or reinstall rewrites that page instead of adding a second one. The page
holds the agent's name and fixed text only; nothing read from the owner's accounts goes on it.

Exit codes: 0 done, 2 bad arguments, 3 the room-commons skill is not installed, 4 a room-commons
call failed (the marker is unchanged; an empty page this run added is removed again).
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import html
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

PAGE_TITLE = "Getting started"
NAME_MAX = 64
SUMMON_CONTEXT = "A short tour of AG2 Space, with one thing to try in each part."
DEFAULT_ROOM_COMMONS = REPO / "skills" / "room-commons" / "scripts" / "room_commons.py"

CORES = ("claude", "codex", "unknown")
CONNECT_PROMPT = "Connect my Google Calendar."
IMPORT_PROMPT = "Import my Claude Code history."

# (title, what it is, how to try it, prompt or None). {name} is the agent's escaped name;
# UI labels match the AG2 Space client.
SECTIONS = [
    ("Rooms",
     "Humans and agents talk in the same chat.",
     "Create a room with the + next to Rooms, then invite {name}.",
     None),
    ("Commons",
     "Each room's shared Doc, HTML pages, whiteboard, sheets and databases, under Commons in "
     "the room header.",
     "Ask {name} in a room:",
     "Make a plan for our team offsite in Commons."),
    ("Teamwork",
     "@-mention any agent in a room to hand it work; it replies in the thread.",
     "In a room with another agent, ask {name}:",
     "Research the three best note-taking apps for a small team, then ask another agent in "
     "this room to review your answer."),
    ("Calls &amp; voice",
     "Talk live in any room or in your DM with {name}; agents join the call.",
     "Press the call button in the room header (Start call).",
     None),
    ("Connect other agents",
     "Bring agents from other apps into your rooms.",
     "Choose + then Agent to open Add an agent, pick Use an agent from another app, then Dot "
     "(in ChatGPT), Grok Bot (in Grok), Muse or Other (any app with connectors).",
     None),
    ("Connect your apps",
     "Gmail, Calendar, Drive, Slack and more, so {name} can work with your real data.",
     "Open Settings &rarr; Integrations, or ask {name} and a Connect button appears:",
     CONNECT_PROMPT),
    ("Activity &amp; notices",
     "Each room's Activity tab shows what needs you and what your agents are working on.",
     "The unread badge follows the room's notification setting (All Messages, Mention &amp; "
     "Keywords or Mute), which you can change per room.",
     None),
    ("Mobile",
     "The same rooms and agents on your phone.",
     "Pair it in Settings &rarr; Mobile pairing.",
     None),
]
IMPORT_SECTION = (
    "Import your Claude Code history",
    "Bring what you worked on in Claude Code into {name}'s memory. You see a digest first; "
    "nothing is kept until you say so.",
    "Ask {name}:",
    IMPORT_PROMPT,
)


class PublishError(RuntimeError):
    pass


def clean_name(name: str) -> str:
    """The agent's display name, single-spaced; refuses an empty or over-long one."""
    cleaned = " ".join((name or "").split())
    if not cleaned or len(cleaned) > NAME_MAX:
        raise ValueError(f"the agent's name must be 1-{NAME_MAX} characters, got {name!r}")
    return cleaned


def _section(title: str, what: str, how: str, prompt: Optional[str], who: str) -> str:
    """One section; the text is trusted markup, only the name and the prompt are escaped."""
    body = f"<h2>{title}</h2><p>{what.format(name=who)}</p><p class=\"how\">{how.format(name=who)}</p>"
    if prompt:
        body += (f'<div class="prompt"><code>{html.escape(prompt)}</code>'
                 f'<button type="button" data-prompt="{html.escape(prompt, quote=True)}">Copy</button></div>')
    return f'<section class="card">{body}</section>'


def sections_for(core: str) -> list:
    """The page's sections; the import one only on a Claude Code core."""
    if core not in CORES:
        raise ValueError(f"core must be one of {', '.join(CORES)}, got {core!r}")
    return SECTIONS + ([IMPORT_SECTION] if core == "claude" else [])


# The frame is sandboxed to allow-scripts; the Clipboard API may be refused there, so the prompt
# is also selected for a manual copy.
COPY_SCRIPT = """<script>
(() => {
  const pick = (el) => { const r = document.createRange(); r.selectNodeContents(el);
    const s = getSelection(); s.removeAllRanges(); s.addRange(r); };
  const legacy = (text) => { const t = document.createElement('textarea'); t.value = text;
    t.setAttribute('readonly', ''); t.style.position = 'fixed'; t.style.opacity = '0';
    document.body.appendChild(t); t.select(); let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    t.remove(); return ok; };
  document.addEventListener('click', async (ev) => {
    const btn = ev.target.closest && ev.target.closest('button[data-prompt]');
    if (!btn) return;
    const text = btn.getAttribute('data-prompt');
    let ok = false;
    try { if (navigator.clipboard) { await navigator.clipboard.writeText(text); ok = true; } } catch (e) { ok = false; }
    if (!ok) ok = legacy(text);
    if (!ok) pick(btn.parentElement.querySelector('code'));
    btn.textContent = ok ? 'Copied' : 'Press \\u2318C';
    setTimeout(() => { btn.textContent = 'Copy'; }, 2500);
  });
})();
</script>"""

STYLE = """<style>
:root { --bg:#fbfaf8; --fg:#1d1d1f; --muted:#5f6368; --card:#ffffff; --line:#e3e1dc; --accent:#3b5bdb; --code:#f2f1ee; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#17181a; --fg:#ececec; --muted:#a3a7ad; --card:#202225; --line:#33363a; --accent:#8ea2ff; --code:#2a2c30; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width:760px; margin:0 auto; padding:32px 16px 48px; }
h1 { font-size:28px; margin:0 0 8px; }
h2 { font-size:17px; margin:0 0 4px; }
p { margin:0 0 6px; }
.lead, .how { color:var(--muted); }
.lead { margin-bottom:20px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin:0 0 12px; }
.prompt { display:flex; gap:8px; align-items:center; margin-top:8px; }
code { flex:1; min-width:0; background:var(--code); border-radius:6px; padding:6px 8px; font-size:14px; overflow-wrap:anywhere; user-select:all; }
button { flex:none; border:1px solid var(--accent); color:var(--accent); background:transparent; border-radius:6px; padding:6px 12px; font:inherit; font-size:14px; cursor:pointer; }
button:hover { background:var(--accent); color:var(--bg); }
</style>"""


def render_page(name: str, core: str = "unknown") -> str:
    """The whole page as one HTML string; depends on nothing but the agent's name and core."""
    who = html.escape(clean_name(name))
    body = "".join(_section(*sec, who=who) for sec in sections_for(core))
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{PAGE_TITLE}</title>{STYLE}</head><body><main>"
        f"<h1>Getting started with {who}</h1>"
        f"<p class=\"lead\">A short tour of AG2 Space. Each part has one thing to try; Copy puts "
        f"a prompt on your clipboard to paste into a chat with {who}.</p>"
        f"{body}</main>{COPY_SCRIPT}</body></html>\n"
    )


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def marker_path(workspace: Path) -> Path:
    return workspace / "state" / "tutorial" / "getting-started.json"


def load_marker(path: Path) -> Dict[str, dict]:
    """{room: {"kind", "hash", "updated_at"}}; an unreadable marker reads as no pages."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rooms = data.get("rooms") if isinstance(data, dict) else None
    if not isinstance(rooms, dict):
        return {}
    return {r: v for r, v in rooms.items()
            if isinstance(v, dict) and isinstance(v.get("kind"), str) and v["kind"].startswith("html-")}


def save_marker(path: Path, rooms: Dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".getting-started.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "rooms": rooms}, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, str(path))


def plan(rooms: Dict[str, dict], room: str, listed_kinds: Optional[List[str]] = None) -> dict:
    """Create unless this room already has a page; a recorded page no longer listed is created anew."""
    entry = rooms.get(room)
    if entry and (listed_kinds is None or entry["kind"] in listed_kinds):
        return {"action": "update", "kind": entry["kind"]}
    return {"action": "create", "kind": None}


@contextlib.contextmanager
def _locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / ".getting-started.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


class Commons:
    """The room-commons CLI, one call per subprocess."""

    def __init__(self, script: Path, room: str):
        self.script, self.room = script, room

    def run(self, *args: str, kind: Optional[str] = None, as_json: bool = False) -> str:
        argv = [sys.executable, str(self.script)]
        if as_json:
            argv.append("--json")
        if kind:
            argv += ["--kind", kind]
        argv += list(args)
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            raise PublishError(f"room-commons {args[0]} timed out") from None
        if done.returncode != 0:
            raise PublishError(f"room-commons {args[0]} failed ({done.returncode}): "
                               f"{(done.stderr or done.stdout).strip()[:400]}")
        return done.stdout

    def listed_kinds(self) -> List[str]:
        pages = json.loads(self.run("pages", self.room, as_json=True))
        return [p["kind"] for p in pages if isinstance(p, dict) and p.get("id")]

    def add_page(self, title: str) -> str:
        kind = json.loads(self.run("page-add", self.room, title, as_json=True)).get("kind")
        if not isinstance(kind, str) or not kind.startswith("html-"):
            raise PublishError(f"room-commons page-add returned no html page kind: {kind!r}")
        return kind

    def read(self, kind: str) -> str:
        return json.loads(self.run("read", self.room, kind=kind, as_json=True)).get("text") or ""


def publish(commons: Commons, marker: Path, name: str, owner: Optional[str] = None,
            summon: bool = False, core: str = "unknown") -> dict:
    page = render_page(name, core)
    with _locked(marker):
        rooms = load_marker(marker)
        step = plan(rooms, commons.room, commons.listed_kinds() if commons.room in rooms else None)
        if step["action"] == "update":
            kind = step["kind"]
            current = commons.read(kind)
            if current == page:
                action = "unchanged"
            else:
                if current:
                    commons.run("replace", commons.room, current, page, kind=kind)
                else:
                    commons.run("append", commons.room, page, kind=kind)
                action = "updated"
        else:
            kind = commons.add_page(PAGE_TITLE)
            try:
                commons.run("append", commons.room, page, kind=kind)
            except PublishError as exc:
                with contextlib.suppress(PublishError):
                    commons.run("page-remove", commons.room, kind)
                raise PublishError(f"{exc}; the empty page was taken off the list again") from None
            action = "created"
        rooms[commons.room] = {"kind": kind, "hash": content_hash(page),
                               "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        save_marker(marker, rooms)
    result = {"ok": True, "action": action, "kind": kind, "summoned": False}
    if owner and (summon or action == "created"):
        try:
            commons.run("summon", commons.room, owner, "--context", SUMMON_CONTEXT,
                        "--page-title", PAGE_TITLE, kind=kind)
            result["summoned"] = True
        except PublishError as exc:
            result["summon_error"] = str(exc)
    return result


def detect_core() -> str:
    """The core runtime the engine resolves (launcher stamp, else config); unknown on failure."""
    try:
        from sutando_config import resolve_core_runtime
        core = resolve_core_runtime()
    except Exception:  # noqa: BLE001
        return "unknown"
    return core if core in CORES else "unknown"


def _workspace() -> Path:
    from workspace_default import resolve_workspace
    return resolve_workspace()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="getting_started.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    r = sub.add_parser("render", help="print the page's HTML")
    r.add_argument("--name", required=True, help="the agent's own name, from its AG2 Space identity")
    r.add_argument("--core", choices=CORES + ("auto",), default="unknown",
                   help="the Sutando core; claude adds the Claude Code import section")
    p = sub.add_parser("plan", help="what publish would do in this room, from the marker alone")
    p.add_argument("--room", required=True)
    u = sub.add_parser("publish", help="create the page in this room, or refresh the one already there")
    u.add_argument("--room", required=True, help="the owner's DM room id")
    u.add_argument("--name", required=True, help="the agent's own name, from its AG2 Space identity")
    u.add_argument("--owner", help="the owner's mxid; summoned to the page when it is created")
    u.add_argument("--core", choices=CORES + ("auto",), default="auto",
                   help="the Sutando core (default auto: what the engine resolves); claude adds the "
                        "Claude Code import section, unknown leaves it out")
    u.add_argument("--summon", action="store_true", help="summon the owner on a refresh too")
    u.add_argument("--room-commons", type=Path, default=DEFAULT_ROOM_COMMONS,
                   help="path to room_commons.py (default: the installed room-commons skill)")
    args = ap.parse_args(argv)

    try:
        if args.command == "render":
            core = detect_core() if args.core == "auto" else args.core
            sys.stdout.write(render_page(args.name, core))
            return 0
        if args.command == "plan":
            print(json.dumps(plan(load_marker(marker_path(_workspace())), args.room)))
            return 0
        clean_name(args.name)
    except ValueError as exc:
        print(f"getting_started: {exc}", file=sys.stderr)
        return 2
    if not args.room_commons.is_file():
        print(json.dumps({"ok": False, "reason": "room-commons not installed",
                          "path": str(args.room_commons)}))
        return 3
    try:
        result = publish(Commons(args.room_commons, args.room), marker_path(_workspace()),
                         args.name, args.owner, args.summon,
                         detect_core() if args.core == "auto" else args.core)
    except (PublishError, ValueError) as exc:
        print(json.dumps({"ok": False, "reason": str(exc)}))
        return 4
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
