"""The text a woken Hatch worker follows, and the tunables, served by the backend or built in.

`wake-context` prints the prompt and worker note from the setup cache the keepawake run
refreshes, else from the copies shipped in the package. A cache that is unreadable, has a
newer text_schema_version, lacks a field, or names any command but the reply command is
not used. Served text never replaces the package's tier notes: they are always printed,
and served tier_addenda come after them. Text only instructs; claims, at-most-once replies
and fail-closed delivery are enforced by the code, which served text cannot reach."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from importlib import resources
from pathlib import Path

from ... import gateway_instance, setup_fetch

TEXT_SCHEMA_VERSION = 1
CACHE_NAME = "hatch-setup.json"
TEXT_FIELDS = ("prompt", "worker_note", "restart_hint")
TIERS = ("owner", "team", "guest")
MAX_TEXT_CHARS = 8192
PLACEHOLDERS = ("<AGENT_MXID>", "<REPLY_COMMAND>", "<ACTIVITY_FILE>")
VENDOR = "muse"
# name: (default, min, max), in seconds.
TUNABLES = {
    "keepawake_interval_s": (15 * 60, 5 * 60, 60 * 60),
    "stuck_task_age_s": (15 * 60, 5 * 60, 2 * 60 * 60),
    "hook_silent_age_s": (5 * 60, 2 * 60, 60 * 60),
    "heartbeat_cadence_s": (5 * 60, 60, 30 * 60),
}


def builtin() -> dict:
    text = resources.files(__package__).joinpath("builtin-runtime-text.json")
    return json.loads(text.read_text(encoding="utf-8"))


def cache_path(state_dir: Path) -> Path:
    return Path(state_dir) / CACHE_NAME


def hint_path(state_dir: Path, instance: str = "") -> Path:
    """What Sparrow recorded from its last heartbeat reply (setup_revision, versions)."""
    return Path(state_dir) / f"setup-hint{gateway_instance.suffix(instance)}.json"


def _read_json(path: Path) -> "dict | None":
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def load_cache(state_dir: Path) -> "dict | None":
    return _read_json(cache_path(state_dir))


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def refresh_cache(state_dir: Path, instance: str = "") -> "dict | None":
    """Fetch the served setup unless the cache already holds the revision Sparrow last
    heard of; a failed fetch keeps the cache. Returns the cache as it now stands."""
    cached = load_cache(state_dir)
    hint = _read_json(hint_path(state_dir, instance)) or {}
    if cached is not None and hint.get("setup_revision") is not None \
            and cached.get("revision") == hint.get("setup_revision"):
        return cached
    fetched = setup_fetch.fetch_setup(kind="acp", vendor=VENDOR)
    if fetched is None:
        return cached
    _write_json(cache_path(state_dir), fetched)
    return fetched


def _commands_ok(text: str) -> bool:
    return all(span.split()[:1] == ["<REPLY_COMMAND>"] for span in re.findall(r"`([^`]+)`", text))


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


def _render(text: str, values: dict) -> str:
    for key in PLACEHOLDERS:
        text = text.replace(key, values.get(key, key))
    return text


def wake_context(cache: "dict | None", values: dict) -> "tuple[str, str]":
    """(the text a woken worker follows, where it came from)."""
    shipped = builtin()
    text, why = served_text(cache)
    source = f"served revision {cache.get('revision')}" if text else f"built-in ({why})"
    text = text or {**{f: shipped[f] for f in TEXT_FIELDS}, "tier_addenda": {}}
    parts = [text["prompt"], "Worker note: " + text["worker_note"], *shipped["tier_notes"]]
    parts += [f"Addendum for {tier} items: {text['tier_addenda'][tier]}"
              for tier in TIERS if (text["tier_addenda"].get(tier) or "").strip()]
    parts.append(text["restart_hint"])
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
