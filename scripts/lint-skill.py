#!/usr/bin/env python3
"""lint-skill.py — validate Sutando skill manifests against the v1 package schema.

Phase 1 of the skill-package model. Stdlib only (no jsonschema dep): the checks
mirror schemas/skill-manifest.schema.json but are hand-rolled so this runs in CI
with zero install. Also does a light *permission cross-check* — if a manifest
declares `permissions.network: false` but the skill's files clearly make network
calls, that's flagged, because a permission declaration that lies is worse than none.

Usage:
  python3 scripts/lint-skill.py skills/zoom            # lint one skill dir
  python3 scripts/lint-skill.py --all                  # lint every skills/*/manifest.json
  python3 scripts/lint-skill.py --all --strict         # warnings are errors

Exit 0 = clean, 1 = errors found. Warnings alone don't fail unless --strict.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


def resolve_hook_command(skill_dir: Path, command: str) -> Path | None:
    """Delegate to src/skill_hooks: one containment rule, not a second copy.
    Lazy via _repo_root() — a __file__ parent-walk trips the resolution gate."""
    global _RESOLVE_HOOK
    if _RESOLVE_HOOK is None:
        sys.path.insert(0, str(_repo_root() / "src"))
        from skill_hooks import resolve_hook_command as _impl
        _RESOLVE_HOOK = _impl
    return _RESOLVE_HOOK(skill_dir, command)


_RESOLVE_HOOK = None


def _repo_root() -> Path:
    """Repo root — NOT the workspace. Resolves via git (the sanctioned method,
    matching scripts/lint-workspace-resolution.sh) rather than a __file__
    parent-walk; falls back to the script's grandparent only if git is absent."""
    try:
        top = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path(__file__).resolve().parent,
            text=True, stderr=subprocess.DEVNULL).strip()
        if top:
            return Path(top)
    except Exception:  # noqa: BLE001 — git missing / not a repo → fall back
        pass
    return Path(__file__).resolve().parents[1]


REPO = _repo_root()
SEMVER = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SCOPE_RE = re.compile(r"^@[a-z0-9][a-z0-9-]*$")
STABILITY = {"stable", "experimental", "deprecated"}
FS_LEVELS = {"none", "read-only", "read-write"}
# The manifest-loader (src/inline-tools.ts) implements exactly two caller tiers:
# `owner` and `any_caller` (anything != any_caller buckets to owner). Keep this
# aligned with that contract — NOT the owner/team/other *task* tiers, which are a
# different axis (who may send a task, enforced at the bridge).
TIERS = {"owner", "any_caller"}
INTENTS = {"candidate-contribution", "private-customization"}
KNOWN_TOP = {
    "name", "scope", "version", "owner", "license", "description", "stability",
    "agent_compatibility", "dependencies", "permissions", "contract",
    "provenance", "enabled", "access_tier", "tools", "server", "startup", "config",
    "hooks",
}
# Signals a skill actually touches the network (used for the permission cross-check).
# No trailing \b: signals ending in a space/paren (`curl `, `fetch(`) are followed
# by a non-word char, and a trailing \b there kills the match — so `curl -s …` and
# `fetch()` (the common forms) would slip past. The leading \b still anchors word-start.
NET_SIGNALS = re.compile(
    r"\b(urllib\.request|requests\.|httpx|aiohttp|socket\.|websocket|fetch\(|curl\s|wget\s)"
)


