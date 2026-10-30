"""The Getting started page: an AG2 Space tour as one self-contained HTML document.

Pure: `render_page(name, core)` depends on nothing else. No external URL appears anywhere in the
page (the HTML surface's sandbox may block them), so every graphic is inline SVG. Progress is kept
in the page runtime's `window.artifact.state` under a per-viewer key; when the runtime names no
viewer it uses this browser's localStorage, then memory, and every storage access is guarded.
"""
from __future__ import annotations

import base64
import html
import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

PAGE_TITLE = "Getting started"
NAME_MAX = 64
CORES = ("claude", "codex", "unknown")
CONNECT_PROMPT = "Connect my Google Calendar."
IMPORT_PROMPT = "Import my Claude Code history."


class Section(NamedTuple):
    id: str
    title: str
    what: str
    how: str
    prompt: Optional[str] = None
    where: Optional[str] = None
    images: Tuple[Tuple[str, str], ...] = ()


# `what` and `how` are trusted markup with {name}, the agent's escaped name; `images` are
# (assets/ file, alt). UI labels match the AG2 Space client and its screenshots.
SECTIONS = [
    Section("dm", "Your DM",
            "Your DM with {name} is where you ask for things for yourself. What you ask for "
            "yourself (research, mail, calendar) comes back in your DM, never in a room.",
            "Ask {name} in your DM:",
            "What can you do for me today?", "dm"),
    Section("rooms", "Rooms",
            "Humans and agents talk in the same chat.",
            "Press + at the top of the room list and choose Room. Then open the room menu "
            "(&#8942;), choose Invite, and add {name}.",
            images=(("room-list-add.jpg", "The + button at the top of the room list"),
                    ("new-room-dialog.jpg", "The New Chat Room dialog"),
                    ("room-menu-invite.jpg", "The room menu, with Invite"))),
    Section("commons", "Commons",
            "Each room's shared Docs, HTML pages, PDFs, whiteboards, sheets and databases. Open them "
            "with the Commons icon in the room header.",
            "Ask {name} in a room:",
            "Make a plan for our team offsite in Commons.", "room",
            (("room-header-commons.jpg", "The room header; Commons is the second icon"),
             ("commons-surfaces.jpg", "Commons: Doc, Whiteboard, HTML, PDF, Sheet and Database"))),
    Section("agents", "Connect other agents",
            "Bring agents from the apps you already use into your rooms.",
            "Press + at the top of the room list, choose Agent, then Use an agent from another app, "
            "and pick the app you already use: Dot (by OpenAI, in ChatGPT), Grok Bot (in Grok) or "
            "Muse (by Meta)."),
    Section("teamwork", "Teamwork",
            "@-mention any agent in a room to hand it work; it replies in the thread.",
            "In a room with another agent, type @ and ask {name}:",
            "Research the three best note-taking apps for a small team, then ask another agent "
            "in this room to review your answer.", "room",
            (("composer-mention.jpg", "The @ button in the message composer"),)),
    Section("calls", "Calls &amp; voice",
            "Talk live with {name} in your DM, or with people in any room.",
            "Press the headphone icon in the room header (Start call). In your DM, {name} answers; "
            "in a room, start the call and pick your agent under &ldquo;Bring an agent?&rdquo;.",
            images=(("room-header-call.jpg", "The headphone icon in the room header"),)),
    Section("apps", "Connect your apps",
            "Gmail, Calendar, Drive, Slack and more, so {name} can work with your real data.",
            "Press the plug icon in your user card (bottom left) to open Settings &rarr; "
            "Integrations, or ask {name} and a Connect button appears:",
            CONNECT_PROMPT, "dm",
            (("user-card-plug.jpg", "The plug icon in your user card"),
             ("settings-integrations.jpg", "Settings \u2192 Apps \u2192 Integrations"))),
    Section("activity", "Activity &amp; notices",
            "The bell at the top of the room list and each room's Activity tab show what needs "
            "you and what your agents are working on. The unread badge follows each room's "
            "notification setting.",
            "Try it: open a room's Activity tab while an agent works on something you asked for, "
            "then open the room menu (&#8942;), choose Notifications, and pick Mention &amp; Keywords.",
            images=(("room-list-top.jpg", "The bell at the top of the room list"),)),
    Section("mobile", "Mobile",
            "The same rooms and agents on your phone.",
            "Pair it in Settings &rarr; Mobile pairing.",
            images=(("mobile-pairing.jpg", "Settings: Mobile pairing"),)),
]
IMPORT_SECTION = Section(
    "import", "Import your Claude Code history",
    "If you use Claude Code, {name} can read your local Claude Code history and bring what you "
    "worked on into its memory. You see a digest first; nothing is kept until you say so.",
    "Ask {name}:",
    IMPORT_PROMPT, "dm",
)
FINALE_PROMPT = "Help me plan my week."
# Slots a --prompts file may fill; the slot, not the text, decides where the prompt is pasted.
PROMPT_SLOTS = ("dm", "commons", "teamwork", "apps", "import", "finale")
PROMPT_MAX = 200
BAD_PROMPT = re.compile(r"[\r\n<>]|://|\bwww\.|\b[a-z0-9-]+\.(?:com|net|org|io|ai|dev|app|co|space|edu|gov)\b", re.I)
PICKED_PREFIX = re.compile(r"^\s*I\s+picked\s*:\s*", re.I)
ROLE_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]*")
ROLE_TEXT = re.compile(r"[\w &'.-]{1,40}")
ROLES_MAX = 6
AUDIENCES = ("dm", "room")
WHERE = {"dm": "your DM", "room": "a room"}
ASSETS = Path(__file__).resolve().parent.parent / "assets"  # lint-workspace-resolution: allow-repo-root


