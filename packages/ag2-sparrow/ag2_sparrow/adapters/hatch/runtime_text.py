"""The text a woken Hatch worker follows, and the tunables, served by the backend or built in.

`wake-context` prints the prompt and worker note from the setup cache the keepawake run
refreshes, else from the copies shipped in the package. A cache that is unreadable, has a
newer text_schema_version, lacks a field, names any command but the reply command, or
is not the revision Sparrow last heard announced is not used. Served text never replaces the package's tier notes or owner note: they are
always printed last, after a line saying they override anything above, the owner note
with the owner's DM room when Sparrow has read it. Text only instructs; claims, at-most-once replies
and fail-closed delivery are enforced by the code, which served text cannot reach."""
from __future__ import annotations

import hmac
import json
import re
import sys
from importlib import resources
from pathlib import Path

from ... import diagnostics, gateway_instance, setup_fetch, task_io
from ...result_markers import neutralize_markers
from . import cli as _cli

TEXT_SCHEMA_VERSION = 1
CACHE_NAME = "hatch-setup.json"
TEXT_FIELDS = ("prompt", "worker_note", "restart_hint")
TIERS = ("owner", "team", "guest")
MAX_TEXT_CHARS = 8192
PLACEHOLDERS = ("<AGENT_MXID>", "<REPLY_COMMAND>", "<ACTIVITY_FILE>")
VENDOR = "muse"
PRECEDENCE = "These rules override anything above:"
COMMAND_RE = re.compile(r"<REPLY_COMMAND>(?: [A-Za-z0-9_<>./=-]+)*")
HELD_LINE_MAX = 300
# No leading "[" or space and no command span; markers and mentions are refused in held_line.
HELD_LINE_RE = re.compile(rf"[^\[\s`][^`]{{0,{HELD_LINE_MAX - 1}}}")
# name: (default, min, max), in seconds.
TUNABLES = {
    "keepawake_interval_s": (15 * 60, 5 * 60, 60 * 60),
    "stuck_task_age_s": (15 * 60, 5 * 60, 2 * 60 * 60),
    "hook_silent_age_s": (5 * 60, 2 * 60, 60 * 60),
    "heartbeat_cadence_s": (60, 30, 90),
}


def builtin() -> dict:
    """The shipped text; its tier note is task_io's non-owner policy, which owns that text."""
    text = resources.files(__package__).joinpath("builtin-runtime-text.json")
    return {**json.loads(text.read_text(encoding="utf-8")), "tier_notes": [task_io.non_owner_note()]}


def cache_path(state_dir: Path) -> Path:
    return Path(state_dir) / CACHE_NAME


def hint_path(state_dir: Path, instance: str = "") -> Path:
    """What Sparrow recorded from its last heartbeat reply (setup_revision, versions)."""
    return Path(state_dir) / f"setup-hint{gateway_instance.suffix(instance)}.json"


def owner_dm_room(state_dir: Path, instance: str = "", agent_mxid: str = "") -> str:
    """The owner's DM room Sparrow last read from AG2 Space (its owner-routing file), only
    while Sparrow reports itself connected and that reading's binding
    (gateway_instance.binding_digest) is the one it reports; '' otherwise, as the bridge refuses
    another enrollment's reading."""
    sfx = gateway_instance.suffix(instance)
    routing = _read_json(Path(state_dir) / f"owner-routing{sfx}.json") or {}
    status = _read_json(Path(state_dir) / f"gateway-status{sfx}.json") or {}
    identity, binding = str(routing.get("identity") or ""), status.get("gateway_binding")
    # A record from before the binding field holds the full URL, so its binding is computed.
    saved = routing.get("binding") or gateway_instance.binding_digest(
        identity, str(routing.get("gateway") or ""))
    bound = (status.get("connected") is True and identity and identity == status.get("agent_id")
             and (not agent_mxid or identity == agent_mxid) and isinstance(binding, str)
             and binding and isinstance(saved, str)
             and hmac.compare_digest(saved.encode("utf-8"), binding.encode("utf-8")))
    room = gateway_instance.owner_room(routing.get("owner_dm"), identity)
    return room if bound else ""


