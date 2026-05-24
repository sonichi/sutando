#!/usr/bin/env python3
"""
Tests for skills/make-viral-video/scripts/validate_asset.py

Coverage:
  md5_of_file   — hash of known bytes, different files differ
  validate()    — file_not_found, non_image_content_type, known_404_hash match,
                  known_404_hash miss, sub_minimum_resolution, ocr 404 keyword,
                  off_whitelist_domain, all_clear (valid=True)
  main()        — exit 0 on valid, exit 1 on invalid, --known-404-hash flag

Run: python3 skills/make-viral-video/tests/validate_asset.test.py
Exit code: 0 on pass, 1 on fail.
"""

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent.parent.parent

_spec = importlib.util.spec_from_file_location(
    "validate_asset",
    REPO / "skills" / "make-viral-video" / "scripts" / "validate_asset.py",
)
va = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(va)

TMPDIR = Path(tempfile.mkdtemp())


def _write_file(name: str, content: bytes = b"fake image data") -> Path:
    p = TMPDIR / name
    p.write_bytes(content)
    return p


# ── md5_of_file ───────────────────────────────────────────────────────────────

class TestMd5OfFile(unittest.TestCase):
    def test_known_bytes(self):
        data = b"hello world"
        expected = hashlib.md5(data).hexdigest()
        p = _write_file("known.bin", data)
        self.assertEqual(va.md5_of_file(p), expected)

    def test_different_content_differs(self):
        p1 = _write_file("file1.bin", b"aaa")
        p2 = _write_file("file2.bin", b"bbb")
        self.assertNotEqual(va.md5_of_file(p1), va.md5_of_file(p2))

    def test_same_content_same_hash(self):
        p1 = _write_file("dup1.bin", b"same data")
        p2 = _write_file("dup2.bin", b"same data")
        self.assertEqual(va.md5_of_file(p1), va.md5_of_file(p2))


# ── validate ──────────────────────────────────────────────────────────────────

class TestValidate(unittest.TestCase):
    def _validate(self, path, allowed_domains=None, known_404=(), source_url=None, mime="image/jpeg", dims=(1280, 720), ocr=""):
        """Call validate() with mocked detect_image_dims and ocr_text."""
        with patch.object(va, "detect_image_dims", return_value=(dims, mime)), \
             patch.object(va, "ocr_text", return_value=ocr):
            return va.validate(path, allowed_domains or [], known_404, source_url=source_url)

    def test_file_not_found(self):
        result = va.validate(TMPDIR / "nonexistent.jpg", [], [])
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "file_not_found")

    def test_non_image_mime(self):
        p = _write_file("page.html", b"<html>")
        result = self._validate(p, mime="text/html")
        self.assertFalse(result["valid"])
        self.assertIn("non_image_content_type", result["reason"])

    def test_known_404_hash_match(self):
        data = b"404 page content"
        md5 = hashlib.md5(data).hexdigest()
        p = _write_file("404page.png", data)
        known = [md5[:14]]  # matching prefix
        result = self._validate(p, known_404=known)
        self.assertFalse(result["valid"])
        self.assertIn("matches_known_404_hash", result["reason"])

    def test_known_404_hash_no_match(self):
        p = _write_file("real_photo.jpg", b"real photo bytes")
        known = ["0000000000dead"]  # won't match
        result = self._validate(p, known_404=known)
        self.assertTrue(result["valid"])

    def test_sub_minimum_resolution(self):
        p = _write_file("tiny.jpg", b"tiny")
        result = self._validate(p, dims=(100, 100))
        self.assertFalse(result["valid"])
        self.assertIn("sub_minimum_resolution", result["reason"])

    def test_exactly_minimum_resolution_ok(self):
        p = _write_file("ok.jpg", b"ok photo")
        result = self._validate(p, dims=(va.MIN_WIDTH, va.MIN_HEIGHT))
        self.assertTrue(result["valid"])

    def test_ocr_404_keyword_detected(self):
        p = _write_file("ocrtest.jpg", b"image bytes")
        result = self._validate(p, ocr="page not found please check url")
        self.assertFalse(result["valid"])
        self.assertIn("ocr_matched_404_keyword", result["reason"])

    def test_ocr_clean_passes(self):
        p = _write_file("ocr_clean.jpg", b"image bytes")
        result = self._validate(p, ocr="a beautiful sunset over the ocean")
        self.assertTrue(result["valid"])

    def test_off_whitelist_domain(self):
        p = _write_file("off_domain.jpg", b"photo")
        result = self._validate(p, allowed_domains=["whitehouse.gov"],
                                source_url="https://evil.com/photo.jpg")
        self.assertFalse(result["valid"])
        self.assertIn("off_whitelist_domain", result["reason"])

    def test_allowed_domain_passes(self):
        p = _write_file("allowed.jpg", b"photo")
        result = self._validate(p, allowed_domains=["whitehouse.gov"],
                                source_url="https://whitehouse.gov/photo.jpg")
        self.assertTrue(result["valid"])

    def test_subdomain_of_allowed_passes(self):
        p = _write_file("subdomain.jpg", b"photo")
        result = self._validate(p, allowed_domains=["whitehouse.gov"],
                                source_url="https://media.whitehouse.gov/photo.jpg")
        self.assertTrue(result["valid"])

    def test_no_domain_check_when_no_source_url(self):
        p = _write_file("no_url.jpg", b"photo")
        result = self._validate(p, allowed_domains=["whitehouse.gov"], source_url=None)
        self.assertTrue(result["valid"])

    def test_valid_report_has_hash_and_mime(self):
        p = _write_file("full.jpg", b"photo")
        result = self._validate(p)
        self.assertIn("hash", result)
        self.assertIn("mime", result)

    def test_valid_report_has_dims(self):
        p = _write_file("dims.jpg", b"photo")
        result = self._validate(p, dims=(800, 600))
        self.assertIn("dims", result)
        self.assertEqual(result["dims"], [800, 600])

    def test_none_dims_not_in_report(self):
        p = _write_file("nodims.jpg", b"photo")
        result = self._validate(p, dims=None)
        self.assertNotIn("dims", result)


# ── main ──────────────────────────────────────────────────────────────────────

class TestMain(unittest.TestCase):
    def _run(self, path, extra_args=None):
        argv = [str(path)] + (extra_args or [])
        with patch("sys.stdout", new_callable=StringIO) as out, \
             patch.object(va, "detect_image_dims", return_value=((1280, 720), "image/jpeg")), \
             patch.object(va, "ocr_text", return_value=""):
            try:
                rc = va.main(argv)
            except SystemExit as e:
                rc = e.code
            return rc, json.loads(out.getvalue())

    def test_valid_file_exits_0(self):
        p = _write_file("valid_main.jpg", b"photo data")
        rc, result = self._run(p)
        self.assertEqual(rc, 0)
        self.assertTrue(result["valid"])

    def test_missing_file_exits_1(self):
        argv = [str(TMPDIR / "ghost.jpg")]
        with patch("sys.stdout", new_callable=StringIO) as out:
            try:
                rc = va.main(argv)
            except SystemExit as e:
                rc = e.code
            result = json.loads(out.getvalue())
        self.assertEqual(rc, 1)
        self.assertFalse(result["valid"])

    def test_known_404_hash_flag(self):
        data = b"definitely a 404 page"
        md5 = hashlib.md5(data).hexdigest()
        p = _write_file("hash_flag.jpg", data)
        rc, result = self._run(p, extra_args=["--known-404-hash", md5[:14]])
        self.assertEqual(rc, 1)
        self.assertFalse(result["valid"])


if __name__ == "__main__":
    unittest.main()