# (light, dark) per section; each passes WCAG AA (4.5:1) as text on that theme's card colour.
CARD = ("#ffffff", "#202225")
ACCENTS = {
    "dm": ("#4a5a70", "#b3c2d6"),
    "rooms": ("#1f5fcc", "#7aa7ff"),
    "commons": ("#0b7a5c", "#4fd1a5"),
    "teamwork": ("#7a3fc8", "#b98cff"),
    "calls": ("#b8341e", "#ff8a73"),
    "agents": ("#8f5600", "#f2b24c"),
    "apps": ("#0a6f91", "#5cc8ee"),
    "activity": ("#b0286c", "#ff7ab8"),
    "mobile": ("#3e4dc9", "#98a4ff"),
    "import": ("#566410", "#c3d65a"),
}

_S = 'fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"'
ICONS = {
    "dm": f'<path {_S} d="M10 10h44a4 4 0 0 1 4 4v28a4 4 0 0 1-4 4H30l-12 10V46h-8a4 4 0 0 1-4-4V14a4 4 0 0 1 4-4z"/>'
          f'<circle {_S} cx="32" cy="22" r="5"/><path {_S} d="M22 37c2-6 18-6 20 0"/>',
    "rooms": f'<path {_S} d="M8 12h30a4 4 0 0 1 4 4v14a4 4 0 0 1-4 4H20l-8 7v-7H8a4 4 0 0 1-4-4V16a4 4 0 0 1 4-4z"/>'
             f'<circle {_S} cx="23" cy="20" r="3"/><path {_S} d="M17 29c1.5-3 10.5-3 12 0"/>'
             f'<path {_S} d="M46 24h10a4 4 0 0 1 4 4v14a4 4 0 0 1-4 4h-2v7l-8-7h-8a4 4 0 0 1-4-4v-4"/>'
             f'<rect {_S} x="44" y="31" width="10" height="8" rx="2"/><path {_S} d="M49 31v-3"/>',
    "commons": f'<rect {_S} x="5" y="8" width="24" height="30" rx="3"/><path {_S} d="M11 16h12M11 22h12M11 28h8"/>'
               f'<rect {_S} x="35" y="8" width="24" height="20" rx="3"/><path {_S} d="M40 22c4-8 7 4 10-3s4-2 5-1"/>'
               f'<rect {_S} x="35" y="34" width="24" height="22" rx="3"/><path {_S} d="M35 41h24M35 48h24M43 34v22M51 34v22"/>'
               f'<rect {_S} x="5" y="44" width="24" height="12" rx="3"/>',
    "teamwork": f'<rect {_S} x="4" y="20" width="20" height="18" rx="5"/><circle cx="10" cy="29" r="2" fill="currentColor"/>'
                f'<circle cx="18" cy="29" r="2" fill="currentColor"/><path {_S} d="M14 20v-5"/>'
                f'<rect {_S} x="40" y="26" width="20" height="18" rx="5"/><circle cx="46" cy="35" r="2" fill="currentColor"/>'
                f'<circle cx="54" cy="35" r="2" fill="currentColor"/><path {_S} d="M50 26v-5"/>'
                f'<path {_S} d="M26 46c6 8 14 8 18 2"/><path {_S} d="M40 44l4 4 3-5"/>'
                f'<rect {_S} x="27" y="10" width="12" height="9" rx="2"/>',
    "calls": f'<path {_S} d="M14 8l8 2 2 9-5 4a32 32 0 0 0 14 14l4-5 9 2 2 8c-1 4-4 6-8 6C24 48 10 34 8 18c0-4 2-8 6-10z"/>'
             f'<path {_S} d="M44 10v10M50 6v18M56 12v6"/>',
    "agents": f'<circle {_S} cx="12" cy="14" r="6"/><circle {_S} cx="52" cy="14" r="6"/><circle {_S} cx="12" cy="50" r="6"/>'
              f'<circle {_S} cx="52" cy="50" r="6"/><path {_S} d="M17 17l8 8M47 17l-8 8M17 47l8-8M47 47l-8-8"/>'
              f'<rect {_S} x="24" y="24" width="16" height="16" rx="4"/><path {_S} d="M29 24v-4M35 24v-4"/>',
    "apps": f'<rect {_S} x="6" y="6" width="22" height="22" rx="6"/><rect {_S} x="36" y="6" width="22" height="22" rx="6"/>'
            f'<rect {_S} x="6" y="36" width="22" height="22" rx="6"/><path {_S} d="M47 38v18M38 47h18"/>'
            f'<path {_S} d="M11 14l6 5 6-5"/><path {_S} d="M41 13h12M41 19h8"/><path {_S} d="M11 50l6-9 6 9z"/>',
    "activity": f'<path {_S} d="M14 44V28a14 14 0 0 1 28 0v16l4 5H10z"/><path {_S} d="M23 53a5 5 0 0 0 10 0"/>'
                f'<circle cx="46" cy="16" r="9" fill="currentColor"/>',
    "mobile": f'<rect {_S} x="18" y="4" width="28" height="56" rx="6"/><path {_S} d="M28 52h8"/>'
              f'<path {_S} d="M24 16h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-9l-5 4v-4h-2a2 2 0 0 1-2-2V18a2 2 0 0 1 2-2z"/>',
    "import": f'<path {_S} d="M6 22a4 4 0 0 1 4-4h12l5 5h27a4 4 0 0 1 4 4v24a4 4 0 0 1-4 4H10a4 4 0 0 1-4-4z"/>'
              f'<path {_S} d="M32 4v26M24 22l8 8 8-8"/>',
}

