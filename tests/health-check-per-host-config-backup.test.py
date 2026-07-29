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
  a) backup byte-identical to live      → ok
  b) durable backup content differs     → warn (stale)
  c) backup file missing                → warn (no backup)
  d) no channels dir at all             → ok (nothing to back up)
  h) unreadable LIVE access.json        → warn (never a false all-clear; #2277 review)
  i) only volatile `pending` differs    → ok (durable config matches; #2277 review)
  j) durable drift + pending differs    → warn (pending doesn't mask real drift)
  k) malformed JSON, bytes differ       → warn (raw-byte fallback)
  l) malformed JSON, bytes identical    → ok (raw-byte fallback)

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


def case_e_unresolvable_home_ok() -> list[str]:
    """claude_home_path raising must degrade to ok, never crash the run."""
    with tempfile.TemporaryDirectory() as td:
        with _Harness(Path(td)) as h:
            def _boom(*_a):
                raise RuntimeError("no home")
            hc.claude_home_path = _boom
            r = hc.check_per_host_config_backup()
    if r["status"] != "ok" or "resolvable" not in r["detail"]:
        return [f"e) unresolvable claude-home should be ok, got {r}"]
    return []


def case_f_channels_dir_but_no_access_ok() -> list[str]:
    """channels/ exists but holds no access.json → nothing to back up → ok."""
    with tempfile.TemporaryDirectory() as td:
        with _Harness(Path(td)) as h:
            (h.home / "channels" / "discord").mkdir(parents=True)  # no access.json
            r = hc.check_per_host_config_backup()
    if r["status"] != "ok" or "no channel access.json" not in r["detail"]:
        return [f"f) channels dir with no access.json should be ok, got {r}"]
    return []


def case_g_unreadable_carrier_warn() -> list[str]:
    """A carrier that can't be read (here: a directory in its place) → warn."""
    with tempfile.TemporaryDirectory() as td:
        with _Harness(Path(td)) as h:
            h.write_live("discord", b'{"allowFrom":["123"]}')
            # carrier path exists but is a directory → read_bytes raises OSError
            (h.ws / "hosts" / "TestHost" / "channels" / "discord" / "access.json").mkdir(parents=True)
            r = hc.check_per_host_config_backup()
    if r["status"] != "warn" or "unreadable backup" not in r["detail"]:
        return [f"g) unreadable carrier should warn 'unreadable backup', got {r}"]
    return []


def case_h_unreadable_live_warn() -> list[str]:
    """A live access.json that can't be read (here: a directory in its place) must
    WARN, not be silently skipped. This is the lone-unreadable-live entry: skipping
    it let checked fall to 0 → a false 'no channel access.json to back up' all-clear,
    masking the exact failure this probe exists to surface (qingyun, #2277 review).
    Non-fatal: it flags as drift, never crashes the run."""
    with tempfile.TemporaryDirectory() as td:
        with _Harness(Path(td)) as h:
            # live path is a directory → read_bytes raises OSError
            (h.home / "channels" / "discord" / "access.json").mkdir(parents=True)
            r = hc.check_per_host_config_backup()
    if r["status"] != "warn" or "live unreadable" not in r["detail"]:
        return [f"h) unreadable live entry should warn 'live unreadable', got {r}"]
    return []


def case_i_pending_only_diff_ok() -> list[str]:
    """Volatile `pending` pairing codes differ but durable config matches → ok.

    Regression for the #2277 review: pending codes churn ~hourly, so a raw byte
    compare flagged healthy backups. Normalizing out `pending` must treat this
    as current, not drift.
    """
    with tempfile.TemporaryDirectory() as td:
        with _Harness(Path(td)) as h:
            h.write_live("discord", b'{"allowFrom":["123"],"pending":{"s6288g":1784600000}}')
            h.write_carrier("discord", b'{"allowFrom":["123"],"pending":{"eo0355":1784500000}}')
            r = hc.check_per_host_config_backup()
    if r["status"] != "ok":
        return [f"i) pending-only difference should be ok (durable config matches), got {r}"]
    return []


def case_j_durable_drift_despite_pending_warn() -> list[str]:
    """Durable allowlist drift must still warn even when pending also differs —
    normalizing pending must not mask a real allowlist/tier change."""
    with tempfile.TemporaryDirectory() as td:
        with _Harness(Path(td)) as h:
            h.write_live("discord", b'{"allowFrom":["123","789"],"pending":{"s6288g":1784600000}}')
            h.write_carrier("discord", b'{"allowFrom":["123"],"pending":{"eo0355":1784500000}}')
            r = hc.check_per_host_config_backup()
    if r["status"] != "warn" or "stale" not in r["detail"]:
        return [f"j) durable allowlist drift should warn 'stale' despite pending diff, got {r}"]
    return []


def case_k_malformed_json_raw_fallback_warn() -> list[str]:
    """When a side isn't parseable JSON, fall back to a raw byte compare and
    flag any difference (can't isolate durable config → stay conservative)."""
    with tempfile.TemporaryDirectory() as td:
        with _Harness(Path(td)) as h:
            h.write_live("discord", b'{"allowFrom":["123"]}')
            h.write_carrier("discord", b'{not valid json')  # malformed → raw compare
            r = hc.check_per_host_config_backup()
    if r["status"] != "warn" or "stale" not in r["detail"]:
        return [f"k) malformed backup should warn 'stale' via raw fallback, got {r}"]
    return []


def case_l_malformed_but_byte_identical_ok() -> list[str]:
    """Malformed JSON on both sides but byte-identical → raw fallback sees no
    diff → ok (a garbled-but-matching backup isn't drift)."""
    with tempfile.TemporaryDirectory() as td:
        with _Harness(Path(td)) as h:
            h.write_live("discord", b'{not valid json')
            h.write_carrier("discord", b'{not valid json')
            r = hc.check_per_host_config_backup()
    if r["status"] != "ok":
        return [f"l) byte-identical malformed pair should be ok via raw fallback, got {r}"]
    return []


def main() -> int:
    cases = [
        ("a", case_a_identical_ok),
        ("b", case_b_diff_warn),
        ("c", case_c_missing_warn),
        ("d", case_d_no_channels_ok),
        ("e", case_e_unresolvable_home_ok),
        ("f", case_f_channels_dir_but_no_access_ok),
        ("g", case_g_unreadable_carrier_warn),
        ("h", case_h_unreadable_live_warn),
        ("i", case_i_pending_only_diff_ok),
        ("j", case_j_durable_drift_despite_pending_warn),
        ("k", case_k_malformed_json_raw_fallback_warn),
        ("l", case_l_malformed_but_byte_identical_ok),
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
