#!/usr/bin/env python3
"""
Tests for skills/make-viral-video/scripts/asset_cache.py

Coverage:
  _basename_from_url — simple path, with query string, with fragment,
                       trailing slash, empty basename fallback
  cache_get          — miss (empty index), hit (file exists), stale (file gone)
  cache_put          — copies file, updates index, skips nonexistent source,
                       skips if already in cache
  preload_run_from_cache — no manifest returns 0, cache miss returns 0,
                            cache hit copies file and returns count
  promote_run_to_cache   — no fetched_assets dir, promotes files with manifest URL

Run: python3 skills/make-viral-video/tests/asset_cache.test.py
Exit code: 0 on pass, 1 on fail.
"""

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "asset_cache",
    REPO / "skills" / "make-viral-video" / "scripts" / "asset_cache.py",
)
ac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac)


class _CacheIsolated(unittest.TestCase):
    """Base class that redirects CACHE_DIR and INDEX_FILE to a temp dir."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self._orig_cache_dir = ac.CACHE_DIR
        self._orig_index_file = ac.INDEX_FILE
        ac.CACHE_DIR = self.tmpdir / "cache"
        ac.INDEX_FILE = self.tmpdir / "index.json"

    def tearDown(self):
        ac.CACHE_DIR = self._orig_cache_dir
        ac.INDEX_FILE = self._orig_index_file
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_file(self, name: str, content: str = "data") -> Path:
        p = self.tmpdir / name
        p.write_text(content)
        return p


# ── _basename_from_url ────────────────────────────────────────────────────────

class TestBasenameFromUrl(unittest.TestCase):
    def test_simple_url(self):
        self.assertEqual(ac._basename_from_url("https://example.com/photo.jpg"), "photo.jpg")

    def test_strips_query_string(self):
        result = ac._basename_from_url("https://example.com/img.png?size=large&v=2")
        self.assertEqual(result, "img.png")

    def test_strips_fragment(self):
        result = ac._basename_from_url("https://example.com/asset.mp4#t=10")
        self.assertEqual(result, "asset.mp4")

    def test_trailing_slash_strips_and_returns_dir_segment(self):
        # "dir/" → strips slash → last segment is "dir"
        result = ac._basename_from_url("https://example.com/dir/")
        self.assertEqual(result, "dir")

    def test_root_path_returns_domain_as_basename(self):
        # "/" → rstrip → "example.com" is last segment
        result = ac._basename_from_url("https://example.com/")
        self.assertEqual(result, "example.com")

    def test_special_chars_sanitized(self):
        result = ac._basename_from_url("https://example.com/file name!.jpg")
        self.assertNotIn(" ", result)
        self.assertNotIn("!", result)


# ── cache_get ─────────────────────────────────────────────────────────────────

class TestCacheGet(_CacheIsolated):
    def test_miss_returns_none(self):
        result = ac.cache_get("https://example.com/photo.jpg")
        self.assertIsNone(result)

    def test_hit_returns_path(self):
        # Manually add to index and create file
        src = self._make_file("photo.jpg")
        ac.cache_put("https://example.com/photo.jpg", src)
        result = ac.cache_get("https://example.com/photo.jpg")
        self.assertIsNotNone(result)
        self.assertTrue(result.is_file())

    def test_stale_entry_returns_none(self):
        # Index entry exists but file doesn't
        ac.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        index = {"https://example.com/ghost.jpg": {"local_file": "ghost.jpg", "fetched_ts": 1, "size_bytes": 0}}
        ac.INDEX_FILE.write_text(json.dumps(index))
        result = ac.cache_get("https://example.com/ghost.jpg")
        self.assertIsNone(result)


# ── cache_put ─────────────────────────────────────────────────────────────────

class TestCachePut(_CacheIsolated):
    def test_puts_file_in_cache(self):
        src = self._make_file("image.jpg", "fake-image-data")
        ac.cache_put("https://cdn.example.com/image.jpg", src)
        cached = ac.CACHE_DIR / "image.jpg"
        self.assertTrue(cached.is_file())
        self.assertEqual(cached.read_text(), "fake-image-data")

    def test_updates_index(self):
        src = self._make_file("img2.jpg", "data")
        ac.cache_put("https://example.com/img2.jpg", src)
        index = json.loads(ac.INDEX_FILE.read_text())
        self.assertIn("https://example.com/img2.jpg", index)
        self.assertEqual(index["https://example.com/img2.jpg"]["local_file"], "img2.jpg")

    def test_nonexistent_source_skipped(self):
        ac.cache_put("https://example.com/x.jpg", self.tmpdir / "nonexistent.jpg")
        self.assertFalse(ac.INDEX_FILE.exists())

    def test_already_in_cache_not_duplicated(self):
        src = self._make_file("dup.jpg", "data")
        ac.cache_put("https://example.com/dup.jpg", src)
        # Cache file is now the target; calling with same resolved path is a no-op
        cached_path = ac.CACHE_DIR / "dup.jpg"
        ac.cache_put("https://example.com/dup.jpg", cached_path)
        # Index should still have exactly one entry (no error)
        index = json.loads(ac.INDEX_FILE.read_text())
        self.assertIn("https://example.com/dup.jpg", index)


# ── preload_run_from_cache ────────────────────────────────────────────────────

class TestPreloadRunFromCache(_CacheIsolated):
    def test_no_manifest_returns_zero(self):
        run_dir = self.tmpdir / "run"
        run_dir.mkdir()
        result = ac.preload_run_from_cache(run_dir)
        self.assertEqual(result, 0)

    def test_cache_miss_returns_zero(self):
        run_dir = self.tmpdir / "run"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True)
        manifest = [{"url": "https://example.com/missing.jpg", "local_file": "missing.jpg"}]
        (artifacts / "asset_manifest.json").write_text(json.dumps(manifest))
        result = ac.preload_run_from_cache(run_dir)
        self.assertEqual(result, 0)

    def test_cache_hit_copies_file(self):
        # Seed the cache
        src = self._make_file("cached_photo.jpg", "photo content")
        ac.cache_put("https://example.com/cached_photo.jpg", src)

        run_dir = self.tmpdir / "run2"
        artifacts = run_dir / "artifacts"
        artifacts.mkdir(parents=True)
        manifest = [{"url": "https://example.com/cached_photo.jpg", "local_file": "cached_photo.jpg"}]
        (artifacts / "asset_manifest.json").write_text(json.dumps(manifest))

        result = ac.preload_run_from_cache(run_dir)
        self.assertEqual(result, 1)
        dest = run_dir / "fetched_assets" / "cached_photo.jpg"
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.read_text(), "photo content")


# ── promote_run_to_cache ──────────────────────────────────────────────────────

class TestPromoteRunToCache(_CacheIsolated):
    def test_no_fetched_dir_returns_zero(self):
        run_dir = self.tmpdir / "empty_run"
        run_dir.mkdir()
        result = ac.promote_run_to_cache(run_dir)
        self.assertEqual(result, 0)

    def test_promotes_files_with_manifest_url(self):
        run_dir = self.tmpdir / "run3"
        fetched = run_dir / "fetched_assets"
        fetched.mkdir(parents=True)
        asset = fetched / "photo.jpg"
        asset.write_text("promoted photo")

        artifacts = run_dir / "artifacts"
        artifacts.mkdir()
        manifest = [{"local_file": "photo.jpg", "url": "https://example.com/photo.jpg"}]
        (artifacts / "asset_manifest.json").write_text(json.dumps(manifest))

        result = ac.promote_run_to_cache(run_dir)
        self.assertGreater(result, 0)
        # Should be in cache now
        cached = ac.cache_get("https://example.com/photo.jpg")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.read_text(), "promoted photo")


if __name__ == "__main__":
    unittest.main()
