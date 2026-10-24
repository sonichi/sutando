"""The Getting started page: an AG2 Space tour as one self-contained HTML document.

The steps come from a feature list, `feature-list.json` beside this skill's scripts (or one passed
in): `{schema_version, revision, steps[{id, title, body[what, how], surface, screenshot, since_release,
paste?, runtime?}]}`, the owner-step shape AG2 Space's backend serves. `parse_list` validates it and
`render_page(name, core, ..., features, media)` renders it; the page records the list's `revision` in
its ownership meta tag, so a later run can tell whether its list is newer. Each screenshot is an
asset reference `{asset, fallback_file, alt}`: when `media` maps the asset name to the room's
`mxc://` URI (room media, uploaded with room.media.upload) the image points there, else the
fallback file under assets/ is inlined as a data: URI. Pure: rendering depends on nothing but its
arguments. No external URL appears anywhere in the page (the HTML surface's sandbox may block
them), so every graphic is inline SVG and every image room media or a data: URI. Progress is kept
in the page runtime's `window.artifact.state` under a per-viewer key; when the runtime names no
viewer it uses this browser's localStorage, then memory, and every storage access is guarded.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import stat
import string
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

PAGE_TITLE = "Getting started"
NAME_MAX = 64
CORES = ("claude", "codex", "unknown")
CONNECT_PROMPT = "Connect my Google Calendar."
IMPORT_PROMPT = "Import my Claude Code history."
SCHEMA_VERSION = 1
LIST_PATH = Path(__file__).resolve().parent.parent / "feature-list.json"  # lint-workspace-resolution: allow-repo-root
# A revision ends in the list's date and a sequence number, so two revisions order by (date, n).
REVISION = re.compile(r"^tutorial\.(\d{4}-\d{2}-\d{2})\.(\d+)$")
STEP_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
ASSET_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
FALLBACK_FILE = re.compile(r"^assets/([a-z0-9][a-z0-9-]*\.jpg)$")
# Room media as room-commons' room_pdf.py accepts it from room.media.upload.
MXC = re.compile(r"^mxc://[A-Za-z0-9][A-Za-z0-9.-]{0,253}(?::\d{1,5})?/[A-Za-z0-9_-]{1,255}$")
RELEASE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SURFACES = ("dm", "room", "mobile")
RUNTIMES = ("claude", "codex")
BODY_PARTS = 2
STEPS_MAX = 24
LIST_MAX_BYTES = 1_048_576  # a list from any source, file or URL
# A fallback-mode page inlines every screenshot; Room Commons refuses a page much larger.
PAGE_MAX_BYTES = 300_000


class Shot(NamedTuple):
    """One screenshot: the room-media asset name, the assets/ file inlined when the room has no
    such media, and the alt text."""
    asset: str
    file: str
    alt: str


class Section(NamedTuple):
    """One step as the page renders it. `what` and `how` are trusted markup with {name}, the
    agent's escaped name; `runtime` limits the step to one core."""
    id: str
    title: str
    what: str
    how: str
    prompt: Optional[str] = None
    where: Optional[str] = None
    images: Tuple[Shot, ...] = ()
    runtime: Optional[str] = None
    since_release: str = ""


class FeatureList(NamedTuple):
    revision: str
    steps: Tuple[Section, ...]


FINALE_PROMPT = "Help me plan my week."
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
DEFAULT_ACCENT = ("#4a5a70", "#b3c2d6")  # a step the list adds before this module has art for it

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
DEFAULT_ICON = f'<path {_S} d="M32 6l6 16 16 6-16 6-6 16-6-16-16-6 16-6z"/>'

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


def revision_key(revision: object) -> Optional[Tuple[str, int]]:
    """(date, n) for a well-formed revision such as `tutorial.2026-10-23.1`; None otherwise."""
    m = REVISION.match(revision) if isinstance(revision, str) else None
    return (m.group(1), int(m.group(2))) if m else None


def newer(revision: str, than: Optional[str]) -> bool:
    """Whether `revision` is newer than `than`: a page with no readable revision counts as older
    than any list; a list with no readable revision is never newer."""
    mine = revision_key(revision)
    return mine is not None and (revision_key(than) is None or mine > revision_key(than))


