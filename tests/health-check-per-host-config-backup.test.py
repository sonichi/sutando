#!/usr/bin/env python3
"""
Tests for `check_per_host_config_backup` in src/health-check.py.

sync-workspace snapshots each live <claude-home>/channels/<svc>/access.json into
hosts/<host>/channels/<svc>/access.json as a vault-carried backup. When that
snapshot silently stops refreshing (or reads the wrong claude-home), the backup
drifts stale while the live allowlist keeps changing — the owner's access config
LOOKS synced but the vault copy is out of date. This probe makes that drift a
first-class health signal.

Root cause it was built for (observed 2026-07-22): a discord access.json backup
6 weeks stale because the snapshot lost CLAUDE_CONFIG_DIR in the cron context and
copied a stale ~/.claude copy instead of the live workspace one.

Covers:
  a) backup byte-identical to live  → ok
  b) backup content differs         → warn (stale)
  c) backup file missing            → warn (no backup)
  d) no channels dir at all         → ok (nothing to back up)

Run: python3 tests/health-check-per-host-config-backup.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hc", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)


class _Harness:
    """Point the probe at a temp claude-home + workspace hosts/ tree.

    Overrides the three collaborators the probe reads:
      - hc.claude_home_path (live channels source)
      - hc.WORKSPACE_DIR    (carrier base is WORKSPACE_DIR/hosts/<host>/channels)
      - hc._host_label      (which host subtree is the carrier)
    """

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "claude-home"
        self.ws = tmp / "workspace"
        self._saved = {}

    def __enter__(self):
        self._saved = {
            "claude_home_path": hc.claude_home_path,
            "WORKSPACE_DIR": hc.WORKSPACE_DIR,
            "_host_label": hc._host_label,
        }

        def _chp(*parts):
            return self.home.joinpath(*parts)

        hc.claude_home_path = _chp
        hc.WORKSPACE_DIR = self.ws
        hc._host_label = lambda: "TestHost"
        return self

    def __exit__(self, *a):
        for k, v in self._saved.items():
            setattr(hc, k, v)

    def write_live(self, svc: str, content: bytes):
        d = self.home / "channels" / svc
        d.mkdir(parents=True, exist_ok=True)
        (d / "access.json").write_bytes(content)

    def write_carrier(self, svc: str, content: bytes):
        d = self.ws / "hosts" / "TestHost" / "channels" / svc
        d.mkdir(parents=True, exist_ok=True)
        (d / "access.json").write_bytes(content)


def case_a_identical_ok() -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        with _Harness(Path(td)) as h:
            h.write_live("discord", b'{"allowFrom":["123"]}')
            h.write_carrier("discord", b'{"allowFrom":["123"]}')
            r = hc.check_per_host_config_backup()
    if r["status"] != "ok":
        return [f"a) identical backup should be ok, got {r}"]
    return []


def case_b_diff_warn() -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        with _Harness(Path(td)) as h:
            h.write_live("discord", b'{"allowFrom":["123","456"]}')
            h.write_carrier("discord", b'{"allowFrom":["123"]}')  # stale
            r = hc.check_per_host_config_backup()
    if r["status"] != "warn" or "stale" not in r["detail"]:
        return [f"b) drifted backup should warn 'stale', got {r}"]
    return []


def case_c_missing_warn() -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        with _Harness(Path(td)) as h:
            h.write_live("discord", b'{"allowFrom":["123"]}')
            # no carrier written
            r = hc.check_per_host_config_backup()
    if r["status"] != "warn" or "no backup" not in r["detail"]:
        return [f"c) missing backup should warn 'no backup', got {r}"]
    return []


def case_d_no_channels_ok() -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        with _Harness(Path(td)) as h:
            # neither live nor carrier exists
            r = hc.check_per_host_config_backup()
    if r["status"] != "ok":
        return [f"d) no channels dir should be ok, got {r}"]
    return []


def main() -> int:
    cases = [
        ("a", case_a_identical_ok),
        ("b", case_b_diff_warn),
        ("c", case_c_missing_warn),
        ("d", case_d_no_channels_ok),
    ]
    failures = []
    for label, fn in cases:
        try:
            fails = fn()
        except Exception as e:  # noqa: BLE001
            fails = [f"{label}) raised {type(e).__name__}: {e}"]
        if fails:
            failures.extend(fails)
            print(f"  ✗ case {label}")
            for f in fails:
                print(f"      {f}")
        else:
            print(f"  ✓ case {label}")
    if failures:
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("\nper-host-config-backup drift probe invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