HERO = (
    '<svg class="hero-art" viewBox="0 0 200 120" aria-hidden="true" focusable="false">'
    '<circle cx="100" cy="60" r="56" fill="var(--hero-ring)"/>'
    f'<g style="color:var(--a-rooms)"><circle {_S} cx="66" cy="46" r="12"/>'
    f'<path {_S} d="M44 92c3-18 41-18 44 0"/></g>'
    f'<g style="color:var(--a-teamwork)"><rect {_S} x="114" y="36" width="34" height="28" rx="8"/>'
    '<circle cx="124" cy="50" r="3" fill="currentColor"/><circle cx="138" cy="50" r="3" fill="currentColor"/>'
    f'<path {_S} d="M131 36v-8"/><circle cx="131" cy="25" r="3" fill="currentColor"/>'
    f'<path {_S} d="M112 92c2-16 36-16 38 0"/><path {_S} d="M150 60l10-8"/></g>'
    f'<g style="color:var(--a-agents)"><path {_S} d="M92 26l3-7 3 7 7 3-7 3-3 7-3-7-7-3z"/></g>'
    f'<g style="color:var(--a-activity)"><path {_S} d="M168 26l2-4 2 4 4 2-4 2-2 4-2-4-4-2z"/></g>'
    '</svg>'
)


