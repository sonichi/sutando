#!/usr/bin/env python3
"""Asset validation for make-viral-video skill — phase 2 of the build pipeline.

Per Lucy's `feedback_verification_gates_need_semantic_checks.md` (2026-05-09)
and the v12 PR46-404-dupe incident:

Structural counts (asset_count == 4) are insufficient. We need semantic
checks that catch:
  - hash duplicates (codex hallucinates 4× the same broken URL → 4× the
    same captured 404 page)
  - the 404 page itself (browser fallback captures a 1280×720 PNG of the
    "Page Not Found" page, indistinguishable from a real news photo by
    structural ffprobe checks alone)
  - sub-thumb resolutions (≥600×400 minimum)
  - non-image content-types served as image extensions
  - off-domain captures (codex following redirects out of the whitelist)

Usage:
  python3 validate_asset.py <asset_path> [--allowed-domain war.gov]
                                         [--allowed-domain whitehouse.gov]
                                         [--known-404-hash <md5>]

Returns exit 0 + writes validation report to stdout (JSON) on success;
exit 1 on validation failure (with structured failure reason).
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path


# Hash patterns observed for known 404-page captures. Lucy reported
# md5 `00743781de0532...` for war.gov's PR46 404 page (4 dupes in v12).
# This list grows as we encounter new 404 page hashes per domain.
KNOWN_404_HASH_PREFIXES = (
    "00743781de0532",  # war.gov PR-not-found, observed 2026-05-09
)

# OCR keyword patterns. If we have to OCR (PIL+tesseract not assumed),
# we look for these in any extracted text. Implemented as a fallback —
# the hash check + content-type check should catch most cases first.
OCR_404_KEYWORDS = (
    "404",
    "page not found",
    "not found",
    "this page does not exist",
    "the requested url",
)

MIN_WIDTH = 600
MIN_HEIGHT = 400


def md5_of_file(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_image_dims(path: Path):
    """Best-effort image dimension detection without requiring PIL.

    Tries `file` command first (POSIX), falls back to PIL if available."""
    try:
        import subprocess
        r = subprocess.run(["file", "--mime-type", "-b", str(path)], capture_output=True, text=True)
        mime = r.stdout.strip()
        if not mime.startswith("image/"):
            return None, mime
    except Exception:
        mime = "unknown"

    # Use PIL if installed; otherwise rely on `file` for size info via -i / sips.
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size, mime
    except Exception:
        pass

    # Fallback: macOS sips
    try:
        import subprocess
        r = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
                           capture_output=True, text=True)
        w, h = None, None
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith("pixelWidth:"):
                w = int(line.split(":")[1].strip())
            elif line.startswith("pixelHeight:"):
                h = int(line.split(":")[1].strip())
        if w and h:
            return (w, h), mime
    except Exception:
        pass

    return None, mime


def ocr_text(path: Path) -> str:
    """Try to OCR an image; return empty string if no OCR available.

    This is a best-effort fallback — we'd prefer to catch 404 pages by
    hash before reaching OCR."""
    try:
        import subprocess
        # tesseract if available
        r = subprocess.run(["tesseract", str(path), "-", "-l", "eng"],
                           capture_output=True, text=True, timeout=15)
        return (r.stdout or "").lower()
    except Exception:
        return ""


def validate(path: Path, allowed_domains, known_404_hashes, source_url=None):
    """Returns dict: {valid: bool, reason?: str, hash: str, mime: str, dims?: [w, h]}."""
    if not path.exists():
        return {"valid": False, "reason": "file_not_found", "path": str(path)}

    file_md5 = md5_of_file(path)
    dims, mime = detect_image_dims(path)

    report = {
        "path": str(path),
        "hash": file_md5,
        "mime": mime,
    }
    if dims:
        report["dims"] = list(dims)

    # 1. Content-type must be image/*
    if not mime.startswith("image/"):
        return {**report, "valid": False, "reason": "non_image_content_type"}

    # 2. Hash must not match a known 404 page
    for prefix in known_404_hashes:
        if file_md5.startswith(prefix):
            return {**report, "valid": False, "reason": f"matches_known_404_hash({prefix}...)"}

    # 3. Resolution check
    if dims:
        w, h = dims
        if w < MIN_WIDTH or h < MIN_HEIGHT:
            return {**report, "valid": False, "reason": f"sub_minimum_resolution({w}x{h}, need {MIN_WIDTH}x{MIN_HEIGHT})"}

    # 4. OCR fallback for 404 detection (best-effort)
    text = ocr_text(path)
    if text:
        for keyword in OCR_404_KEYWORDS:
            if keyword in text:
                return {**report, "valid": False, "reason": f"ocr_matched_404_keyword({keyword!r})", "ocr_excerpt": text[:200]}

    # 5. Domain whitelist (informational only — caller enforces if needed)
    if source_url and allowed_domains:
        from urllib.parse import urlparse
        host = urlparse(source_url).hostname or ""
        if not any(host == d or host.endswith("." + d) for d in allowed_domains):
            return {**report, "valid": False, "reason": f"off_whitelist_domain({host})", "source_url": source_url}

    return {**report, "valid": True}


def main(argv=None):
    p = argparse.ArgumentParser(description="Validate a fetched asset for the make-viral-video skill")
    p.add_argument("path", help="Path to the asset file")
    p.add_argument("--allowed-domain", action="append", default=[], help="Allowed source domain (repeatable). Omit to skip whitelist check.")
    p.add_argument("--known-404-hash", action="append", default=[], help="Additional known-404 hash prefix (repeatable). Built-in list always applied.")
    p.add_argument("--source-url", default=None, help="Original source URL (for whitelist enforcement)")
    args = p.parse_args(argv)

    known_404 = list(KNOWN_404_HASH_PREFIXES) + list(args.known_404_hash)
    result = validate(Path(args.path), args.allowed_domain, known_404, source_url=args.source_url)
    print(json.dumps(result, indent=2))
    return 0 if result.get("valid") else 1


if __name__ == "__main__":
    sys.exit(main())
