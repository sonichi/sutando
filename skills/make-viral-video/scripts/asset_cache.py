#!/usr/bin/env python3
"""Shared asset cache for make-viral-video skill.

Chi 2026-05-10: "Make existing assets available." Currently each re-run
gets its own state/viral-{ts}/fetched_assets/ — Apollo image + NASA
portraits get re-fetched every time. This module backs a shared
state/viral-cache/fetched_assets/ that any run can preload from + promote
new fetches into.

Schema:
- Cache dir: $REPO/state/viral-cache/fetched_assets/<basename>
- Key: URL basename (post-querystring stripping). Collision-resistant
  enough for our scale (<1000 unique URLs); no SHA needed yet.
- Manifest sidecar: $REPO/state/viral-cache/index.json
  Maps URL → {"local_file": str, "fetched_ts": int, "size_bytes": int}

CLI:
  python3 asset_cache.py preload <run_dir> [--manifest <path>]
    Reads run_dir/artifacts/asset_manifest.json (or --manifest) and copies
    cache hits into run_dir/fetched_assets/. Returns count of hits.

  python3 asset_cache.py promote <run_dir>
    Copies all files in run_dir/fetched_assets/ to the cache (URL inferred
    from manifest entries; un-manifested files keyed by basename only).

  python3 asset_cache.py list
    Prints URL → local_file mapping from the cache index.

API (when imported):
  cache_get(url) -> Optional[Path]   # cached path or None
  cache_put(url, local_path)          # copy local_path to cache
"""
import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional


REPO = Path(__file__).resolve().parents[3]
CACHE_DIR = REPO / "state" / "viral-cache" / "fetched_assets"
INDEX_FILE = REPO / "state" / "viral-cache" / "index.json"


def _ensure_cache_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _load_index() -> dict:
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_index(index: dict):
    _ensure_cache_dir()
    tmp = INDEX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True))
    tmp.rename(INDEX_FILE)


def _basename_from_url(url: str) -> str:
    """Strip query string, return path basename."""
    path = url.split("?", 1)[0].split("#", 1)[0]
    name = path.rstrip("/").split("/")[-1]
    if not name:
        return "asset"
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


def cache_get(url: str) -> Optional[Path]:
    """Return cached file path for URL, or None if not cached."""
    index = _load_index()
    entry = index.get(url)
    if not entry:
        return None
    p = CACHE_DIR / entry["local_file"]
    return p if p.is_file() else None


def cache_put(url: str, local_path: Path):
    """Copy local_path into cache by URL basename. Updates index."""
    if not local_path.is_file():
        return
    _ensure_cache_dir()
    target_name = _basename_from_url(url)
    target = CACHE_DIR / target_name
    if target.resolve() == local_path.resolve():
        return  # already in cache
    shutil.copy2(local_path, target)
    index = _load_index()
    index[url] = {
        "local_file": target_name,
        "fetched_ts": int(time.time()),
        "size_bytes": target.stat().st_size,
    }
    _save_index(index)


def preload_run_from_cache(run_dir: Path, manifest_path: Optional[Path] = None) -> int:
    """For each manifest entry with a cache hit, copy from cache into
    run_dir/fetched_assets/. Returns count of hits."""
    manifest_path = manifest_path or (run_dir / "artifacts" / "asset_manifest.json")
    if not manifest_path.is_file():
        return 0
    manifest = json.loads(manifest_path.read_text())
    fetched = run_dir / "fetched_assets"
    fetched.mkdir(parents=True, exist_ok=True)

    hits = 0
    for entry in manifest:
        url = entry.get("url")
        if not url:
            continue
        cached = cache_get(url)
        if not cached:
            continue
        # Use manifest-declared local_file if set, else cache basename
        target_name = entry.get("local_file") or cached.name
        target = fetched / target_name
        if target.exists() and target.stat().st_size == cached.stat().st_size:
            hits += 1
            continue
        shutil.copy2(cached, target)
        hits += 1
    return hits


def promote_run_to_cache(run_dir: Path) -> int:
    """For each fetched asset in run_dir/fetched_assets/, promote to cache.
    URL is read from the manifest if available. Returns count promoted."""
    fetched = run_dir / "fetched_assets"
    manifest_path = run_dir / "artifacts" / "asset_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else []
    by_filename = {}
    for entry in manifest:
        local = entry.get("local_file")
        url = entry.get("url")
        if local and url and url.startswith("http"):
            by_filename[local] = url

    promoted = 0
    for asset in fetched.glob("*"):
        if not asset.is_file():
            continue
        # Skip PIL-generated data-cards (locally rendered, not real fetches)
        if asset.name.startswith("data-card-"):
            continue
        url = by_filename.get(asset.name)
        if not url:
            # No URL known — promote under a synthetic key for content-addressed reuse
            url = f"local://{asset.name}"
        cache_put(url, asset)
        promoted += 1
    return promoted


def main():
    ap = argparse.ArgumentParser(description="Shared asset cache for make-viral-video")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_pre = sub.add_parser("preload", help="Pre-stage cache hits into a run dir")
    p_pre.add_argument("run_dir")
    p_pre.add_argument("--manifest")
    p_prom = sub.add_parser("promote", help="Promote a run's fetched_assets/ to cache")
    p_prom.add_argument("run_dir")
    sub.add_parser("list", help="Print cache index")
    args = ap.parse_args()

    if args.cmd == "preload":
        n = preload_run_from_cache(Path(args.run_dir),
                                   Path(args.manifest) if args.manifest else None)
        print(f"preload: {n} cache hits → {args.run_dir}/fetched_assets/")
    elif args.cmd == "promote":
        n = promote_run_to_cache(Path(args.run_dir))
        print(f"promote: {n} files copied to {CACHE_DIR}")
    elif args.cmd == "list":
        index = _load_index()
        for url, entry in sorted(index.items()):
            print(f"{entry['local_file']:40s}  {entry['size_bytes']:>9d}  {url}")


if __name__ == "__main__":
    main()