def _lint_manifest(skill_dir: Path) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for one skill directory."""
    errors: list[str] = []
    warnings: list[str] = []
    mf = skill_dir / "manifest.json"
    if not mf.exists():
        return ([f"{skill_dir.name}: no manifest.json"], [])
    try:
        m = json.loads(mf.read_text())
    except json.JSONDecodeError as e:
        return ([f"{skill_dir.name}/manifest.json: invalid JSON — {e}"], [])
    if not isinstance(m, dict):
        return ([f"{skill_dir.name}/manifest.json: top-level must be an object"], [])

    def err(msg: str) -> None:
        errors.append(f"{skill_dir.name}: {msg}")

    def warn(msg: str) -> None:
        warnings.append(f"{skill_dir.name}: {msg}")

    # unknown keys
    for k in m:
        if k not in KNOWN_TOP:
            warn(f"unknown manifest field '{k}'")

    # required
    for req in ("name", "version", "owner", "stability"):
        if req not in m or m[req] in (None, ""):
            err(f"missing required field '{req}'")

    name = m.get("name")
    if isinstance(name, str):
        if not NAME_RE.match(name):
            err(f"name '{name}' must be lowercase-dash slug")
        if name != skill_dir.name:
            err(f"name '{name}' does not match directory '{skill_dir.name}'")
    # scope is optional; it maps this skill to a SkillPack `@scope/name` id at
    # publish (in-repo `name` stays flat/dir-matched — the loader is unaffected).
    scope = m.get("scope")
    if scope is not None and not (isinstance(scope, str) and SCOPE_RE.match(scope)):
        err(f"scope '{scope}' must be an '@namespace' slug (e.g. '@sutando')")
    ver = m.get("version")
    if isinstance(ver, str) and not SEMVER.match(ver):
        err(f"version '{ver}' is not SemVer (X.Y.Z)")
    stab = m.get("stability")
    if stab is not None and stab not in STABILITY:
        err(f"stability '{stab}' not in {sorted(STABILITY)}")

    # permissions
    perms = m.get("permissions")
    if perms is not None:
        if not isinstance(perms, dict):
            err("permissions must be an object")
        else:
            fs = perms.get("filesystem")
            if fs is not None and fs not in FS_LEVELS:
                err(f"permissions.filesystem '{fs}' not in {sorted(FS_LEVELS)}")
            secrets = perms.get("secrets")
            if secrets is not None and secrets != "none" and not isinstance(secrets, list):
                err("permissions.secrets must be 'none' or a list of key names")
            net = perms.get("network")
            if net is not None and not isinstance(net, bool):
                err("permissions.network must be a boolean")
            # cross-check: declared network:false but code looks networked
            if net is False:
                hits = _network_hits(skill_dir)
                if hits:
                    warn(f"permissions.network=false but code references {hits[0]} "
                         f"(in {hits[1]}) — declaration may be inaccurate")

    # access_tier
    tier = m.get("access_tier")
    if tier is not None and tier not in TIERS:
        err(f"access_tier '{tier}' not in {sorted(TIERS)}")

    # provenance.upstream_intent
    prov = m.get("provenance")
    if isinstance(prov, dict):
        ui = prov.get("upstream_intent")
        if ui is not None and ui not in INTENTS:
            err(f"provenance.upstream_intent '{ui}' not in {sorted(INTENTS)}")

    # tools imply manifest-loaded
    if "tools" in m:
        for req in ("enabled", "access_tier"):
            if req not in m:
                err(f"declares 'tools' → must also set '{req}'")
        tools_rel = str(m["tools"])
        # Hard-error on any '..' segment: a tool file escaping its skill dir
        # defeats the per-skill permission model this schema builds. (The old
        # `.lstrip('./')` also silently rewrote '../x' → 'x', linting the wrong
        # path; match the loader's single-'./'-prefix normalization instead.)
        if ".." in tools_rel.split("/"):
            err(f"tools path '{tools_rel}' must not escape the skill dir (no '..')")
        else:
            tpath = skill_dir / re.sub(r"^\./", "", tools_rel)
            if not tpath.exists():
                err(f"tools path '{tools_rel}' does not exist")

    hooks = m.get("hooks")
    if hooks is not None:
        if not isinstance(hooks, list):
            err(f"hooks must be a list, got {type(hooks).__name__}")
        else:
            for i, hook in enumerate(hooks):
                if not isinstance(hook, dict):
                    err(f"hooks[{i}] must be an object")
                    continue
                event, cmd = hook.get("event"), hook.get("command")
                if not isinstance(event, str) or not event:
                    err(f"hooks[{i}] missing 'event'")
                if not isinstance(cmd, str) or not cmd:
                    err(f"hooks[{i}] missing 'command'")
                    continue
                # One containment rule, shared with discovery: lint that accepts
                # what discovery registers is the only version that gates anything.
                if resolve_hook_command(skill_dir, cmd) is None:
                    err(f"hooks[{i}] command '{cmd}' must resolve inside the skill dir")
                elif not (skill_dir / re.sub(r"^\./", "", cmd)).exists():
                    err(f"hooks[{i}] command '{cmd}' does not exist")

    return (errors, warnings)


def _network_hits(skill_dir: Path) -> tuple[str, str] | tuple:
    """First network signal found in the skill's source, or ()."""
    for f in skill_dir.rglob("*"):
        if f.suffix in (".py", ".ts", ".js", ".sh", ".cjs", ".mjs") and f.is_file():
            try:
                text = f.read_text(errors="ignore")
            except OSError:
                continue
            mo = NET_SIGNALS.search(text)
            if mo:
                return (mo.group(1), f.relative_to(skill_dir).as_posix())
    return ()


def main(argv: list[str]) -> int:
    strict = "--strict" in argv
    args = [a for a in argv if not a.startswith("--")]
    if "--all" in argv:
        targets = sorted(p.parent for p in (REPO / "skills").glob("*/manifest.json"))
    elif args:
        targets = [Path(a) if Path(a).is_absolute() else REPO / a for a in args]
    else:
        print(__doc__.strip().splitlines()[0])
        print("usage: lint-skill.py <skill-dir> | --all [--strict]", file=sys.stderr)
        return 2

    all_err: list[str] = []
    all_warn: list[str] = []
    for t in targets:
        e, w = _lint_manifest(t)
        all_err += e
        all_warn += w

    for w in all_warn:
        print(f"  ⚠ {w}")
    for e in all_err:
        print(f"  ✗ {e}")
    n = len(targets)
    print(f"\nlint-skill: {n} manifest(s), {len(all_err)} error(s), {len(all_warn)} warning(s)")
    if all_err or (strict and all_warn):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
