"""Atomic, fail-closed installs of skill directories into the core's skills dir.

The single owner of "put a skill directory on disk where the core will see it".
Two installers use it: trusted-capabilities (files fetched from GitHub) and
marketplace (a tar.gz bundle from Sutando Cloud). The rules mirror the desktop
host's skill_materializer.rs, each learned the hard way:

- The target must be a REAL directory, replaced by rename. Claude Code's skill
  watcher picks up an in-place rename within ~1s but does not follow symlinks.
- Refuse to replace a symlink: core-bundled skills are symlinks into the engine
  checkout, and swapping one for a downloaded copy silently forks it.
- Stage in a temp dir on the same filesystem, then os.replace — never leave a
  half-written skill that the watcher could load.
- Reject tar members that are absolute, contain `..`, or are links/devices.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable

PROVENANCE_FILE = ".sutando-source.json"
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
MAX_FILES = 500
MAX_BYTES = 25 * 1024 * 1024
# Archive litter from packing on a Mac: AppleDouble `._name` resource forks and
# Finder metadata. Never skill content, and a top-level `._<dir>` beside the
# wrapper directory otherwise reads as a second top-level entry (live catalog
# bundle live-preview 0.4.1, 2026-09-14).
_MAC_LITTER = ("__MACOSX", ".DS_Store")


def _is_mac_litter(parts: tuple[str, ...]) -> bool:
    return any(p in _MAC_LITTER or p.startswith("._") for p in parts)


def check_slug(slug: str) -> str:
    if not SLUG_RE.fullmatch(slug or "") or ".." in slug:
        raise ValueError(f"not a safe skill slug: {slug!r}")
    return slug


def refuse_symlink(target: Path) -> None:
    if target.is_symlink():
        raise ValueError(
            f"{target} is a symlink (a bundled engine skill); refusing to replace it"
        )


def atomic_install(
    slug: str,
    dest_root: Path,
    populate: Callable[[Path], None],
    metadata: dict | None = None,
) -> Path:
    """Stage a skill with `populate(temp_dir)`, then swap it onto dest_root/slug.

    On a failed swap the previous directory is restored; the temp dir is always
    removed.
    """
    check_slug(slug)
    dest_root.mkdir(parents=True, exist_ok=True)
    target = dest_root / slug
    refuse_symlink(target)
    temp = Path(tempfile.mkdtemp(prefix=f".{slug}.", dir=dest_root))
    try:
        populate(temp)
        if metadata is not None:
            (temp / PROVENANCE_FILE).write_text(json.dumps(metadata, indent=2) + "\n")
        backup = target.with_name(f".{slug}.previous")
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(temp, target)
        except BaseException:
            if backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
        return target
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def extract_bundle(tar_gz: bytes, dest: Path) -> None:
    """Unpack a skill tar.gz into `dest`, flattening a single wrapper directory.

    SKILL.md must end up at the root of `dest` (either at the archive root or
    inside exactly one top-level directory — the two shapes publishers ship).
    """
    try:
        archive = tarfile.open(fileobj=io.BytesIO(tar_gz), mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise ValueError(f"bundle is not a readable tar.gz: {exc}") from None
    with archive:
        members = archive.getmembers()
        files = [m for m in members if m.isfile()]
        if len(files) > MAX_FILES:
            raise ValueError(f"bundle has {len(files)} files; safety limit is {MAX_FILES}")
        if sum(m.size for m in files) > MAX_BYTES:
            raise ValueError(f"bundle exceeds safety limit of {MAX_BYTES} bytes")
        paths = []
        for m in members:
            rel = PurePosixPath(m.name)
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"unsafe bundle member: {m.name}")
            if not (m.isfile() or m.isdir()):
                raise ValueError(f"bundle member is not a regular file or directory: {m.name}")
            parts = tuple(p for p in rel.parts if p not in ("", "."))
            if parts and not _is_mac_litter(parts):
                paths.append((m, parts))
        strip = _wrapper_depth(paths)
        for m, parts in paths:
            parts = parts[strip:]
            if not parts or m.isdir():
                continue
            out = dest.joinpath(*parts)
            out.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(m) as src, open(out, "wb") as fh:
                shutil.copyfileobj(src, fh)
            if m.mode & 0o111:
                out.chmod(0o755)
    if not (dest / "SKILL.md").is_file():
        raise ValueError("bundle has no SKILL.md at its root")


def _wrapper_depth(paths: list) -> int:
    if any(parts == ("SKILL.md",) for _, parts in paths):
        return 0
    tops = {parts[0] for _, parts in paths}
    if len(tops) == 1 and any(parts[1:] == ("SKILL.md",) for _, parts in paths):
        return 1
    raise ValueError("bundle has no SKILL.md at its root or inside a single wrapper directory")


def remove_skill(slug: str, dest_root: Path) -> bool:
    """Delete an installed skill directory. False when nothing was there."""
    check_slug(slug)
    target = dest_root / slug
    refuse_symlink(target)
    if not target.exists():
        return False
    shutil.rmtree(target)
    return True


def local_version(skill_dir: Path) -> str | None:
    """The version an installed skill reports: manifest.json, else provenance."""
    for name, key in (("manifest.json", "version"), (PROVENANCE_FILE, "version")):
        try:
            v = json.loads((skill_dir / name).read_text()).get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        except (OSError, ValueError, AttributeError):
            continue
    return None


def read_provenance(skill_dir: Path) -> dict:
    try:
        data = json.loads((skill_dir / PROVENANCE_FILE).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
