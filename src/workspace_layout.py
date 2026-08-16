"""Spawn-time guard for the `<repo>/workspace` wiring: heals recoverable breaks
to the durable symlink; a real directory HOLDING data is never touched."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_ENGINE_DIRNAME = "engine"
_DATA_IGNORABLE = {".gitkeep", ".DS_Store"}


def _repo_root() -> Path:
    # Runs BEFORE workspace resolution, so it cannot go through sutando_config.
    return Path(__file__).resolve().parent.parent  # lint-workspace-resolution: allow-repo-root


def _has_workspace_override(repo_root: Path) -> bool:
    """True only when config points workspace.path AWAY from `<repo>/workspace` —
    the tracked config ships the default as an explicit value on every install."""
    configured = None
    for name in ("sutando.config.local.json", "sutando.config.json"):
        try:
            cfg = json.loads((repo_root / name).read_text())
        except (OSError, ValueError):
            continue
        if isinstance(cfg, dict) and isinstance(cfg.get("workspace"), dict):
            path = cfg["workspace"].get("path")
            if path:
                configured = str(path)
                break  # local file wins; do not fall through to tracked
    if not configured:
        return False
    expanded = configured.replace("${REPO_DIR}", str(repo_root))
    expanded = os.path.expanduser(os.path.expandvars(expanded))
    try:
        # Resolve parents only — following the final component would let an
        # existing symlink AT the default path launder an override into "equal".
        return _canon_no_follow(Path(expanded)) != _canon_no_follow(repo_root / "workspace")
    except OSError:
        return True  # unresolvable custom path: treat as override, do not touch


def _canon_no_follow(p) -> Path:
    # str(Path) carries no trailing slash, so the dirname/basename split is stable.
    s = str(p)
    return Path(os.path.dirname(s)).resolve() / os.path.basename(s)


def app_workspace_target(repo_root: Path) -> Path | None:
    """The durable workspace an app-managed checkout should link to, or None."""
    parent = repo_root.parent
    if parent.name != APP_ENGINE_DIRNAME:
        return None
    target = parent.parent / "workspace"
    # The target must be a REAL directory — a symlink target could loop back
    # into the checkout.
    if target.is_dir() and not target.is_symlink():
        return target
    return None


def _dir_holds_data(path: Path) -> bool:
    try:
        return any(e.name not in _DATA_IGNORABLE for e in path.iterdir())
    except OSError:
        return True  # unreadable counts as data: never replace what we can't see


def inspect_layout(repo_root: Path | None = None) -> dict:
    """Classify the wiring without touching it. See module doc for states."""
    root = repo_root or _repo_root()
    ws = root / "workspace"
    target = app_workspace_target(root)
    report = {
        "path": str(ws),
        "app_target": str(target) if target else None,
        "state": "ok",
        "detail": "",
    }

    if _has_workspace_override(root):
        report["detail"] = "workspace.path override configured — symlink not load-bearing"
        return report

    if target is None:
        # Plain checkout: a real dir (or nothing yet) is the M0 default.
        report["detail"] = "plain checkout (no app layout) — nothing to enforce"
        return report

    if ws.is_symlink():
        try:
            resolved = ws.resolve(strict=True)
        except OSError:
            report["state"] = "dangling"
            report["detail"] = f"symlink target missing (points at {os.readlink(ws)!r})"
            return report
        if resolved == target.resolve():
            report["detail"] = "symlink -> durable workspace"
            return report
        report["state"] = "wrong-target"
        report["detail"] = f"symlink resolves to {resolved}, expected {target}"
        return report

    if not ws.exists():
        report["state"] = "missing"
        report["detail"] = "entry absent (deleted symlink?)"
        return report

    if ws.is_dir():
        if _dir_holds_data(ws):
            report["state"] = "materialized-with-data"
            report["detail"] = (
                "real directory holds files — a service ran while the symlink "
                "was gone; healing would orphan them"
            )
        else:
            report["state"] = "materialized-empty"
            report["detail"] = "real directory with no user data (empty or .gitkeep only)"
        return report

    report["state"] = "not-a-directory"
    report["detail"] = "workspace entry is a regular file"
    return report


def ensure_workspace_layout(repo_root: Path | None = None) -> dict:
    """Heal what is safe to heal; never delete user data. Returns the report."""
    root = repo_root or _repo_root()
    report = inspect_layout(root)
    target = app_workspace_target(root)
    ws = root / "workspace"
    state = report["state"]

    if state == "ok" or target is None:
        report["action"] = "none"
        return report

    link_value = os.path.relpath(target, root)

    if state in ("missing", "dangling", "wrong-target", "materialized-empty"):
        try:
            if ws.is_symlink() or ws.is_file():
                ws.unlink()
            elif ws.is_dir():
                # Re-verify at DELETE time, entry by entry: a file landing
                # after classification must abort the heal, never be unlinked.
                for entry in ws.iterdir():
                    if entry.name not in _DATA_IGNORABLE:
                        report["state"] = "materialized-with-data"
                        report["action"] = "left-broken"
                        report["detail"] += "; data appeared before heal — left untouched"
                        return report
                    entry.unlink()
                ws.rmdir()
            ws.symlink_to(link_value)
        except OSError as exc:
            report["action"] = "heal-failed"
            report["detail"] += f"; heal failed: {exc}"
            return report
        report["action"] = f"healed-{state}"
        report["detail"] += f"; relinked -> {link_value}"
        return report

    # materialized-with-data / not-a-directory: surface, don't destroy.
    report["action"] = "left-broken"
    return report


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--check"
    if mode == "--ensure":
        report = ensure_workspace_layout()
    else:
        report = inspect_layout()
        report["action"] = "check-only"
    healthy = report["state"] == "ok" or str(report.get("action", "")).startswith("healed-")
    stream = sys.stdout if healthy else sys.stderr
    print(json.dumps(report), file=stream)
    if not healthy:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
