#!/usr/bin/env python3
"""Regression tests for stale bridge logs after a successful restart."""

import importlib.util
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("health_check", REPO / "src/health-check.py")
health_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health_check)

with tempfile.TemporaryDirectory() as td:
    log = Path(td) / "discord-bridge.log"
    log.write_text("discord.errors.LoginFailure: Improper token\n")
    now = time.time()

    os.utime(log, (now - 60, now - 60))
    assert not health_check._bridge_log_belongs_to_process(log, now)

    os.utime(log, (now + 1, now + 1))
    assert health_check._bridge_log_belongs_to_process(log, now)

    assert health_check._bridge_log_belongs_to_process(log, None)

    with mock.patch.object(Path, "stat", side_effect=OSError("log disappeared")):
        assert health_check._bridge_log_belongs_to_process(log, now)

print("PASS: stale bridge logs cannot override a newer live process")