def _no_tags(text: str, where: str) -> str:
    # The page interpolates these as markup; entities (&rarr;) say everything a list needs.
    if "<" in text or ">" in text:
        raise ValueError(f"{where}: '<' and '>' are not allowed; write entities such as &rarr;")
    return text


def _text(part: object, where: str) -> str:
    if isinstance(part, dict) and "href" not in part:
        part = part.get("text")
    if isinstance(part, dict):
        raise ValueError(f"{where}: a link is not allowed; the page carries no external URL")
    if not isinstance(part, str) or not part.strip():
        raise ValueError(f"{where}: a body part must be a non-empty string")
    _no_tags(part, where)
    try:
        fields = [(f, spec, conv) for _, f, spec, conv in string.Formatter().parse(part)]
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}; a literal brace is written {{{{ or }}}}") from None
    for field, spec, conv in fields:
        if field is not None and (field != "name" or spec or conv):
            raise ValueError(f"{where}: {{name}} is the only placeholder allowed, got {{{field}}}")
    return part


def _step(raw: object, n: int, assets: Path) -> Section:
    where = f"steps[{n}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: not an object")
    sid = raw.get("id")
    if not isinstance(sid, str) or not STEP_ID.match(sid):
        raise ValueError(f"{where}: id must match {STEP_ID.pattern}, got {sid!r}")
    where = f"step {sid}"
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"{where}: title must be a non-empty string")
    _text(title, f"{where} title")
    body = raw.get("body")
    if not isinstance(body, list) or len(body) != BODY_PARTS:
        raise ValueError(f"{where}: body must list {BODY_PARTS} parts (what it is, what to try)")
    what, how = (_text(part, f"{where} body[{i}]") for i, part in enumerate(body))
    paste = raw.get("paste")
    if paste is not None and (not isinstance(paste, str) or not paste.strip()):
        raise ValueError(f"{where}: paste must be a non-empty string or null")
    surface = raw.get("surface")
    allowed = tuple(WHERE) if paste else SURFACES
    if surface not in allowed:
        raise ValueError(f"{where}: surface must be one of {', '.join(allowed)}, got {surface!r}")
    shots = raw.get("screenshot")
    images = []
    for i, shot in enumerate(shots if isinstance(shots, list) else [] if shots is None else [shots]):
        at = f"{where} screenshot[{i}]"
        if not isinstance(shot, dict):
            raise ValueError(f"{at}: must be an object with asset, fallback_file and alt")
        asset, fallback, alt = shot.get("asset"), shot.get("fallback_file"), shot.get("alt")
        if not isinstance(asset, str) or not ASSET_NAME.match(asset):
            raise ValueError(f"{at}: asset must be a bare lowercase name, got {asset!r}")
        m = FALLBACK_FILE.match(fallback) if isinstance(fallback, str) else None
        if not m:
            raise ValueError(f"{at}: fallback_file must be assets/<name>.jpg, got {fallback!r}")
        if not isinstance(alt, str) or not alt.strip():
            raise ValueError(f"{at}: alt text is required")
        if not (assets / m.group(1)).is_file():
            raise ValueError(f"{at}: {fallback} is not under {assets}")
        images.append(Shot(asset, m.group(1), alt))
    since = raw.get("since_release")
    if not isinstance(since, str) or not RELEASE.match(since):
        raise ValueError(f"{where}: since_release must be YYYY-MM-DD, got {since!r}")
    runtime = raw.get("runtime")
    if runtime is not None and runtime not in RUNTIMES:
        raise ValueError(f"{where}: runtime must be one of {', '.join(RUNTIMES)} or absent, got {runtime!r}")
    return Section(sid, title, what, how, paste, surface, tuple(images), runtime, since)