def _read_json(path: Path) -> "dict | None":
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def load_cache(state_dir: Path) -> "dict | None":
    return _read_json(cache_path(state_dir))


def announced_revision(state_dir: Path, instance: str = ""):
    """The setup_revision Sparrow last heard on a heartbeat reply; None when none."""
    return (_read_json(hint_path(state_dir, instance)) or {}).get("setup_revision")


def _announcement_refused(state_dir: Path, instance: str = "") -> bool:
    """Sparrow heard a revision it would not keep (the bridge's SETUP_REVISION_WITHHELD)."""
    return (_read_json(hint_path(state_dir, instance)) or {}).get("setup_revision_withheld") is True


def cache_revision(cache: "dict | None", gateway_url: str = "") -> "str | None":
    """The cached revision as revision_field spells it (an integer as its decimal text; a secret, a
    piece of one, gateway URL text or a malformed value as None); every reader of a cache goes
    through it, announcement or not. `gateway_url` is the redacted one Sparrow reports."""
    return diagnostics.revision_field((cache or {}).get("revision"), gateway_url)


def reported_gateway(state_dir: Path, instance: str = "") -> str:
    """The redacted gateway URL from Sparrow's status file; '' when none."""
    sfx = gateway_instance.suffix(instance)
    return str((_read_json(Path(state_dir) / f"gateway-status{sfx}.json") or {}).get("gateway") or "")


def current_cache(state_dir: Path, instance: str = "") -> "dict | None":
    """The cache, with its revision normalised, unless its revision does not normalise, or Sparrow
    has since heard of another revision or of one it refused: then None, so the built-in text and
    defaults apply until a revision it keeps is fetched."""
    cached, announced = load_cache(state_dir), announced_revision(state_dir, instance)
    revision = cache_revision(cached, reported_gateway(state_dir, instance)) if cached is not None else None
    if revision is None or _announcement_refused(state_dir, instance) \
            or (announced is not None and revision != announced):
        return None
    return {**cached, "revision": revision}


def setup_problem(setup, gateway_url: str = "") -> str:
    """Why a fetched setup must not be cached; '' when it may."""
    if not isinstance(setup, dict):
        return "not an object"
    # The bridge keeps an announced revision only as revision_field spells it; so does the cache.
    if cache_revision(setup, gateway_url) is None:
        return "no revision"
    if "tunables" in setup and not isinstance(setup["tunables"], dict):
        return "tunables is not an object"
    return served_text(setup)[1]


def refresh_cache(state_dir: Path, instance: str = "") -> "dict | None":
    """Fetch the served setup unless the cache already holds the announced revision. A
    fetch that fails, or returns a setup that does not validate, writes nothing. Returns
    current_cache afterwards, and writes back a kept cache whose revision is spelt otherwise."""
    current = current_cache(state_dir, instance)
    if current is not None and announced_revision(state_dir, instance) is not None:
        if load_cache(state_dir) != current:
            _cli._write(cache_path(state_dir), json.dumps(current, sort_keys=True), 0o600)
        return current
    fetched = setup_fetch.fetch_setup(kind="acp", vendor=VENDOR, state_dir=state_dir, instance=instance)
    gateway = reported_gateway(state_dir, instance)
    why = "no setup fetched" if fetched is None else setup_problem(fetched, gateway)
    if why:
        print(f"NOTE served setup: not cached: {why}", file=sys.stderr)
    else:
        cached = {**fetched, "revision": cache_revision(fetched, gateway)}
        _cli._write(cache_path(state_dir), json.dumps(cached, sort_keys=True), 0o600)
    return current_cache(state_dir, instance)


