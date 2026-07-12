#!/usr/bin/env python3
"""Regenerate the package modules from the canonical sutando src/ (single source).

The AG2 Space relay client lives canonically in sonichi/sutando `src/` (the core
runs it directly). This package is a *distribution* of those exact files — never
a hand-edited fork. Run this to refresh the copies; `--check` (used in CI/tests)
fails if the package has drifted from src/.

    python tools/sync_from_src.py          # regenerate
    python tools/sync_from_src.py --check   # verify in sync (exit 1 on drift)
"""
import sys
from pathlib import Path

# src file  ->  package module (hyphen→underscore for the bridge)
MAP = {
    "remote-gateway-bridge.py": "remote_gateway_bridge.py",
    "workspace_default.py": "workspace_default.py",
    "task_archive.py": "task_archive.py",
    "local_task_protocol.py": "local_task_protocol.py",
    "result_markers.py": "result_markers.py",
    "send_allowlist.py": "send_allowlist.py",
    "util_paths.py": "util_paths.py",
    "sutando_config.py": "sutando_config.py",
}
PKG_DIR = Path(__file__).resolve().parent.parent / "ag2_task_relay_client"
SRC_DIR = Path(__file__).resolve().parents[3] / "src"


def main() -> int:
    check = "--check" in sys.argv
    drift = []
    for src_name, pkg_name in MAP.items():
        src = SRC_DIR / src_name
        pkg = PKG_DIR / pkg_name
        if not src.exists():
            print(f"MISSING canonical source: {src}", file=sys.stderr)
            return 1
        want = src.read_text(encoding="utf-8")
        have = pkg.read_text(encoding="utf-8") if pkg.exists() else None
        if check:
            if have != want:
                drift.append(pkg_name)
        else:
            pkg.write_text(want, encoding="utf-8")
    if check:
        if drift:
            print("DRIFT — package out of sync with src/: " + ", ".join(drift), file=sys.stderr)
            print("Run: python tools/sync_from_src.py", file=sys.stderr)
            return 1
        print("in sync with src/ ✓")
    else:
        print(f"synced {len(MAP)} modules from {SRC_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