def clean_name(name: str) -> str:
    """The agent's display name, single-spaced; refuses an empty or over-long one."""
    cleaned = " ".join((name or "").split())
    if not cleaned or len(cleaned) > NAME_MAX:
        raise ValueError(f"the agent's name must be 1-{NAME_MAX} characters, got {name!r}")
    return cleaned


def sections_for(core: str) -> List[Section]:
    """The page's sections; the import one only on a Claude Code core."""
    if core not in CORES:
        raise ValueError(f"core must be one of {', '.join(CORES)}, got {core!r}")
    return SECTIONS + ([IMPORT_SECTION] if core == "claude" else [])


def _luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        v = c / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _data_uri(filename: str) -> str:
    return "data:image/jpeg;base64," + base64.b64encode((ASSETS / filename).read_bytes()).decode("ascii")


def _prompt(key: str, prompt: str, where: Optional[str]) -> str:
    esc = html.escape(prompt, quote=True)
    hint = f'<p class="paste">Paste in: {WHERE[where]}</p>' if where else ""
    return (f'<div class="prompt"><code id="p-{key}">{html.escape(prompt)}</code>'
            f'<button type="button" class="copy" data-prompt="{esc}" aria-describedby="p-{key}">Copy</button>'
            f'</div>{hint}')


def _shots(images) -> str:
    if not images:
        return ""
    figs = "".join(f'<img src="{_data_uri(f)}" alt="{html.escape(alt, quote=True)}" loading="lazy">'
                   for f, alt in images)
    return f'<div class="shots">{figs}</div>'


