#!/usr/bin/env python3
"""What may not appear in text published where outsiders can read it.

One owner for the public-write privacy policy: the private-term denylist (kept
in the workspace, never in the repo, since listing the terms here would leak
them), the list-free shapes (local home paths, Matrix room IDs and MXIDs), the
allowlist for names that are already public, and repo visibility with a cache.
The PreToolUse guard only parses commands; it asks this module what is private.

Denylist and allowlist live in <workspace>/state/privacy/, one term per line,
optionally prefixed with a category (`repo: name`). Person and agent names match
case-sensitively (common words such as a name that is also a verb would
otherwise fire on prose); every other category matches case-insensitively.
Matching is on alphanumeric word boundaries.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, List, Optional

DENYLIST_NAME = "public-denylist.txt"
ALLOWLIST_NAME = "public-allowlist.txt"
VISIBILITY_CACHE_NAME = "repo-visibility.json"

CATEGORIES = ("repo", "room", "room-id", "agent", "person", "account", "term")
CASE_SENSITIVE = ("agent", "person")

# A PUBLIC answer is cached longer than PRIVATE: a stale PUBLIC only scans a
# private-repo post, a stale PRIVATE would skip the scan after a repo went public.
TTL_SECONDS = {"PUBLIC": 7 * 86400, "PRIVATE": 600}

BUILTIN_PATTERNS = (
    ("local path", re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+(?=/|\b)")),
    # Claude Code's project slug spells the same home path with dashes.
    ("local path", re.compile(r"-Users-[A-Za-z0-9._]+-")),
    ("Matrix room ID", re.compile(r"![A-Za-z0-9._~+/=-]{6,}:[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("Matrix ID", re.compile(r"(?<![\w.])@[A-Za-z0-9._=/+-]+:[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
)


# Documentation placeholders: an example server or a stand-in user name names nobody.
EXAMPLE_SERVER = re.compile(r":(?:[\w-]+\.)*(?:example\.(?:com|org|net)|example|test|localhost|invalid)$", re.I)
PLACEHOLDER_USERS = {"someone", "admin", "user", "username", "name", "you", "me", "alice", "bob",
                     "example", "runner", "ubuntu", "linuxbrew", "shared", "x", "foo", "test"}


def _placeholder(category: str, matched: str) -> bool:
    if category == "local path":
        user = re.split(r"[/-]", matched.strip("/-"))[1:2]
        return bool(user) and user[0].lower() in PLACEHOLDER_USERS
    return bool(EXAMPLE_SERVER.search(matched))


@dataclass(frozen=True)
class Term:
    text: str
    category: str


@dataclass(frozen=True)
class Finding:
    matched: str
    category: str


def privacy_dir(workspace: Path) -> Path:
    return Path(workspace) / "state" / "privacy"


def resolve_workspace_dir() -> Path:
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    from workspace_default import resolve_workspace
    return Path(resolve_workspace(migrate=False))


def parse_terms(text: str) -> List[Term]:
    out: List[Term] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        category = "term"
        head, sep, rest = line.partition(":")
        if sep and head.strip() in CATEGORIES and rest.strip():
            category, line = head.strip(), rest.strip()
        out.append(Term(line, category))
    return out


def load_terms(path: Path) -> Optional[List[Term]]:
    """Terms in the file, [] for an empty file, None when it does not exist."""
    try:
        return parse_terms(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _term_regex(term: Term) -> re.Pattern:
    flags = 0 if term.category in CASE_SENSITIVE else re.IGNORECASE
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term.text) + r"(?![A-Za-z0-9])", flags)


def scan(text: str, deny: Iterable[Term], allow: Iterable[str] = ()) -> List[Finding]:
    """Every private term or list-free shape in `text`, minus allowlisted ones."""
    allowed = {a.lower() for a in allow if a}
    found: List[Finding] = []
    seen = set()

    def add(matched: str, category: str) -> None:
        key = (matched.lower(), category)
        if matched.lower() in allowed or key in seen:
            return
        seen.add(key)
        found.append(Finding(matched, category))

    for term in deny:
        if term.text.lower() in allowed:
            continue
        m = _term_regex(term).search(text)
        if m:
            add(m.group(0), term.category)
    for category, rx in BUILTIN_PATTERNS:
        for m in rx.finditer(text):
            if not _placeholder(category, m.group(0)):
                add(m.group(0), category)
    return found


def own_identities() -> List[str]:
    """Identities this agent must sign with, so this gate never forbids a
    signature the comment-signature guard requires."""
    mxid = os.environ.get("SUTANDO_AGENT_MXID", "").strip()
    return [mxid] if mxid else []


def _gh_visibility(repo: str) -> Optional[str]:
    try:
        r = subprocess.run(["gh", "repo", "view", repo, "--json", "visibility", "-q", ".visibility"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    value = r.stdout.strip().upper()
    return value if r.returncode == 0 and value else None


GITHUB_REMOTE = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$")


def repo_from_remote(cwd) -> Optional[str]:
    """OWNER/NAME gh would target from `cwd` with no --repo: the remote marked
    gh-resolved, else upstream, github, origin, else any GitHub remote."""
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    try:
        r = subprocess.run(["git", "-C", str(cwd), "config", "--get-regexp",
                            r"^remote\..*\.(url|gh-resolved)$"],
                           capture_output=True, text=True, timeout=5, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    urls, resolved = {}, []
    for line in r.stdout.splitlines():
        key, _, value = line.partition(" ")
        name, _, field = key[len("remote."):].rpartition(".")
        if field == "gh-resolved":
            resolved.append(name)
        else:
            urls[name] = value.strip()
    for name in resolved + ["upstream", "github", "origin"] + sorted(urls):
        m = GITHUB_REMOTE.search(urls.get(name, ""))
        if m:
            return f"{m.group(1)}/{m.group(2)}"
    return None


def _write_cache(cache_path: Path, cache: dict) -> None:
    try:
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(cache_path) + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cache, indent=1, sort_keys=True))
        os.replace(tmp, cache_path)
    except OSError:
        pass


def forget_visibility(repo: Optional[str], cache_path: Optional[Path]) -> None:
    """Drop a cached answer for a repo whose visibility a command is changing."""
    if not repo or cache_path is None:
        return
    try:
        cache = json.loads(Path(cache_path).read_text())
    except (OSError, ValueError):
        return
    if isinstance(cache, dict) and cache.pop(repo.lower(), None) is not None:
        _write_cache(cache_path, cache)


def repo_visibility(repo: Optional[str], cache_path: Optional[Path],
                    lookup: Callable[[str], Optional[str]] = _gh_visibility,
                    now: Optional[float] = None) -> Optional[str]:
    """PUBLIC / PRIVATE / INTERNAL, or None when it cannot be established."""
    if not repo or "{" in repo or repo.count("/") != 1:
        return None
    key = repo.lower()
    now = time.time() if now is None else now
    cache = {}
    if cache_path is not None:
        try:
            cache = json.loads(Path(cache_path).read_text())
        except (OSError, ValueError):
            cache = {}
    hit = cache.get(key) if isinstance(cache, dict) else None
    if isinstance(hit, dict) and now - hit.get("at", 0) < TTL_SECONDS.get(hit.get("visibility"), 0):
        return hit["visibility"]
    value = lookup(repo)
    if value and cache_path is not None:
        cache = cache if isinstance(cache, dict) else {}
        cache[key] = {"visibility": value, "at": now}
        _write_cache(cache_path, cache)
    return value
