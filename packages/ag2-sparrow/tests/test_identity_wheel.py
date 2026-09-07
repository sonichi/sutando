#!/usr/bin/env python3
"""Wheel-manifest smoke test: the advertised API must survive installation.

The repo ships a FLAT explicit package list (see tests/sparrow-result-guard
history) — a subpackage present on disk but absent from [tool.setuptools]
packages is silently dropped from the wheel. This suite (a) requires every
package directory under ag2_sparrow/ to be configured, and (b) imports the
identity API from a simulated install containing ONLY the configured
packages, in a subprocess with no source checkout on sys.path.

Run: python3 packages/ag2-sparrow/tests/test_identity_wheel.py  (stdlib only)
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PKG_ROOT = Path(__file__).resolve().parent.parent


def configured_packages() -> list[str]:
    text = (PKG_ROOT / "pyproject.toml").read_text()
    m = re.search(r"^packages\s*=\s*\[([^\]]*)\]", text, re.MULTILINE)
    if not m:
        raise AssertionError("no [tool.setuptools] packages list found")
    return re.findall(r'"([^"]+)"', m.group(1))


def on_disk_packages() -> set[str]:
    found = set()
    for init in (PKG_ROOT / "ag2_sparrow").rglob("__init__.py"):
        rel = init.parent.relative_to(PKG_ROOT)
        found.add(".".join(rel.parts))
    return found


class WheelManifest(unittest.TestCase):
    def test_every_package_dir_is_configured(self):
        configured = set(configured_packages())
        missing = on_disk_packages() - configured
        self.assertFalse(
            missing,
            f"package dirs exist but are absent from pyproject packages "
            f"(they will be DROPPED from the wheel): {sorted(missing)}")

    def test_identity_api_imports_from_simulated_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp) / "site"
            for pkg in configured_packages():
                src = PKG_ROOT / Path(*pkg.split("."))
                dst = site / Path(*pkg.split("."))
                dst.mkdir(parents=True, exist_ok=True)
                for f in src.glob("*.py"):
                    shutil.copy2(f, dst / f.name)
            data = PKG_ROOT / "ag2_sparrow" / "identity" / "vectors.json"
            if (site / "ag2_sparrow" / "identity").is_dir() and data.is_file():
                shutil.copy2(data, site / "ag2_sparrow" / "identity" / data.name)
            neutral = Path(tmp) / "cwd"
            neutral.mkdir()
            proc = subprocess.run(
                [sys.executable, "-c",
                 "from ag2_sparrow.identity import ingress_task_id;"
                 "print(ingress_task_id('inst7', '9f3ab2').value)"],
                cwd=neutral, capture_output=True, text=True,
                env={"PYTHONPATH": str(site), "PATH": "/usr/bin:/bin"})
            self.assertEqual(
                proc.returncode, 0,
                f"identity API failed to import from the installed layout:\n"
                f"{proc.stderr}")
            self.assertEqual(proc.stdout.strip(), "task-inst7~9f3ab2")


if __name__ == "__main__":
    unittest.main()
