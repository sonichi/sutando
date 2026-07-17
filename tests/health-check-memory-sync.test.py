#!/usr/bin/env python3
"""
Tests for health-check.py's check_memory_sync() opt-out behavior.

Context (owner ask 2026-07-10): cross-machine memory sync is OPT-IN, but the
health check warned "SUTANDO_MEMORY_REPO not set — cross-machine sync disabled"
on every tick — a permanent nag on any single-machine install, and it ignored a
deliberate config opt-out (vault.enabled=false). This surfaced as "noise" after
the 0.61 migration. Fix: report the disabled / not-configured cases as
informational (ok), not warn; a configured-but-stale sync still warns.

Covers: _vault_sync_disabled() (true/false/error) and the two early-return
branches of check_memory_sync (opt-out → ok, not-configured → ok).

Run: python3 tests/health-check-memory-sync.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

FAILURES = []


def check(name, cond, detail=""):
    print(f"{'  ok  ' if cond else '  FAIL '}{name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(f"{name}: {detail}")


def _run_mock(stdout):
    r = unittest.mock.MagicMock()
    r.stdout = stdout
    return r


def main() -> int:
    # NOTE (config-extraction refactor): the original #2069 shelled out to
    # sutando-config.sh via _vault_sync_disabled()/_vault_remote_url(). This
    # branch routes the SAME behavior through the canonical Python resolver
    # (_resolved_vault → sutando_config.resolve_vault). These tests now assert
    # the resolver-backed behavior, which is the point of the extraction.
    import os, time as _time

    def _vault(enabled=None, remote_url="", explicit_disable=False):
        return {"enabled": enabled, "remote_url": remote_url,
                "_explicit_disable": explicit_disable}

    # check_memory_sync: deliberate opt-out (vault.enabled=false) → ok (no nag).
    with unittest.mock.patch.object(hc, "_resolved_vault",
                                    return_value=_vault(enabled=False, explicit_disable=True)):
        r = hc.check_memory_sync()
    check("opt-out → ok", r["status"] == "ok", f"got {r!r}")
    check("opt-out detail mentions opt-out", "opt-out" in r["detail"], f"got {r!r}")

    # check_memory_sync: not opted out, nothing configured → ok (single-machine),
    # NOT warn.
    empty_repo = Path(tempfile.mkdtemp(prefix="sutando-hc-nosync-"))  # no .env inside
    with unittest.mock.patch.object(hc, "_resolved_vault", return_value=_vault()), \
         unittest.mock.patch.object(hc, "REPO_DIR", empty_repo):
        r = hc.check_memory_sync()
    check("not configured → ok (not warn)", r["status"] == "ok", f"got {r!r}")
    check("not-configured detail is non-scary", "not configured" in r["detail"], f"got {r!r}")

    # _resolved_vault unit behavior: fail-open safe defaults on resolver error.
    with unittest.mock.patch("sutando_config.resolve_vault", side_effect=OSError("boom")):
        v = hc._resolved_vault()
    check("_resolved_vault: error → safe defaults",
          v.get("enabled") is False and v.get("remote_url") == "", f"got {v!r}")

    # ── qingyun P1 regression (config-file hosts) ────────────────────────────
    # A host with vault.remote_url set in sutando.config (the CANONICAL setup)
    # and NO legacy .env alias must NOT be reported as "not configured" — that
    # false "ok" would silence real stale-sync alerts.

    # config-configured + workspace git repo + STALE fetch → warn (NOT the false ok)
    ws = Path(tempfile.mkdtemp(prefix="sutando-hc-ws-"))
    (ws / ".git").mkdir()
    fetch = ws / ".git" / "FETCH_HEAD"
    fetch.write_text("x")
    os.utime(fetch, (_time.time() - 96 * 3600, _time.time() - 96 * 3600))  # 96h old
    empty_repo2 = Path(tempfile.mkdtemp(prefix="sutando-hc-noenv-"))  # no .env
    with unittest.mock.patch.object(hc, "_resolved_vault",
                                    return_value=_vault(enabled=True, remote_url="https://vault.example/repo.git")), \
         unittest.mock.patch.object(hc, "REPO_DIR", empty_repo2), \
         unittest.mock.patch.object(hc, "WORKSPACE_DIR", ws):
        r = hc.check_memory_sync()
    check("config-file configured + stale fetch → warn (qingyun P1)",
          r["status"] == "warn" and "stale" in r["detail"], f"got {r!r}")

    # config-configured + workspace git repo + never fetched → ok 'never fetched',
    # NOT the 'not configured (single-machine mode)' false-positive.
    fetch.unlink()
    with unittest.mock.patch.object(hc, "_resolved_vault",
                                    return_value=_vault(enabled=True, remote_url="https://vault.example/repo.git")), \
         unittest.mock.patch.object(hc, "REPO_DIR", empty_repo2), \
         unittest.mock.patch.object(hc, "WORKSPACE_DIR", ws):
        r = hc.check_memory_sync()
    check("config-file configured + never fetched → 'never fetched' state (not 'not configured')",
          "never fetched" in r["detail"] and "not configured" not in r["detail"], f"got {r!r}")

    # Legacy .env fallback path: config has NO vault URL but REPO_DIR/.env
    # carries the deprecated SUTANDO_MEMORY_REPO alias → repo_url resolves from
    # .env, and (with a never-fetched workspace git repo) we get the
    # initialized state, NOT the 'not configured' false-ok.
    legacy_repo = Path(tempfile.mkdtemp(prefix="sutando-hc-legacy-"))
    (legacy_repo / ".env").write_text('SUTANDO_MEMORY_REPO="https://vault.example/legacy.git"\n')
    ws2 = Path(tempfile.mkdtemp(prefix="sutando-hc-ws2-"))
    (ws2 / ".git").mkdir()
    with unittest.mock.patch.object(hc, "_resolved_vault", return_value=_vault()), \
         unittest.mock.patch.object(hc, "REPO_DIR", legacy_repo), \
         unittest.mock.patch.object(hc, "WORKSPACE_DIR", ws2):
        r = hc.check_memory_sync()
    check("legacy .env SUTANDO_MEMORY_REPO → configured (not 'not configured')",
          "not configured" not in r["detail"], f"got {r!r}")

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall check_memory_sync opt-out cases passed")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
