#!/usr/bin/env python3
"""`vault.sync.exclude_extra` adds a carve-out without dropping the shipped set.

`_deep_merge` replaces lists, so a local `vault.sync.exclude` override silently
loses the shipped carve-outs — nothing errors, the carrier just stops excluding
them. This is the additive path, and the tests assert the failure it prevents as
well as the behaviour it adds.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("sutando_config", REPO / "src" / "sutando_config.py")
sc = importlib.util.module_from_spec(spec)
sys.modules["sutando_config"] = sc
spec.loader.exec_module(sc)

failures = []


def check(label, cond, extra=""):
    if cond:
        print(f"  ok   {label}")
    else:
        failures.append(label)
        print(f"  FAIL {label}  {extra}")


def _resolve(shipped: dict, local: Optional[dict]):
    """resolve_vault() against a throwaway repo root, never the real config."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "sutando.config.json").write_text(json.dumps(shipped))
        if local is not None:
            (root / "sutando.config.local.json").write_text(json.dumps(local))
        return sc.resolve_vault(root)


SHIPPED = {"vault": {"enabled": True, "sync": {
    "include": ["notes/", "hosts/*/"],
    "exclude": ["tasks/", "results/"],
}}}

print("the defect this exists to prevent:")
v = _resolve(SHIPPED, {"vault": {"sync": {"exclude": ["notes/generated/"]}}})
check("a local `exclude` override still REPLACES (documented _deep_merge behaviour)",
      v["sync"]["exclude"] == ["notes/generated/"], str(v["sync"]["exclude"]))
check("...so the shipped carve-outs are gone — silently",
      "tasks/" not in v["sync"]["exclude"], str(v["sync"]["exclude"]))

print("\nexclude_extra is additive:")
v = _resolve(SHIPPED, {"vault": {"sync": {"exclude_extra": ["notes/generated/", "notes/media/"]}}})
check("shipped carve-outs survive",
      {"tasks/", "results/"} <= set(v["sync"]["exclude"]), str(v["sync"]["exclude"]))
check("the added paths are present",
      {"notes/generated/", "notes/media/"} <= set(v["sync"]["exclude"]), str(v["sync"]["exclude"]))
check("shipped denies stay FIRST (gitignore is last-match-wins)",
      v["sync"]["exclude"][:2] == ["tasks/", "results/"], str(v["sync"]["exclude"]))
check("exclude_extra does not leak into the resolved schema",
      "exclude_extra" not in v["sync"], str(v["sync"].keys()))

print("\nedges:")
v = _resolve(SHIPPED, {"vault": {"sync": {"exclude_extra": ["tasks/", "notes/media/"]}}})
check("restating a shipped path does not duplicate it",
      v["sync"]["exclude"].count("tasks/") == 1, str(v["sync"]["exclude"]))

v = _resolve(SHIPPED, {"vault": {"sync": {"exclude_extra": []}}})
check("an empty exclude_extra changes nothing",
      v["sync"]["exclude"] == ["tasks/", "results/"], str(v["sync"]["exclude"]))

v = _resolve(SHIPPED, None)
check("no local config → shipped list unchanged",
      v["sync"]["exclude"] == ["tasks/", "results/"], str(v["sync"]["exclude"]))
check("...and no exclude_extra key is invented",
      "exclude_extra" not in v["sync"], str(v["sync"].keys()))

# Both together: an operator who deliberately REPLACES and also adds.
v = _resolve(SHIPPED, {"vault": {"sync": {"exclude": ["only/this/"],
                                         "exclude_extra": ["plus/that/"]}}})
check("explicit replace is still honoured, with extra appended to the REPLACEMENT",
      v["sync"]["exclude"] == ["only/this/", "plus/that/"], str(v["sync"]["exclude"]))

# include must NOT gain additive behaviour — it is a whitelist.
v = _resolve(SHIPPED, {"vault": {"sync": {"include": ["only/"]}}})
check("include keeps replace semantics (no accidental widening)",
      v["sync"]["include"] == ["only/"], str(v["sync"]["include"]))

if failures:
    print(f"\nFAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("\nPASS — vault exclude_extra additive")
