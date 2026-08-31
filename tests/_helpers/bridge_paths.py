"""Rebind a bridge module's import-time Path constants that sit under its own resolved
root. Second roots ($CLAUDE_CONFIG_DIR and friends) are out of scope by design."""
from __future__ import annotations

from pathlib import Path


def _original_root(mod) -> "Path | None":
    for name in ("REPO", "WORKSPACE", "WORKSPACE_DIR"):
        v = getattr(mod, name, None)
        if isinstance(v, Path):
            return v
        if isinstance(v, str) and v:
            return Path(v)
    return None


def derived_path_attrs(mod, root: "Path | None" = None) -> "dict[str, Path]":
    """Path attributes under `root`, discovered by relationship rather than a name
    list, so a constant added later is covered without anyone extending a list."""
    root = root or _original_root(mod)
    if root is None:
        return {}
    out = {}
    for name in dir(mod):
        if name.startswith("__"):
            continue
        v = getattr(mod, name, None)
        if not isinstance(v, Path):
            continue
        try:
            v.relative_to(root)
        except ValueError:
            continue
        out[name] = v
    return out


def rebind_workspace(mod, new_root: Path) -> "dict[str, Path]":
    """Rebind path attrs under the module's root to `new_root`, returning the originals.
    The root attribute itself is rebound in whichever type it already had."""
    old_root = _original_root(mod)
    if old_root is None:
        raise AssertionError("module exposes no REPO/WORKSPACE root to rebind from")
    originals = derived_path_attrs(mod, old_root)
    new_root = Path(new_root)
    for name, val in originals.items():
        setattr(mod, name, new_root / val.relative_to(old_root))
    for name in ("REPO", "WORKSPACE", "WORKSPACE_DIR"):
        cur = getattr(mod, name, None)
        if isinstance(cur, Path):
            originals.setdefault(name, cur)
            setattr(mod, name, new_root)
        elif isinstance(cur, str) and cur:
            originals.setdefault(name, Path(cur))
            setattr(mod, name, str(new_root))
    return originals


def restore(mod, originals: "dict[str, Path]") -> None:
    for name, val in originals.items():
        cur = getattr(mod, name, None)
        setattr(mod, name, str(val) if isinstance(cur, str) else val)