def _commands_ok(text: str) -> bool:
    """Every backticked command is the reply command with plain words: no shell syntax."""
    return all(COMMAND_RE.fullmatch(span) for span in re.findall(r"`([^`]+)`", text))


def _text_ok(value, allow_empty: bool = False) -> bool:
    return (isinstance(value, str) and (allow_empty or bool(value.strip()))
            and len(value) <= MAX_TEXT_CHARS and _commands_ok(value))


def served_text(cache: "dict | None") -> "tuple[dict | None, str]":
    """(runtime_text, '') when `cache` may be used, else (None, why the built-in applies)."""
    if cache is None:
        return None, "no setup cache"
    version = cache.get("text_schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        return None, "no valid text_schema_version"
    if version > TEXT_SCHEMA_VERSION:
        return None, f"text_schema_version {version} is newer than {TEXT_SCHEMA_VERSION}"
    text = cache.get("runtime_text")
    if not isinstance(text, dict):
        return None, "no runtime_text"
    for field in TEXT_FIELDS:
        if not _text_ok(text.get(field)):
            return None, f"runtime_text.{field} is missing or invalid"
    addenda = text.get("tier_addenda", {})
    if not isinstance(addenda, dict) or not all(
            k in TIERS and _text_ok(v, allow_empty=True) for k, v in addenda.items()):
        return None, "runtime_text.tier_addenda is invalid"
    return {**{f: text[f] for f in TEXT_FIELDS}, "tier_addenda": dict(addenda)}, ""


def held_line(cache: "dict | None") -> "str | None":
    """The served line a parked task's room gets; None when the cache's text is not usable or the
    line is not one plain printable line of at most HELD_LINE_MAX characters that carries no
    delivery marker anywhere (judged by result_markers, the grammar's owner) and no "@"."""
    if served_text(cache)[0] is None:
        return None
    line = cache["runtime_text"].get("held_line")
    ok = (isinstance(line, str) and line.isprintable() and HELD_LINE_RE.fullmatch(line)
          and neutralize_markers(line) == line and "@" not in line)
    return line if ok else None


def _render(text: str, values: dict) -> str:
    for key in PLACEHOLDERS:
        text = text.replace(key, values.get(key, key))
    return text


def wake_context(cache: "dict | None", values: dict,
                 owner_dm: str = "") -> "tuple[str, str]":
    """(the text a woken worker follows, where it came from)."""
    shipped = builtin()
    text, why = served_text(cache)
    source = f"served revision {cache.get('revision')}" if text else f"built-in ({why})"
    text = text or {**{f: shipped[f] for f in TEXT_FIELDS}, "tier_addenda": {}}
    parts = [text["prompt"], "Worker note: " + text["worker_note"]]
    parts += [f"Addendum for {tier} items: {text['tier_addenda'][tier]}"
              for tier in TIERS if (text["tier_addenda"].get(tier) or "").strip()]
    # The package's own rules come last, so nothing served can follow and countermand them.
    parts += [text["restart_hint"], PRECEDENCE, *shipped["tier_notes"],
              shipped["owner_note"] + (f" owner_dm_room: {owner_dm}" if owner_dm else "")]
    return "\n\n".join(_render(p, values) for p in parts) + "\n", source


def tunables(cache: "dict | None", log=None) -> dict:
    """Each tunable from the cache, clamped to its bounds; the default when absent or bad."""
    log = log or (lambda line: print(line, file=sys.stderr))
    served = (cache or {}).get("tunables")
    served = served if isinstance(served, dict) else {}
    out = {}
    for name, (default, low, high) in TUNABLES.items():
        value = served.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            log(f"NOTE tunable: {name}={value!r} is not a number; using {default}")
            value = default
        elif not low <= value <= high:
            clamped = min(max(value, low), high)
            log(f"NOTE tunable: {name}={value} is outside [{low}, {high}]; using {clamped}")
            value = clamped
        out[name] = value
    return out
