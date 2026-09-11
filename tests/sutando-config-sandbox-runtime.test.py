#!/usr/bin/env python3
"""sandbox.runtime selects what answers non-owner tasks: codex by default, gemini
on installs without Codex. Read like core.runtime: env override first, then
merged config, then the default, and anything else is refused by name."""
from __future__ import annotations

import importlib
import json
import os
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
cfg = importlib.import_module("sutando_config")

fails = []


def check(cond, label):
    print(("  ok   " if cond else "  FAIL ") + label)
    if not cond:
        fails.append(label)


def repo_with(config: dict) -> pathlib.Path:
    root = pathlib.Path(tempfile.mkdtemp(prefix="sandbox-runtime-"))
    (root / "sutando.config.json").write_text(json.dumps(config))
    cfg._reset_cache_for_tests()
    return root


os.environ.pop("SUTANDO_SANDBOX_RUNTIME", None)

check(cfg.resolve_sandbox_runtime(repo_with({})) == "codex", "default is codex")
check(cfg.resolve_sandbox_runtime(repo_with({"sandbox": {}})) == "codex",
      "an empty sandbox block is the default")
check(cfg.resolve_sandbox_runtime(repo_with({"sandbox": {"runtime": "gemini"}})) == "gemini",
      "sandbox.runtime: gemini")
check(cfg.resolve_sandbox_runtime(repo_with({"sandbox": {"runtime": " codex "}})) == "codex",
      "whitespace is stripped")

os.environ["SUTANDO_SANDBOX_RUNTIME"] = "gemini"
check(cfg.resolve_sandbox_runtime(repo_with({"sandbox": {"runtime": "codex"}})) == "gemini",
      "SUTANDO_SANDBOX_RUNTIME overrides the file")
os.environ.pop("SUTANDO_SANDBOX_RUNTIME")

try:
    cfg.resolve_sandbox_runtime(repo_with({"sandbox": {"runtime": "claude"}}))
    check(False, "an unsupported runtime is refused")
except ValueError as exc:
    check("claude" in str(exc) and "codex, gemini" in str(exc),
          "an unsupported runtime is refused by name, listing what is supported")

check("sandbox" in cfg._KNOWN_TOP_LEVEL_KEYS, "sandbox is a known top-level key, so no unknown-key warning")
check(cfg.resolve_core_runtime(repo_with({"sandbox": {"runtime": "gemini"}})) == "claude",
      "the sandbox runtime does not touch the core runtime")

if fails:
    print(f"\n{len(fails)} FAILED")
    sys.exit(1)
print("\nall passed")