def parse_list(data: object, assets: Optional[Path] = None) -> FeatureList:
    """The feature list in `data` (a parsed JSON document), validated: schema_version 1, a
    well-formed revision, and at least one step with a unique id, a two-part body, a known surface,
    screenshots that exist under `assets` with alt text, a release date and an optional runtime.
    Anything else raises ValueError naming the step and field, so a bad list never renders."""
    assets = ASSETS if assets is None else assets
    if not isinstance(data, dict):
        raise ValueError("the feature list must be a JSON object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"schema_version must be {SCHEMA_VERSION}, got {data.get('schema_version')!r}")
    revision = data.get("revision")
    if revision_key(revision) is None:
        raise ValueError(f"revision must look like tutorial.YYYY-MM-DD.N, got {revision!r}")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("steps must be a non-empty list")
    if len(raw_steps) > STEPS_MAX:
        raise ValueError(f"steps must list at most {STEPS_MAX} steps, got {len(raw_steps)}")
    steps = tuple(_step(raw, n, assets) for n, raw in enumerate(raw_steps))
    ids = [s.id for s in steps]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise ValueError(f"duplicate step id(s): {', '.join(dup)}")
    if "finale" in ids:
        raise ValueError("finale is the closing card's slot, not a step id")
    assets_named = [shot.asset for s in steps for shot in s.images]
    dup = sorted({a for a in assets_named if assets_named.count(a) > 1})
    if dup:
        raise ValueError(f"duplicate asset name(s): {', '.join(dup)}")
    if all(s.runtime for s in steps):
        raise ValueError("at least one step must apply to every core")
    return FeatureList(revision, steps)


def read_regular(path: Path, cap: int, what: str = "file") -> bytes:
    """At most `cap` bytes of a regular file, from one descriptor opened without blocking (a
    writer-less FIFO would hang a plain open) and checked before the read, so no special file
    blocks or gets past the cap. ValueError names what went wrong."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError(f"cannot read the {what} {path}: {exc.strerror or exc}") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"the {what} {path} is not a regular file")
    with os.fdopen(fd, "rb") as fh:
        try:
            raw = fh.read(cap + 1)
        except OSError as exc:
            raise ValueError(f"cannot read the {what} {path}: {exc.strerror or exc}") from None
    if len(raw) > cap:
        raise ValueError(f"the {what} {path} is over the {cap}-byte cap")
    return raw


def load_list(path: Optional[Path] = None) -> FeatureList:
    """The feature list in a JSON file; the skill's own `feature-list.json` by default."""
    path = LIST_PATH if path is None else Path(path)
    raw = read_regular(path, LIST_MAX_BYTES, "feature list")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ValueError(f"the feature list {path} is not JSON: {exc}") from None
    return parse_list(data)


def asset_names(features: FeatureList) -> Tuple[str, ...]:
    """The room-media asset names the list's screenshots refer to, in page order."""
    return tuple(shot.asset for s in features.steps for shot in s.images)


def clean_media(raw: object, features: Optional[FeatureList] = None) -> Dict[str, str]:
    """{asset name: mxc URI} from `raw`: only names the list refers to, only well-formed `mxc://`
    URIs. Anything else is dropped, so that asset keeps its inlined fallback."""
    if not isinstance(raw, dict):
        return {}
    names = asset_names(features or FEATURES)
    return {k: v for k, v in raw.items() if k in names and isinstance(v, str) and MXC.match(v)}


def prompt_slots(features: FeatureList) -> Tuple[str, ...]:
    """Slots a --prompts file may fill: each step with a paste prompt, then the closing card. The
    slot, not the text, decides where the prompt is pasted."""
    return tuple(s.id for s in features.steps if s.prompt) + ("finale",)


def sections_for(core: str, features: Optional[FeatureList] = None) -> List[Section]:
    """The page's sections: every step of the list that names no runtime, plus those naming this
    core (the Claude Code import step only on a Claude Code core)."""
    if core not in CORES:
        raise ValueError(f"core must be one of {', '.join(CORES)}, got {core!r}")
    return [s for s in (features or FEATURES).steps if s.runtime in (None, core)]


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


def _shots(images: Tuple[Shot, ...], media: Dict[str, str]) -> str:
    if not images:
        return ""
    figs = "".join(f'<img src="{media.get(shot.asset) or _data_uri(shot.file)}" '
                   f'alt="{html.escape(shot.alt, quote=True)}" loading="lazy">' for shot in images)
    return f'<div class="shots">{figs}</div>'


def clean_prompts(raw: object, slots: Optional[Tuple[str, ...]] = None) -> Dict[str, str]:
    """The usable per-slot prompts from `raw`: known slots only, each a single line of 1-200
    characters with no link or markup. Anything else is dropped, so its slot keeps the default."""
    if not isinstance(raw, dict):
        return {}
    slots = PROMPT_SLOTS if slots is None else slots
    out = {}
    for slot, text in raw.items():
        if slot not in slots or not isinstance(text, str):
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


def _section(sec: Section, first: bool, who: str, prompts: Dict[str, str], media: Dict[str, str]) -> str:
    prompt = _prompt(sec.id, prompts.get(sec.id, sec.prompt), sec.where) if sec.prompt else ""
    icon = ICONS.get(sec.id, DEFAULT_ICON)
    return (
        f'<section class="sec card{" cur" if first else ""}" id="s-{sec.id}" data-id="{sec.id}" '
        f'aria-labelledby="h-{sec.id}" style="--accent:var(--a-{sec.id})">'
        f'<div class="icon" aria-hidden="true"><svg viewBox="0 0 64 64" focusable="false">{icon}</svg></div>'
        f'<div class="body"><h2 id="h-{sec.id}">{sec.title.format(name=who)}</h2>'
        f'<p>{sec.what.format(name=who)}</p><p class="how">{sec.how.format(name=who)}</p>{prompt}'
        f'{_shots(sec.images, media)}'
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


def _palette(extra: Tuple[str, ...] = ()) -> tuple:
    """The accent variables: one per known step, then `extra` (step ids without their own accent)
    on the default accent."""
    light = "".join(f"--a-{k}:{v[0]};" for k, v in ACCENTS.items())
    light += "".join(f"--a-{k}:{DEFAULT_ACCENT[0]};" for k in extra)
    dark = ("--bg:#151618; --fg:#ececec; --muted:#a9adb3; --card:#202225; --line:#34373b; --brand:#7aa7ff; "
            "--code:#2b2d31; --hero-ring:#1d2433; --track:#34373b; "
            + "".join(f"--a-{k}:{v[1]};" for k, v in ACCENTS.items())
            + "".join(f"--a-{k}:{DEFAULT_ACCENT[1]};" for k in extra))
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

FEATURES = load_list()
# The bundled list's steps for every core, and the slots its paste prompts open.
SECTIONS = [s for s in FEATURES.steps if s.runtime is None]
PROMPT_SLOTS = prompt_slots(FEATURES)


PAGE_TAIL = f"</main>{SCRIPT}{COPY_SCRIPT}</body></html>\n"
MARKER = re.compile(r'<meta name="sutando-tutorial" content="([^"]*)"(?: data-revision="([^"]*)")?'
                    r'(?: data-digest="([0-9a-f]{64})")?>')
DIGEST_SLOT = "f" * 64
DIGEST_ATTR = re.compile(r' data-digest="[0-9a-f]{64}"')
PAGE_HEAD = ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
             "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")


def owner_marker(agent: str, revision: Optional[str] = None, digest: Optional[str] = None) -> str:
    """The tag that marks a page as this agent's tutorial page, so a later run can recognise it,
    and records the feature-list revision it was built from and the page's own digest."""
    rev = f' data-revision="{html.escape(revision, quote=True)}"' if revision else ""
    dig = f' data-digest="{digest}"' if digest else ""
    return f'<meta name="sutando-tutorial" content="{html.escape(agent, quote=True)}"{rev}{dig}>'


def strip_digest(text: str) -> str:
    """`text` with the tag's digest attribute removed: the form a page took before tags carried one."""
    return DIGEST_ATTR.sub("", text, count=1)


def page_shape_ok(text: str) -> bool:
    """Whether `text` begins and ends exactly as this renderer writes a page and carries one
    ownership tag: a page with anything added before, after or around the tag fails."""
    return text.startswith(PAGE_HEAD + "<meta name=\"sutando-tutorial\"") and text.endswith(PAGE_TAIL) \
        and len(MARKER.findall(text)) == 1 and text.count("<!doctype") == 1 and text.count("</html>") == 1


def page_digest_ok(text: str) -> Optional[bool]:
    """Whether a page is exactly as this renderer wrote it: its tag's digest is the sha256 of the
    page with that digest blanked. None when the page carries no digest (an older page)."""
    m = MARKER.search(text or "")
    if not m or not m.group(3):
        return None
    slotted = text[:m.start(3)] + DIGEST_SLOT + text[m.end(3):]
    return hashlib.sha256(slotted.encode("utf-8")).hexdigest() == m.group(3)


def read_marker(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """(agent, revision) from a page's ownership tag; revision is None on a page built before lists
    had one; None when the page carries no tag."""
    m = MARKER.search(text or "")
    if not m:
        return None
    return html.unescape(m.group(1)), (html.unescape(m.group(2)) if m.group(2) else None)


def render_page(name: str, core: str = "unknown", audience: str = "dm", agent: Optional[str] = None,
                prompts: object = None, roles: object = None,
                features: Optional[FeatureList] = None, media: object = None) -> str:
    """The whole page as one HTML string; depends on nothing but its arguments: the agent's name,
    core, audience, mxid (the ownership marker, which also records the list's revision), per-slot
    prompts (`clean_prompts`), the owner's onboarding roles, the feature list (the skill's own by
    default) and the room's media map (`clean_media`; an asset it does not map is inlined). In a
    room other than the owner's DM the agent is "your agent"."""
    features = features or FEATURES
    prompts = clean_prompts(prompts, prompt_slots(features))
    media = clean_media(media, features)
    labels = role_labels(roles)
    picked = (f'<p class="roles">Picked for: {html.escape(", ".join(labels))}</p>' if labels else "")
    if audience not in AUDIENCES:
        raise ValueError(f"audience must be one of {', '.join(AUDIENCES)}, got {audience!r}")
    who = html.escape(clean_name(name)) if audience == "dm" else "your agent"
    secs = sections_for(core, features)
    body = "".join(_section(s, i == 0, who, prompts, media) for i, s in enumerate(secs))
    light, dark = _palette(tuple(s.id for s in features.steps if s.id not in ACCENTS))
    page = (
        f"{PAGE_HEAD}"
        f"{owner_marker(agent, features.revision, DIGEST_SLOT) if agent else ''}"
        f"<title>{PAGE_TITLE}</title>{HEAD_SCRIPT}{STYLE % {'light': light, 'darkvars': dark}}</head><body>"
        "<main id=\"tour\" data-mode=\"step\">"
        f"<header class=\"hero\">{HERO}<div><h1>Getting started with {who}</h1>"
        f"<p class=\"lead\">A short tour of AG2 Space. Each part has one thing to try; Copy puts "
        f"a prompt on your clipboard to paste into a chat with {who}.</p>{picked}</div></header>"
        "<div class=\"progress\"><div class=\"progress-label\" id=\"progress-label\" aria-hidden=\"true\">"
        f"0 of {len(secs)} done</div>"
        f"<div class=\"bar\" id=\"progress-bar\" role=\"progressbar\" aria-label=\"Tour progress\" "
        f"aria-valuemin=\"0\" aria-valuemax=\"{len(secs)}\" aria-valuenow=\"0\"><span id=\"progress-fill\"></span></div></div>"
        "<nav class=\"controls\" id=\"controls\" aria-label=\"Tour steps\">"
        "<div class=\"dots\" id=\"dots\"></div>"
        f"<p class=\"step-title\"><span id=\"step-title\">{secs[0].title.format(name=who)}</span>"
        f"<span class=\"pos\" id=\"step-pos\" aria-hidden=\"true\">1/{len(secs)}</span></p>"
        "<button type=\"button\" id=\"back\" aria-label=\"Previous step\">Back</button>"
        "<button type=\"button\" id=\"next\" aria-label=\"Next step\">Next</button>"
        "<button type=\"button\" id=\"all\" aria-pressed=\"false\">Show all</button></nav>"
        f"{_finale(who, prompts)}{body}<div id=\"status\" class=\"sr-only\" role=\"status\" aria-live=\"polite\"></div>"
        f"{PAGE_TAIL}"
    )
    if agent:  # the tag's digest covers the whole page with the digest itself blanked
        page = page.replace(f'data-digest="{DIGEST_SLOT}"',
                            f'data-digest="{hashlib.sha256(page.encode("utf-8")).hexdigest()}"', 1)
    size = len(page.encode("utf-8"))
    if size > PAGE_MAX_BYTES:
        raise ValueError(f"the page is {size} bytes, over the {PAGE_MAX_BYTES}-byte cap: fewer or smaller "
                         "inlined screenshots, or map them to room media")
    return page
