#!/usr/bin/env python3
"""Seed <workspace>/state/privacy/public-denylist.txt from this host's real data.

Sources: private repo names of the gh user and its orgs (names only), room names
and IDs from the workspace room stores, and person/agent names and account IDs
from the collaboration-intelligence rosters. The file is local state and must
never be committed; the public-write privacy guard reads it.

Lines below the MANUAL marker are kept across reseeds. Prints counts only, never
terms, so its output is safe to paste anywhere.

Usage: python3 scripts/seed-public-denylist.py [--workspace DIR] [--no-github] [--dry-run]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import public_write_privacy as policy  # noqa: E402

MANUAL = "# --- manual: lines below are kept on reseed ---"
PERSON_FIELDS = ("human", "human_name", "person", "who")
AGENT_FIELDS = ("stand", "stand_name", "display_name", "secondary_agent")
ROOM_FIELDS = ("room", "room_name", "home_room_name")
ACCOUNT_FIELDS = ("discord_id", "stand_discord_id", "discord_handle")
DICT = Path("/usr/share/dict/words")


def _gh(args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {r.stderr.strip()[:200]}")
    return r.stdout


def github_repos():
    """(private names, public names) across the gh user and its orgs."""
    owners = [_gh(["api", "user", "--jq", ".login"]).strip()]
    owners += _gh(["api", "user/orgs", "--paginate", "--jq", ".[].login"]).split()
    private, public = set(), set(owners)
    for owner in owners:
        for vis, bucket in (("private", private), ("public", public)):
            out = _gh(["repo", "list", owner, "--visibility", vis, "--limit", "5000",
                       "--json", "name", "--jq", ".[].name"])
            bucket.update(n for n in out.split() if n)
    return private, public


def _load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def _name_like(value):
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or len(v) > 40 or len(v.split()) > 4 or re.search(r"[(),;!?]|\. |\s—\s", v):
        return None
    return v


def public_tree_tokens(repo: Path):
    """Path components of this public checkout, split on separators: a name the
    public tree already uses as a directory or file name is public vocabulary."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-files"], capture_output=True,
                             text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    tokens = set()
    for line in out.splitlines():
        for comp in line.split("/"):
            stem = comp.rsplit(".", 1)[0].lower()
            tokens.add(stem)
            tokens.update(t for t in re.split(r"[-_.]", stem) if t)
    return tokens


def workspace_terms(ws: Path, public: set):
    terms = []
    rosters = sorted(glob.glob(str(ws / "hosts" / "*" / "data" / "collaboration-intelligence" / "reviewer-stands*.json")))
    rosters += sorted(glob.glob(str(ws / "data" / "collaboration-intelligence" / "reviewer-stands*.json")))
    for path in rosters:
        data = _load(path)
        if not isinstance(data, dict):
            continue
        for entry in data.values():
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("github"), str):
                public.add(entry["github"].strip())
            for fields, cat in ((PERSON_FIELDS, "person"), (AGENT_FIELDS, "agent"),
                                (ROOM_FIELDS, "room"), (ACCOUNT_FIELDS, "account")):
                for f in fields:
                    v = entry.get(f)
                    if cat == "account" and isinstance(v, int):
                        v = str(v)
                    v = _name_like(v)
                    if v:
                        terms.append((cat, v))
    cmap = _load(ws / "data" / "collaboration-intelligence" / "map.json") or {}
    rooms = list(cmap.get("rooms") or []) if isinstance(cmap, dict) else []
    store = _load(ws / "state" / "ag2space-rooms.json") or {}
    rooms += list(store.get("rooms") or []) if isinstance(store, dict) else []
    for room in rooms:
        if isinstance(room, dict):
            if _name_like(room.get("name")):
                terms.append(("room", room["name"].strip()))
            if isinstance(room.get("room_id"), str) and room["room_id"].strip():
                terms.append(("room-id", room["room_id"].strip()))
    peer = _load(ws / "data" / "collaboration-intelligence" / "rooms-from-peer.json") or {}
    for rid, room in ((peer.get("rooms") or {}) if isinstance(peer, dict) else {}).items():
        terms.append(("room-id", str(rid)))
        if isinstance(room, dict) and _name_like(room.get("label")):
            terms.append(("room", room["label"].strip()))
    for ident in (cmap.get("identities") or []) if isinstance(cmap, dict) else []:
        if isinstance(ident, dict) and _name_like(ident.get("entity")):
            terms.append(("person", ident["entity"].strip()))
    return terms


def too_generic(term: str, words) -> bool:
    """A short or single-dictionary-word term would refuse ordinary prose."""
    if len(term) < 4:
        return True
    low = term.lower()
    return low in words or (low.endswith("s") and low[:-1] in words)


def build(ws: Path, use_github: bool):
    words = set()
    if DICT.is_file():
        words = {w.strip().lower() for w in DICT.read_text(errors="ignore").splitlines()}
    stats = {"private_repos": 0, "dropped_public_name": 0, "dropped_generic": 0}
    terms = []
    public = set()
    if use_github:
        private, public = github_repos()
        stats["private_repos"] = len(private)
        terms += [("repo", n) for n in sorted(private)]
    public = set(public)
    terms += workspace_terms(ws, public)
    public_lower = {p.lower() for p in public} | public_tree_tokens(REPO)
    out, seen = [], set()
    for cat, term in terms:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        if key in public_lower:
            stats["dropped_public_name"] += 1
            continue
        if cat not in ("room-id", "account") and too_generic(term, words):
            stats["dropped_generic"] += 1
            continue
        out.append(f"{cat}: {term}")
    return out, stats


def write(path: Path, lines):
    manual = []
    try:
        old = path.read_text(encoding="utf-8").splitlines()
        if MANUAL in old:
            manual = old[old.index(MANUAL) + 1:]
    except FileNotFoundError:
        pass
    body = ["# Generated by scripts/seed-public-denylist.py. Local state: never commit.",
            *lines, MANUAL, *manual]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text("\n".join(body) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return len([m for m in manual if m.strip() and not m.startswith("#")])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace")
    ap.add_argument("--no-github", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    ws = Path(a.workspace) if a.workspace else policy.resolve_workspace_dir()
    try:
        lines, stats = build(ws, not a.no_github)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        print(f"seed-public-denylist: {e}", file=sys.stderr)
        return 2
    target = policy.privacy_dir(ws) / policy.DENYLIST_NAME
    if a.dry_run:
        print(f"would write {len(lines)} generated terms; {json.dumps(stats)}")
        return 0
    kept = write(target, lines)
    print(f"wrote {len(lines)} generated terms (+{kept} manual) to {target}; {json.dumps(stats)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
