"""Where installed skills live, and what their manifests declare — the one Python scan of the
sanctioned roots: the engine's `<repo>/skills` and the owner's `<workspace>/skills`, the pair
`skills/install.sh` links (the TS loader `loadSkillManifestTools` scans the same two). Generic:
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
from pathlib import Path
from typing import NamedTuple, Optional

REPO_SKILLS = Path(__file__).resolve().parent.parent / "skills"  # lint-workspace-resolution: allow-repo-root


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


def declared_scripts(field: str, roots) -> list:
    """(skill name, script) per enabled manifest under `roots` declaring `field` with a script that
    resolves inside its skill. `roots`: one directory or several."""
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
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rel = data.get(field) if isinstance(data, dict) else None
            if not isinstance(rel, str) or not rel or data.get("enabled") is False:
                continue
            skill = manifest.parent.resolve()
            script = (skill / rel).resolve()
            if script.is_relative_to(skill) and script.is_file():
                out.append((name, script))
    return out


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