def clean_prompts(raw: object) -> Dict[str, str]:
    """The usable per-slot prompts from `raw`: known slots only, each a single line of 1-200
    characters with no link or markup. Anything else is dropped, so its slot keeps the default."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for slot, text in raw.items():
        if slot not in PROMPT_SLOTS or not isinstance(text, str):
            continue
        text = text.strip()
        if 0 < len(text) <= PROMPT_MAX and not BAD_PROMPT.search(text):
            out[slot] = text
    return out


def role_labels(roles: object) -> List[str]:
    """Display labels from the first-contact "I picked: ..." payload, as given: a comma-separated
    string (or a list) of persona labels, or slugs when the client had no labels. A slug such as
    `product-manager` reads `Product Manager`; a label keeps its own words. Junk is dropped."""
    if isinstance(roles, str):
        roles = PICKED_PREFIX.sub("", roles, count=1).split(",")
    if not isinstance(roles, (list, tuple)):
        return []
    out: Dict[str, str] = {}
    for raw in roles:
        if not isinstance(raw, str):
            continue
        text = " ".join(raw.split())
        text = " ".join(text[:-1].split()) if text.endswith(".") else text
        if not ROLE_TEXT.fullmatch(text) or not any(ch.isalnum() for ch in text):
            continue
        if ROLE_SLUG.fullmatch(text):
            text = " ".join(w.capitalize() for w in re.split(r"[-_]", text) if w)
        out.setdefault(text.casefold(), text)
    return list(out.values())[:ROLES_MAX]


def _section(sec: Section, first: bool, who: str, prompts: Dict[str, str]) -> str:
    prompt = _prompt(sec.id, prompts.get(sec.id, sec.prompt), sec.where) if sec.prompt else ""
    return (
        f'<section class="sec card{" cur" if first else ""}" id="s-{sec.id}" data-id="{sec.id}" '
        f'aria-labelledby="h-{sec.id}" style="--accent:var(--a-{sec.id})">'
        f'<div class="icon" aria-hidden="true"><svg viewBox="0 0 64 64" focusable="false">{ICONS[sec.id]}</svg></div>'
        f'<div class="body"><h2 id="h-{sec.id}">{sec.title}</h2>'
        f'<p>{sec.what.format(name=who)}</p><p class="how">{sec.how.format(name=who)}</p>{prompt}'
        f'{_shots(sec.images)}'
        f'<label class="done"><input type="checkbox" data-done="{sec.id}" disabled> Mark done</label>'
        '</div></section>'
    )


def _finale(who: str, prompts: Dict[str, str]) -> str:
    return (
        '<section class="card finale" id="finale" aria-labelledby="h-finale" hidden '
        'style="--accent:var(--brand)"><div class="body">'
        f'<h2 id="h-finale" tabindex="-1">You&rsquo;re set &mdash; ask {who} anything</h2>'
        f'<p class="how">That was the whole tour. Start with something real:</p>'
        f'{_prompt("finale", prompts.get("finale", FINALE_PROMPT), "dm")}</div></section>'
    )


def _palette() -> tuple:
    light = "".join(f"--a-{k}:{v[0]};" for k, v in ACCENTS.items())
    dark = ("--bg:#151618; --fg:#ececec; --muted:#a9adb3; --card:#202225; --line:#34373b; --brand:#7aa7ff; "
            "--code:#2b2d31; --hero-ring:#1d2433; --track:#34373b; "
            + "".join(f"--a-{k}:{v[1]};" for k, v in ACCENTS.items()))
    return light, dark


STYLE = """<style>
:root { color-scheme: light dark; --bg:#f7f6f3; --fg:#1d1d1f; --muted:#5a5e63; --card:#ffffff; --line:#e3e1dc;
  --brand:#1f5fcc; --code:#f2f1ee; --hero-ring:#eaf0fb; --track:#e6e4df; %(light)s }
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) { %(darkvars)s }
}
:root[data-theme="dark"] { color-scheme: dark; %(darkvars)s }
:root[data-theme="light"] { color-scheme: light; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
main { max-width:760px; margin:0 auto; padding:24px 16px 48px; }
.hero { display:flex; gap:16px; align-items:center; margin-bottom:16px; }
.hero-art { width:160px; flex:none; }
h1 { font-size:28px; line-height:1.2; margin:0 0 6px; }
h2 { font-size:19px; margin:0 0 4px; }
p { margin:0 0 6px; }
.lead, .how { color:var(--muted); }
.roles { font-size:14px; font-weight:600; color:var(--brand); margin:6px 0 0; }
.progress { margin:4px 0 16px; }
.progress-label { font-size:14px; color:var(--muted); margin-bottom:4px; }
.bar { height:8px; border-radius:99px; background:var(--track); overflow:hidden; }
.bar > span { display:block; height:100%%; width:0; background:var(--brand); border-radius:99px; transition:width .3s ease; }
.controls { display:none; }
.js .controls { display:flex; flex-wrap:wrap; gap:10px 8px; align-items:center; margin:0 0 12px; }
.dots { display:flex; flex-wrap:wrap; gap:6px; flex-basis:100%%; }
#all { margin-left:auto; }
#all[aria-pressed="true"] { background:var(--brand); color:var(--card); }
.dot { width:28px; height:28px; padding:0; border-radius:50%%; font-size:12px; border:2px solid var(--line); color:var(--muted); background:var(--card); }
.dot[aria-current="step"] { border-color:var(--brand); color:var(--fg); }
.dot.is-done { background:var(--brand); border-color:var(--brand); color:var(--card); }
.card[hidden] { display:none; }
.card { background:var(--card); border:1px solid var(--line); border-top:4px solid var(--accent); border-radius:12px; padding:16px; margin:0 0 12px; display:flex; gap:16px; }
.icon { flex:none; width:72px; height:72px; border-radius:16px; color:var(--accent); position:relative; display:grid; place-items:center; }
.icon::before { content:""; position:absolute; inset:0; border-radius:inherit; background:currentColor; opacity:.12; }
.icon svg { width:52px; height:52px; position:relative; }
.body { min-width:0; flex:1; }
.step-title { flex-basis:100%%; margin:0; font-weight:600; }
.pos { font-weight:400; color:var(--muted); margin-left:6px; font-size:14px; }
.progress, .done { display:none; }
.js .progress { display:block; }
.js main[data-mode="all"] .step-title { display:none; }
.paste { font-size:13px; color:var(--muted); margin:4px 0 0; }
.shots { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; align-items:flex-start; }
.shots img { max-width:100%%; height:auto; border:1px solid var(--line); border-radius:8px; }
.finale { border-top-color:var(--brand); }
.finale h2 { font-size:21px; }
.prompt { display:flex; gap:8px; align-items:center; margin-top:8px; }
code { flex:1; min-width:0; background:var(--code); border-radius:6px; padding:6px 8px; font-size:14px; overflow-wrap:anywhere; user-select:all; }
button { flex:none; border:1px solid var(--brand); color:var(--brand); background:transparent; border-radius:8px; padding:6px 12px; font:inherit; font-size:14px; cursor:pointer; }
button:hover:not(:disabled) { background:var(--brand); color:var(--card); }
button:disabled { opacity:.45; cursor:default; }
button:focus-visible, input:focus-visible { outline:3px solid var(--brand); outline-offset:2px; }
.copy { border-color:var(--accent); color:var(--accent); }
.copy:hover { background:var(--accent); color:var(--card); }
.js .done { display:inline-flex; gap:8px; align-items:center; margin-top:10px; font-size:14px; cursor:pointer; }
.done input { width:18px; height:18px; accent-color:var(--accent); }
.js main[data-mode="step"] .sec { display:none; }
.js main[data-mode="step"] .sec.cur { display:flex; animation:enter .22s ease-out; }
.sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0 0 0 0); white-space:nowrap; border:0; }
.done input:disabled { opacity:.5; }
@keyframes enter { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:none; } }
@media (prefers-reduced-motion: reduce) {
  .js main[data-mode="step"] .sec.cur { animation:none; }
  .bar > span { transition:none; }
}
@media (max-width: 520px) {
  .hero { flex-direction:column; align-items:flex-start; }
  .hero-art { width:120px; }
  .card { flex-direction:column; gap:10px; }
  .icon { width:56px; height:56px; }
  .icon svg { width:40px; height:40px; }
}
</style>"""

# Storage: the page runtime's state under a per-viewer key when the runtime names the viewer, else
# this browser's localStorage, else memory. Every access is guarded: the sandbox may refuse it.
SCRIPT = """<script>
(() => {
  const main = document.getElementById('tour');
  const secs = Array.from(main.querySelectorAll('section.sec'));
  const ids = secs.map((s) => s.dataset.id);
  const titles = secs.map((s) => s.querySelector('h2').textContent);
  const boxes = secs.map((s) => s.querySelector('input[data-done]'));
  const dotsBox = document.getElementById('dots');
  const back = document.getElementById('back');
  const next = document.getElementById('next');
  const all = document.getElementById('all');
  const label = document.getElementById('progress-label');
  const fill = document.getElementById('progress-fill');
  const bar = document.getElementById('progress-bar');
  const stepTitle = document.getElementById('step-title');
  const stepPos = document.getElementById('step-pos');
  const theme = document.body.getAttribute('data-theme');
  if ((theme === 'dark' || theme === 'light') && !document.documentElement.hasAttribute('data-theme')) {
    document.documentElement.setAttribute('data-theme', theme);
  }
  const finale = document.getElementById('finale');
  const done = new Set();
  let cur = 0;
  let store = { kind: 'memory', key: 'gs-done' };

  const art = (() => { try { return window.artifact && window.artifact.state ? window.artifact : null; } catch (e) { return null; } })();
  const local = (() => { try { const s = window.localStorage; s.setItem('gs-probe', '1'); s.removeItem('gs-probe'); return s; } catch (e) { return null; } })();
  const reduce = (() => { try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; } })();
  const valid = (v) => Array.isArray(v) ? v.filter((x) => ids.includes(x)) : [];
  const settle = async (v) => (v && typeof v.then === 'function' ? await v : v);

  const pickStore = async () => {
    if (art) {
      try {
        await Promise.race([art.ready, new Promise((r) => setTimeout(r, 1500))]);
        const id = art.me && art.me.id;
        if (typeof id === 'string' && id) return { kind: 'state', key: 'gs-done-' + id };
      } catch (e) { /* no viewer id: never the shared key */ }
    }
    return local ? { kind: 'local', key: 'gs-done' } : { kind: 'memory', key: 'gs-done' };
  };
  const load = async () => {
    if (store.kind === 'state') {
      try { return valid(await settle(art.state.get(store.key))); } catch (e) { return []; }
    }
    if (store.kind === 'local') {
      try { return valid(JSON.parse(local.getItem(store.key) || '[]')); } catch (e) { return []; }
    }
    return [];
  };
  const save = async () => {
    const v = ids.filter((x) => done.has(x));
    if (store.kind === 'state') { try { await art.state.set(store.key, v); } catch (e) { /* memory only */ } }
    if (store.kind === 'local') { try { local.setItem(store.key, JSON.stringify(v)); } catch (e) { /* memory only */ } }
  };

  const reveal = (i) => {
    const sec = secs[i];
    try { sec.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' }); } catch (e) { /* old engine */ }
    try { sec.querySelector('h2').focus({ preventScroll: true }); } catch (e) { sec.querySelector('h2').focus(); }
  };
  const dots = ids.map((id, i) => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'dot'; b.textContent = String(i + 1);
    b.addEventListener('click', () => (main.dataset.mode === 'step' ? show(i, true) : (cur = i, paint(), reveal(i))));
    dotsBox.appendChild(b); return b;
  });

  const paint = () => {
    const n = done.size;
    label.textContent = n + ' of ' + ids.length + ' done';
    fill.style.width = Math.round((n / ids.length) * 100) + '%';
    bar.setAttribute('aria-valuenow', String(n));
    bar.setAttribute('aria-valuetext', label.textContent);
    secs.forEach((s, i) => { boxes[i].checked = done.has(ids[i]); s.classList.toggle('cur', i === cur); });
    dots.forEach((d, i) => {
      d.classList.toggle('is-done', done.has(ids[i]));
      d.setAttribute('aria-label', 'Step ' + (i + 1) + ': ' + titles[i] + (done.has(ids[i]) ? ' (done)' : ''));
      if (i === cur) d.setAttribute('aria-current', 'step'); else d.removeAttribute('aria-current');
    });
    const stepping = main.dataset.mode === 'step';
    stepTitle.textContent = titles[cur];
    stepPos.textContent = (cur + 1) + '/' + ids.length;
    finale.hidden = n !== ids.length;
    back.hidden = next.hidden = !stepping;
    back.disabled = cur === 0;
    next.disabled = cur === ids.length - 1;
    all.setAttribute('aria-pressed', stepping ? 'false' : 'true');
  };
  const show = (i, focus) => {
    cur = Math.max(0, Math.min(ids.length - 1, i));
    paint();
    if (focus && main.dataset.mode === 'step') secs[cur].querySelector('h2').focus();
  };

  secs.forEach((s) => s.querySelector('h2').setAttribute('tabindex', '-1'));
  back.addEventListener('click', () => show(cur - 1, true));
  next.addEventListener('click', () => show(cur + 1, true));
  all.addEventListener('click', () => { main.dataset.mode = main.dataset.mode === 'step' ? 'all' : 'step'; paint(); });
  main.addEventListener('change', (ev) => {
    const id = ev.target && ev.target.getAttribute && ev.target.getAttribute('data-done');
    if (!id) return;
    if (ev.target.checked) done.add(id); else done.delete(id);
    save(); paint();
    if (!ev.target.checked || main.dataset.mode !== 'step') return;
    const after = ids.map((_, k) => (cur + 1 + k) % ids.length).find((k) => !done.has(ids[k]));
    if (after === undefined) { finale.querySelector('h2').focus(); return; }
    show(after, true);
  });
  document.addEventListener('keydown', (ev) => {
    if (main.dataset.mode !== 'step' || ev.altKey || ev.metaKey || ev.ctrlKey) return;
    const t = ev.target;
    if (!t || !main.contains(t) || t.isContentEditable) return;
    if (/^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(t.tagName)) return;
    if (ev.key === 'ArrowRight') { show(cur + 1, true); ev.preventDefault(); }
    if (ev.key === 'ArrowLeft') { show(cur - 1, true); ev.preventDefault(); }
  });

  main.dataset.mode = 'step';
  paint();
  (async () => {
    store = await pickStore();
    (await load()).forEach((id) => done.add(id));
    const first = ids.findIndex((id) => !done.has(id));
    show(first < 0 ? 0 : first, false);
    boxes.forEach((b) => { b.disabled = false; });
    if (store.kind === 'state') {
      try { art.state.on(store.key, async (val) => { const v = valid(await settle(val)); done.clear(); v.forEach((id) => done.add(id)); paint(); }); } catch (e) { /* no live sync */ }
    }
  })();
})();
</script>"""

# The frame is sandboxed to allow-scripts; the Clipboard API may be refused there, so the prompt
# is also selected for a manual copy, and the outcome is announced in the status region.
COPY_SCRIPT = """<script>
(() => {
  const status = document.getElementById('status');
  const plat = (() => { try { return (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || ''; } catch (e) { return ''; } })();
  const keys = /mac|iphone|ipad/i.test(plat) ? 'Press \\u2318C' : (plat ? 'Press Ctrl+C' : 'Press Ctrl/\\u2318+C');
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
    btn.textContent = ok ? 'Copied' : keys;
    const said = ok ? 'Copied: ' + text : 'Prompt selected. ' + keys + ' to copy it.';
    status.textContent = '';
    setTimeout(() => { status.textContent = said; }, 60);
    setTimeout(() => { btn.textContent = 'Copy'; }, 2500);
  });
})();
</script>"""

# Set before the body paints, so a scripted page opens on one step instead of flashing all of them.
HEAD_SCRIPT = "<script>document.documentElement.classList.add('js');</script>"


def owner_marker(agent: str) -> str:
    """The tag that marks a page as this agent's tutorial page, so a later run can recognise it."""
    return f'<meta name="sutando-tutorial" content="{html.escape(agent, quote=True)}">'


