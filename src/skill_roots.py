"""Where installed skills live, and what their manifests declare — the one Python scan of the
sanctioned roots: the engine's `<repo>/skills` and the owner's `<workspace>/skills`, the pair
`skills/install.sh` links (the TS loader `loadSkillManifestTools` scans these and more; consumers
and their roots are listed in skills/MANIFEST.md). Generic:
a caller names the manifest FIELD it wants, never a skill; a disabled manifest is skipped; the
declared script must resolve inside its own skill, symlinks followed (a manifest may be a third
party's). Two declarers, in one root or one per root, are a conflict nobody picks from —
except the same skill name in both roots: the shipped copy wins and shadows the owner's, the
rule `skills/install.sh` applies to the same pair.

`declared(field, workspace, override=...)` is what an edge injects into a core helper: the one
script, or none and why. Core helpers receive that; they do not call this.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import NamedTuple, Optional

REPO_SKILLS = Path(__file__).resolve().parent.parent / "skills"  # lint-workspace-resolution: allow-repo-root
MANIFEST_MAX_BYTES = 1 << 20


class DeclarationConflict(RuntimeError):
    """More than one installed skill declares the field; none is picked."""


class Declaration(NamedTuple):
    """The roots' answer for a field: `script`, or None with `reason` for a conflict (None: nothing declared)."""
    script: Optional[Path]
    reason: Optional[str]


def skill_roots(workspace=None) -> list:
    """[<repo>/skills, <workspace>/skills] — the engine's first; the same directory once."""
    if workspace is None:
        from workspace_default import resolve_workspace  # noqa: PLC0415 — heavy loader
        workspace = resolve_workspace(migrate=False)
    roots: list = []
    for d in (REPO_SKILLS, Path(workspace) / "skills"):
        if all(d.resolve() != r.resolve() for r in roots):
            roots.append(d)
    return roots


def read_manifest(path: Path):
    """A manifest's JSON, read without ever waiting: a FIFO, device or directory there, or one over
    MANIFEST_MAX_BYTES, raises OSError or ValueError instead of blocking or reading on."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{path} is not a regular file")
        data = f.read(MANIFEST_MAX_BYTES + 1)
    if len(data) > MANIFEST_MAX_BYTES:
        raise ValueError(f"{path} is over {MANIFEST_MAX_BYTES} bytes")
    return json.loads(data.decode("utf-8"))


def declared_scripts(field: str, roots, suffixes=None) -> list:
    """(skill name, script) per enabled manifest under `roots` declaring `field` with a script that
    resolves inside its skill, and ends in one of `suffixes` when given. `roots`: one directory or
    several. A manifest that cannot be read or resolved, whatever the error, is skipped; the others
    still count."""
    dirs = [Path(roots)] if isinstance(roots, (str, Path)) else [Path(d) for d in roots]
    out: list = []
    seen: set = set()
    for d in dirs:  # root order is precedence: a same-name skill in a later root is shadowed
        for manifest in sorted(d.glob("*/manifest.json")):
            name = manifest.parent.name
            if name in seen:
                continue
            seen.add(name)
            try:
                found = _declaration(manifest, field, suffixes)
            except Exception:  # noqa: BLE001 — one unreadable or unresolvable manifest skips only itself
                continue
            if found:
                out.append((name, found))
    return out


def _declaration(manifest: Path, field: str, suffixes) -> Optional[Path]:
    """The script one manifest declares for `field`, when enabled, suffixed and contained; else None."""
    data = read_manifest(manifest)
    rel = data.get(field) if isinstance(data, dict) else None
    if not isinstance(rel, str) or not rel or data.get("enabled") is False:
        return None
    if suffixes is not None and not rel.endswith(tuple(suffixes)):
        return None
    skill = manifest.parent.resolve()
    script = (skill / rel).resolve()
    return script if script.is_relative_to(skill) and script.is_file() else None


def declared_script(field: str, roots) -> Optional[Path]:
    """The one declared script under `roots`; None when none; DeclarationConflict when several."""
    found = declared_scripts(field, roots)
    if len(found) > 1:
        raise DeclarationConflict(f"more than one skill declares {field}: "
                                  + ", ".join(sorted(n for n, _ in found)) + "; refusing to pick one")
    return found[0][1] if found else None


def declared(field: str, workspace=None, override=None, roots=None) -> Declaration:
    """The edge's resolution: an `override` path (a `--store-adapter` flag) is taken as given;
    else the one declarer across `roots` (default: skill_roots(workspace))."""
    if override:
        return Declaration(Path(override), None)
    try:
        return Declaration(declared_script(field, skill_roots(workspace) if roots is None else roots), None)
    except DeclarationConflict as e:
        return Declaration(None, str(e))
