"""Migration runner — applies pending NNNN-*.{sh,py} scripts under migrations/.

Called by src/startup.sh before services launch. Also importable for testing.

State lives at $SUTANDO_WORKSPACE/state/schema-version.json.
Exit 0 = all pending migrations applied (or none pending).
Exit non-zero = a migration script failed; startup should abort.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional


# ── state schema ──────────────────────────────────────────────────────────────

_EMPTY_STATE: dict = {"applied": [], "current": 0}


def _state_path(workspace: Path) -> Path:
    return workspace / "state" / "schema-version.json"


def read_state(workspace: Path) -> dict:
    p = _state_path(workspace)
    if not p.exists():
        return dict(_EMPTY_STATE)
    try:
        data = json.loads(p.read_text())
        data.setdefault("applied", [])
        data.setdefault("current", 0)
        return data
    except (json.JSONDecodeError, OSError):
        return dict(_EMPTY_STATE)


def write_state(workspace: Path, state: dict) -> None:
    p = _state_path(workspace)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: tmp + rename so a mid-write crash leaves a valid file.
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".schema-version-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── migration discovery ────────────────────────────────────────────────────────

def _migrations_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "migrations"


def list_pending(workspace: Path, migrations_dir: Optional[Path] = None) -> list[tuple[int, Path]]:
    """Return [(N, path), ...] for migrations not yet in state['applied'], sorted."""
    mdir = migrations_dir or _migrations_dir()
    state = read_state(workspace)
    applied: set[int] = set(state.get("applied", []))

    pending: list[tuple[int, Path]] = []
    if not mdir.is_dir():
        return pending

    for p in sorted(mdir.iterdir()):
        name = p.name
        if not p.is_file():
            continue
        if p.suffix not in (".sh", ".py"):
            continue
        parts = name.split("-", 1)
        if not parts[0].isdigit() or len(parts[0]) != 4:
            continue
        n = int(parts[0])
        if n not in applied:
            pending.append((n, p))

    return sorted(pending)


# ── runner ────────────────────────────────────────────────────────────────────

def run_migrations(
    workspace: Path,
    migrations_dir: Optional[Path] = None,
    dry_run: bool = False,
    engine_version: str = "",
) -> int:
    """Apply pending migrations. Returns 0 on success, 1 on failure."""
    pending = list_pending(workspace, migrations_dir)

    if not pending:
        return 0

    state = read_state(workspace)

    for n, script in pending:
        if dry_run:
            print(f"[dry-run] would apply migration {n:04d}: {script.name}")
            continue

        print(f"  Applying migration {n:04d}: {script.name}")
        env = {**os.environ, "SUTANDO_WORKSPACE": str(workspace), "DRY_RUN": "0"}

        if script.suffix == ".sh":
            cmd = ["bash", str(script)]
        else:
            cmd = [sys.executable, str(script)]

        result = subprocess.run(cmd, env=env, capture_output=True, text=True)

        if result.returncode != 0:
            print(f"  ✗ Migration {n:04d} failed (exit {result.returncode}): {script}", file=sys.stderr)
            if result.stderr.strip():
                for line in result.stderr.strip().splitlines()[-10:]:
                    print(f"    {line}", file=sys.stderr)
            return 1

        state["applied"] = sorted(set(state.get("applied", [])) | {n})
        state["current"] = max(state.get("current", 0), n)
        if engine_version:
            state["engine_version_at_apply"] = engine_version
        write_state(workspace, state)
        print(f"  ✓ Migration {n:04d} applied")

    return 0


# ── workspace_default idempotent bootstrap ────────────────────────────────────

_bootstrap_sentinel: bool = False


def _bootstrap_on_import() -> None:
    """Run migrations once on first import in manual claude sessions.

    Sentinel-gated so repeated imports are cheap.
    """
    global _bootstrap_sentinel
    if _bootstrap_sentinel:
        return
    _bootstrap_sentinel = True

    try:
        from workspace_default import resolve_workspace  # type: ignore[import]
        workspace = Path(resolve_workspace())
    except Exception:
        return

    try:
        run_migrations(workspace)
    except Exception:
        pass


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> int:
    dry_run = "--dry-run" in sys.argv

    try:
        from workspace_default import resolve_workspace  # type: ignore[import]
        workspace = Path(resolve_workspace())
    except ImportError:
        # Fallback when run outside the repo (e.g. in tests with a fake home).
        ws_env = os.environ.get("SUTANDO_WORKSPACE", "")
        if ws_env:
            workspace = Path(ws_env).expanduser()
        else:
            workspace = Path.home() / ".sutando" / "workspace"

    return run_migrations(workspace, dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(main())
