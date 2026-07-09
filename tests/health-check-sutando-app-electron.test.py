#!/usr/bin/env python3
"""
Tests for the sutando-app probe's Electron-impostor filter in
src/health-check.py (#2038).

The desktop UI also installs as "Sutando.app"; its main binary and Helper
processes match the probe's pgrep pattern, so pre-fix the probe reported
"running" while the actual Swift menu-bar app was dead. The filter drops
PIDs whose OUTERMOST .app bundle ships Contents/Frameworks/Sutando Helper.app
(an Electron marker the Swift app never has).

Covers:
  a) Electron main binary            → impostor (dropped)
  b) Electron Helper process         → resolves to outermost bundle → dropped
  c) Swift-style bundle (no helper)  → kept
  d) bare dev binary (no .app)       → kept
  e) _filter_electron_impostor_pids → drops impostors, keeps real, and
     fail-opens (keeps PID) when the ps lookup raises
  f) _ps_comm on a live PID          → returns a non-empty string

Run: python3 tests/health-check-sutando-app-electron.test.py
Exit code: 0 on pass, 1 on fail.
"""

from __future__ import annotations
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("health_check", REPO / "src" / "health-check.py")
hc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hc)

failures = []


def check(name: str, cond: bool):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"  FAIL {name}")
        failures.append(name)


with tempfile.TemporaryDirectory() as td:
    # Fake Electron bundle: Sutando.app WITH Contents/Frameworks/Sutando Helper.app
    electron = Path(td) / "Applications" / "Sutando.app"
    (electron / "Contents" / "Frameworks" / "Sutando Helper.app").mkdir(parents=True)
    (electron / "Contents" / "MacOS").mkdir(parents=True)
    electron_main = str(electron / "Contents" / "MacOS" / "Sutando")
    electron_helper = str(
        electron / "Contents" / "Frameworks" / "Sutando Helper (GPU).app"
        / "Contents" / "MacOS" / "Sutando Helper (GPU)"
    )

    # Fake Swift menu-bar bundle: Sutando.app WITHOUT helper frameworks
    swift = Path(td) / "src" / "Sutando" / "Sutando.app"
    (swift / "Contents" / "MacOS").mkdir(parents=True)
    swift_main = str(swift / "Contents" / "MacOS" / "Sutando")

    # Bare dev binary — no .app anywhere in the path
    dev_bin = str(Path(td) / "src" / "Sutando" / "Sutando")

    # a) Electron main binary is an impostor
    check("electron main → impostor", hc._is_electron_impostor(electron_main) is True)

    # b) Helper resolves to the OUTERMOST bundle (Sutando.app, not the
    #    nested helper bundle) and is therefore also an impostor
    check("electron helper → impostor via outermost bundle",
          hc._is_electron_impostor(electron_helper) is True)
    check("outermost bundle of helper is top-level Sutando.app",
          hc._outermost_bundle(electron_helper) == electron)

    # c) Swift-style bundle (no Sutando Helper.app) is kept
    check("swift bundle → not impostor", hc._is_electron_impostor(swift_main) is False)

    # d) Bare dev binary is kept (no bundle at all)
    check("dev binary → not impostor", hc._is_electron_impostor(dev_bin) is False)
    check("dev binary has no bundle", hc._outermost_bundle(dev_bin) is None)

    # e) _filter_electron_impostor_pids: drop impostors, keep real ones,
    #    fail-open on ps errors. Patch _ps_comm with a recording fake.
    comm_by_pid = {"1": electron_main, "2": swift_main, "3": dev_bin}

    def fake_ps_comm(pid):
        if pid == "4":
            raise RuntimeError("ps blew up")
        return comm_by_pid[pid]

    orig = hc._ps_comm
    hc._ps_comm = fake_ps_comm
    try:
        kept = hc._filter_electron_impostor_pids(["1", "2", "3", "4"])
    finally:
        hc._ps_comm = orig
    check("filter drops electron, keeps swift + dev + errored",
          kept == ["2", "3", "4"])

    # f) _ps_comm works against a real live PID (this test process)
    check("_ps_comm returns non-empty for live pid",
          bool(hc._ps_comm(str(os.getpid()))))

if failures:
    print(f"\n{len(failures)} failure(s): {failures}")
    sys.exit(1)
print("\nall tests passed")
sys.exit(0)
