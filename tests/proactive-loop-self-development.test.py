#!/usr/bin/env python3
"""Regression tests for the proactive-loop self-development policy gate."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "skills/proactive-loop/scripts/self-development-enabled.py"
MANIFEST = REPO / "skills/proactive-loop/manifest.json"
ENV_NAME = "SUTANDO_SELF_DEVELOPMENT_ENABLED"

spec = importlib.util.spec_from_file_location("self_development_gate", SCRIPT)
assert spec and spec.loader
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

failures = []


def check(label: str, condition: bool) -> None:
    if condition:
        print(f"PASS  {label}")
    else:
        print(f"FAIL  {label}")
        failures.append(label)


manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
check("manifest declares the feature flag", ENV_NAME in manifest.get("config", {}))
check("shipped manifest default is enabled", gate.self_development_enabled({}))

for value in ("1", "true", "YES", "on", "enabled"):
    check(f"truthy override {value!r}", gate.self_development_enabled({ENV_NAME: value}))

for value in ("0", "false", "NO", "off", "disabled"):
    check(f"false override {value!r}", not gate.self_development_enabled({ENV_NAME: value}))

check("invalid override fails closed", not gate.self_development_enabled({ENV_NAME: "maybe"}))

with tempfile.TemporaryDirectory() as td:
    missing = Path(td) / "missing-manifest.json"
    check(
        "missing manifest fails closed",
        not gate.self_development_enabled({}, manifest_path=missing),
    )
    malformed = Path(td) / "malformed-manifest.json"
    malformed.write_text('{"config": []}', encoding="utf-8")
    check(
        "malformed manifest config fails closed",
        not gate.self_development_enabled({}, manifest_path=malformed),
    )

disabled = subprocess.run(
    ["python3", str(SCRIPT)],
    env={ENV_NAME: "0"},
    capture_output=True,
    text=True,
    check=True,
)
check("CLI reports disabled", disabled.stdout.strip() == "disabled")

invalid = subprocess.run(
    ["python3", str(SCRIPT)],
    env={ENV_NAME: "surprise"},
    capture_output=True,
    text=True,
    check=True,
)
check("CLI invalid value reports disabled", invalid.stdout.strip() == "disabled")
check("CLI invalid value warns", "invalid" in invalid.stderr)


def run_main(value: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch.dict(os.environ, {ENV_NAME: value}, clear=True):
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = gate.main()
    return result, stdout.getvalue(), stderr.getvalue()


rc, stdout, stderr = run_main("1")
check("main enabled path", rc == 0 and stdout.strip() == "enabled" and not stderr)
rc, stdout, stderr = run_main("0")
check("main disabled path", rc == 0 and stdout.strip() == "disabled" and not stderr)
rc, stdout, stderr = run_main("unexpected")
check(
    "main invalid path fails closed with warning",
    rc == 0 and stdout.strip() == "disabled" and "invalid" in stderr,
)

skill_text = (REPO / "skills/proactive-loop/SKILL.md").read_text(encoding="utf-8")
check("loop invokes the gate", "self-development-enabled.py" in skill_text)
check("disabled loop still handles owner tasks", "Owner-requested" in skill_text)
check("manual invocation cannot bypass", "does not override the policy" in skill_text)

for launcher in (
    REPO / "src/agent/claude/cli/start-cli.sh",
    REPO / "src/agent/codex/cli/start-cli.sh",
):
    text = launcher.read_text(encoding="utf-8")
    check(
        f"{launcher.parent.parent.name} launcher forwards override",
        f'-e "{ENV_NAME}=${ENV_NAME}"' in text,
    )
    check(
        f"{launcher.parent.parent.name} launcher preserves an explicit empty override",
        f'"${{{ENV_NAME}+x}}" = x' in text,
    )

if failures:
    raise SystemExit(f"{len(failures)} failure(s): {', '.join(failures)}")
print("\nall tests passed")