def render_page(name: str, core: str = "unknown", audience: str = "dm", agent: Optional[str] = None,
                prompts: object = None, roles: object = None) -> str:
    """The whole page as one HTML string; depends on nothing but its arguments: the agent's name,
    core, audience, mxid (the ownership marker), per-slot prompts (`clean_prompts`) and the owner's
    onboarding roles. In a room other than the owner's DM the agent is "your agent"."""
    prompts = clean_prompts(prompts)
    labels = role_labels(roles)
    picked = (f'<p class="roles">Picked for: {html.escape(", ".join(labels))}</p>' if labels else "")
    if audience not in AUDIENCES:
        raise ValueError(f"audience must be one of {', '.join(AUDIENCES)}, got {audience!r}")
    who = html.escape(clean_name(name)) if audience == "dm" else "your agent"
    secs = sections_for(core)
    body = "".join(_section(s, i == 0, who, prompts) for i, s in enumerate(secs))
    light, dark = _palette()
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"{owner_marker(agent) if agent else ''}"
        f"<title>{PAGE_TITLE}</title>{HEAD_SCRIPT}{STYLE % {'light': light, 'darkvars': dark}}</head><body>"
        "<main id=\"tour\" data-mode=\"step\">"
        f"<header class=\"hero\">{HERO}<div><h1>Getting started with {who}</h1>"
        f"<p class=\"lead\">A short tour of s.space. Each part has one thing to try; Copy puts "
        f"a prompt on your clipboard to paste into a chat with {who}.</p>{picked}</div></header>"
        "<div class=\"progress\"><div class=\"progress-label\" id=\"progress-label\" aria-hidden=\"true\">"
        f"0 of {len(secs)} done</div>"
        f"<div class=\"bar\" id=\"progress-bar\" role=\"progressbar\" aria-label=\"Tour progress\" "
        f"aria-valuemin=\"0\" aria-valuemax=\"{len(secs)}\" aria-valuenow=\"0\"><span id=\"progress-fill\"></span></div></div>"
        "<nav class=\"controls\" id=\"controls\" aria-label=\"Tour steps\">"
        "<div class=\"dots\" id=\"dots\"></div>"
        f"<p class=\"step-title\"><span id=\"step-title\">{secs[0].title}</span>"
        f"<span class=\"pos\" id=\"step-pos\" aria-hidden=\"true\">1/{len(secs)}</span></p>"
        "<button type=\"button\" id=\"back\" aria-label=\"Previous step\">Back</button>"
        "<button type=\"button\" id=\"next\" aria-label=\"Next step\">Next</button>"
        "<button type=\"button\" id=\"all\" aria-pressed=\"false\">Show all</button></nav>"
        f"{_finale(who, prompts)}{body}<div id=\"status\" class=\"sr-only\" role=\"status\" aria-live=\"polite\"></div>"
        f"</main>{SCRIPT}{COPY_SCRIPT}</body></html>\n"
    )
