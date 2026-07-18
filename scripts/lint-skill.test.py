#!/usr/bin/env python3
"""Tests for scripts/lint-skill.py — the skill-manifest v1 validator.

Stdlib only. Builds temp skill dirs and asserts the linter's errors/warnings.
Run: python3 scripts/lint-skill.test.py
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "lint_skill", Path(__file__).resolve().parent / "lint-skill.py")
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  ok  " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def _skill(tmp: Path, name: str, manifest: dict | None, files: dict | None = None) -> Path:
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (d / "manifest.json").write_text(json.dumps(manifest))
    for fn, body in (files or {}).items():
        (d / fn).write_text(body)
    return d


def errs(d: Path):
    return lint._lint_manifest(d)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lint-skill-test-"))

    # 1. a fully valid manifest → no errors, no warnings
    good = _skill(tmp, "good-skill", {
        "name": "good-skill", "version": "1.2.3", "owner": "me",
        "stability": "stable", "permissions": {"network": False, "filesystem": "read-only", "secrets": "none"},
    })
    e, w = errs(good)
    check(e == [] and w == [], "valid manifest → no errors/warnings")

    # 2. missing required fields
    e, _ = errs(_skill(tmp, "bare", {"name": "bare"}))
    check(any("version" in x for x in e) and any("owner" in x for x in e)
          and any("stability" in x for x in e),
          "missing version/owner/stability all flagged")

    # 3. bad semver
    e, _ = errs(_skill(tmp, "badver", {"name": "badver", "version": "1.2", "owner": "m", "stability": "stable"}))
    check(any("SemVer" in x for x in e), "non-SemVer version flagged")

    # 4. name must match directory
    e, _ = errs(_skill(tmp, "dirname", {"name": "other", "version": "1.0.0", "owner": "m", "stability": "stable"}))
    check(any("does not match directory" in x for x in e), "name/dir mismatch flagged")

    # 5. bad stability enum
    e, _ = errs(_skill(tmp, "badstab", {"name": "badstab", "version": "1.0.0", "owner": "m", "stability": "meh"}))
    check(any("stability" in x for x in e), "invalid stability flagged")

    # 6. tools without enabled/access_tier
    e, _ = errs(_skill(tmp, "toolsonly",
                       {"name": "toolsonly", "version": "1.0.0", "owner": "m", "stability": "stable", "tools": "./tools.ts"},
                       files={"tools.ts": "export const tools = []"}))
    check(any("enabled" in x for x in e) and any("access_tier" in x for x in e),
          "tools without enabled/access_tier flagged")

    # 7. permission cross-check: network:false but code fetches → warning
    e, w = errs(_skill(tmp, "liar",
                       {"name": "liar", "version": "1.0.0", "owner": "m", "stability": "stable",
                        "permissions": {"network": False}},
                       files={"run.py": "import urllib.request\nurllib.request.urlopen('http://x')"}))
    check(e == [] and any("network=false" in x for x in w),
          "network:false + real network call → warning (not error)")

    # 8. network:true + network code → no warning
    _, w = errs(_skill(tmp, "honest",
                       {"name": "honest", "version": "1.0.0", "owner": "m", "stability": "stable",
                        "permissions": {"network": True}},
                       files={"run.py": "import requests\nrequests.get('http://x')"}))
    check(w == [], "network:true + network call → no warning")

    # 9. invalid JSON
    d = tmp / "brokenjson"
    d.mkdir()
    (d / "manifest.json").write_text("{ not json")
    e, _ = errs(d)
    check(any("invalid JSON" in x for x in e), "invalid JSON flagged")

    # 10. valid optional scope (SkillPack @scope/name mapping) → no error
    e, w = errs(_skill(tmp, "scoped", {
        "name": "scoped", "version": "1.0.0", "owner": "m", "stability": "stable",
        "scope": "@sutando",
    }))
    check(e == [] and w == [], "valid '@scope' → no error/warning")

    # 11. malformed scope (missing '@') → error
    e, _ = errs(_skill(tmp, "badscope", {
        "name": "badscope", "version": "1.0.0", "owner": "m", "stability": "stable",
        "scope": "sutando",
    }))
    check(any("scope" in x for x in e), "scope without '@' flagged")

    # 12. access_tier must match the loader contract (owner|any_caller), not the
    #     owner/team/other task tiers. any_caller accepted; other/team rejected.
    e, _ = errs(_skill(tmp, "anycaller",
                       {"name": "anycaller", "version": "1.0.0", "owner": "m", "stability": "stable",
                        "access_tier": "any_caller", "enabled": True, "tools": "./tools.ts"},
                       files={"tools.ts": "export const tools = []"}))
    check(e == [], "access_tier 'any_caller' accepted (loader contract)")
    e, _ = errs(_skill(tmp, "othertier",
                       {"name": "othertier", "version": "1.0.0", "owner": "m", "stability": "stable",
                        "access_tier": "other", "enabled": True, "tools": "./tools.ts"},
                       files={"tools.ts": "export const tools = []"}))
    check(any("access_tier" in x for x in e), "access_tier 'other' rejected (not a loader tier)")

    # 13. permission cross-check catches the common shell forms `curl -s` and no-arg
    #     `fetch()` — previously slipped past the trailing-\b in NET_SIGNALS.
    _, w = errs(_skill(tmp, "curldash",
                       {"name": "curldash", "version": "1.0.0", "owner": "m", "stability": "stable",
                        "permissions": {"network": False}},
                       files={"run.sh": "curl -s https://x.com/data"}))
    check(any("network=false" in x for x in w), "`curl -s` flagged (network:false lie)")
    _, w = errs(_skill(tmp, "bareFetch",
                       {"name": "bareFetch", "version": "1.0.0", "owner": "m", "stability": "stable",
                        "permissions": {"network": False}},
                       files={"run.js": "const r = await fetch()"}))
    check(any("network=false" in x for x in w), "no-arg `fetch()` flagged (network:false lie)")

    # 14. a tools path escaping the skill dir ('..') is a hard error.
    e, _ = errs(_skill(tmp, "escape",
                       {"name": "escape", "version": "1.0.0", "owner": "m", "stability": "stable",
                        "access_tier": "owner", "enabled": True, "tools": "../shared/tools.ts"}))
    check(any("escape the skill dir" in x for x in e), "tools path with '..' rejected")

    print(f"\n{'PASS — all checks green' if not FAILS else f'FAIL — {len(FAILS)} failing'}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
