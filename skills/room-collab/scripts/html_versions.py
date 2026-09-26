"""Named snapshots of an HTML page, kept in the page's own document.

Y.Map `versions`: `<vid>` -> {name, created, by, size, auto}; Y.Map
`version_texts`: `<vid>` -> the page source. The same caps as the web client's
htmlVersions.ts, so a page versioned from either side reads the same.
"""
from __future__ import annotations

import re
import secrets
import time

VERSIONS_KEY = "versions"
VERSION_TEXTS_KEY = "version_texts"
VERSION_MAX_BYTES = 2 * 1024 * 1024
VERSION_CAP = 50
NAME_MAX = 80
ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"
VERSION_ID_RE = re.compile(r"[a-z0-9]{10}")


def new_version_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(10))


def clean_name(name: object) -> str:
    return " ".join(str(name).split())[:NAME_MAX] if isinstance(name, str) else ""


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def read_versions(index: object) -> list[dict]:
    """The listed versions, newest first; malformed entries are skipped."""
    if not isinstance(index, dict):
        return []
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0  # noqa: E731
    out = []
    for vid, v in index.items():
        if not isinstance(vid, str) or not VERSION_ID_RE.fullmatch(vid) or not isinstance(v, dict):
            continue
        out.append({"id": vid, "name": clean_name(v.get("name")) or "Untitled version",
                    "created": num(v.get("created")),
                    "by": v.get("by")[:255] if isinstance(v.get("by"), str) else "",
                    "size": num(v.get("size")), "auto": v.get("auto") is True})
    return sorted(out, key=lambda e: (-e["created"], e["id"]))


class VersionRefused(ValueError):
    """A snapshot that does not fit; the message is for the caller verbatim."""


def plan_snapshot(versions: list[dict], text: str, name: str, by: str,
                  now_ms: int | None = None, auto: bool = False) -> tuple[dict, list[str]]:
    """The entry to write and the automatic snapshots to prune for it, or VersionRefused."""
    size = len(text.encode("utf-8"))
    if size > VERSION_MAX_BYTES:
        raise VersionRefused(f"the page is too large to version ({format_size(size)}; "
                             f"the limit is {format_size(VERSION_MAX_BYTES)})")
    clean = clean_name(name)
    if not clean:
        raise VersionRefused("a version needs a name")
    over = len(versions) + 1 - VERSION_CAP
    autos = sorted((v for v in versions if v["auto"]), key=lambda v: v["created"])
    if over > len(autos):
        raise VersionRefused(f"the page already keeps {VERSION_CAP} named versions; none can be added")
    prune = [v["id"] for v in autos[:max(0, over)]]
    created = int(time.time() * 1000) if now_ms is None else now_ms
    return {"name": clean, "created": created, "by": by, "size": size, "auto": auto}, prune


def before_restore_name(now: float | None = None) -> str:
    return "Before restore " + time.strftime("%Y-%m-%d %H:%M", time.localtime(now))


def find_version(versions: list[dict], ref: str) -> dict | None:
    """A version by id, else by name (case-insensitive; the newest of equal names)."""
    for v in versions:
        if v["id"] == ref:
            return v
    want = clean_name(ref).casefold()
    return next((v for v in versions if v["name"].casefold() == want), None)
