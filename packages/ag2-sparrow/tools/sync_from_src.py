#!/usr/bin/env python3
"""Regenerate the package modules from the canonical sutando src/ (single source).

Everything in MAP below is bundled verbatim from sonichi/sutando `src/`, which is
canonical for those modules — currently 15 of them, including outbox.py and its
transport seam outbox_adapter.py. Only the modules NOT in MAP are package-canonical
and intentionally diverge from src (remote_gateway_bridge, _dirs, send_allowlist:
dir-interface, no workspace-resolution).

MAP is the authority on which is which. This docstring named three modules for a
long time after MAP grew past them, so it read as "outbox is not covered" — the
opposite of what --check does. When you add an entry, the count above is the only
thing here that needs updating; better still, read MAP.

The relay client lives canonically in sonichi/sutando `src/` (the core
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
    "task_archive.py": "task_archive.py",
    "local_task_protocol.py": "local_task_protocol.py",
    "result_markers.py": "result_markers.py",
    "delivery/readiness.py": "result_ready.py",
    "dedup_recovery.py": "dedup_recovery.py",
    "file_lock.py": "file_lock.py",
    "workspace_lock.py": "workspace_lock.py",
    "chat_secret_filter.py": "chat_secret_filter.py",
    "policy/egress/result.py": "team_result_guard.py",
    "policy/guardrail.py": "team_guardrail.py",
    "vault_set_grammar.py": "vault_set_grammar.py",
    "send_failure_policy.py": "send_failure_policy.py",
    # send_failure_policy.resolve_failed_send imports it; vendoring one without
    # the other ships a copy that dies on first call (ModuleNotFoundError).
    "proactive_recovery.py": "proactive_recovery.py",
    # outbox core + its transport seam: src-canonical like send_failure_policy,
    # so the coverage gate (source = src) can see them.
    "outbox.py": "outbox.py",
    "outbox_adapter.py": "outbox_adapter.py",
}
PKG_DIR = Path(__file__).resolve().parent.parent / "ag2_sparrow"
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
