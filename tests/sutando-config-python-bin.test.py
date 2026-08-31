#!/usr/bin/env python3
"""Contract test for `scripts/sutando-config.sh python-bin` (voice-reliability
impl plan amendment T1).

The subcommand must print an ABSOLUTE interpreter path only after a smoke test
that EXECUTES the interpreter and imports `fcntl`, else exit non-zero with an
actionable message. Previously it printed whatever $PY resolved to, unverified
("prints the interpreter to TRY FIRST") — so callers that skipped their own
probe shelled a broken interpreter (the Xcode-stub trap class).

Tiers covered: explicit ($SUTANDO_PY, valid + broken), bundled
(<engine>/../runtime/python/bin/python3), valid PATH python3, and
absent-interpreter (fail closed, actionable). The true Xcode-CLT-stub tier
(system python3 with no developer tools) cannot be simulated off /usr/bin;
its two halves are pinned separately: resolve_python() refuses the stub
WITHOUT executing it (tests/python-binary-sh.test.sh) and this file pins that
a chosen-but-broken interpreter is rejected by the smoke test and that an
empty resolution fails closed.

Run: python3 tests/sutando-config-python-bin.test.py
Exit: 0 = all pass, 1 = failure
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REAL_PY = Path(sys.executable or shutil.which("python3")).resolve()

failures = []


def check(name, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(name)


def run_python_bin(script_dir, env_overrides, path):
    env = {k: v for k, v in os.environ.items() if k not in ("SUTANDO_PY",)}
    env.update(env_overrides)
    env["PATH"] = path
    return subprocess.run(
        ["/bin/bash", str(script_dir / "sutando-config.sh"), "python-bin"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def main():
    tmp = Path(tempfile.mkdtemp(prefix="python-bin-test-"))
    try:
        # --- explicit $SUTANDO_PY, valid interpreter ---
        p = run_python_bin(REPO / "scripts", {"SUTANDO_PY": str(REAL_PY)}, "/usr/bin:/bin")
        check("explicit SUTANDO_PY exits 0", p.returncode == 0, p.stderr)
        check("explicit output is absolute", p.stdout.startswith("/"), p.stdout)
        check(
            "explicit output executes and imports fcntl",
            subprocess.run([p.stdout.strip(), "-c", "import fcntl"], capture_output=True).returncode == 0,
        )

        # --- explicit $SUTANDO_PY, broken interpreter → smoke test rejects ---
        broken = tmp / "broken-python3"
        broken.write_text("#!/bin/sh\nexit 47\n")
        broken.chmod(0o755)
        p = run_python_bin(REPO / "scripts", {"SUTANDO_PY": str(broken)}, "/usr/bin:/bin")
        check("broken interpreter exits non-zero", p.returncode != 0, str(p.returncode))
        check("broken interpreter names the smoke test", "smoke test" in p.stderr, p.stderr)
        check("broken interpreter prints nothing on stdout", p.stdout == "", p.stdout)

        # --- bundled tier: <engine>/../runtime/python/bin/python3 ---
        engine = tmp / "engine"
        (engine / "scripts").mkdir(parents=True)
        for f in ("sutando-config.sh", "python-binary.sh"):
            shutil.copy(REPO / "scripts" / f, engine / "scripts" / f)
        bundled_bin = tmp / "runtime" / "python" / "bin"
        bundled_bin.mkdir(parents=True)
        (bundled_bin / "python3").symlink_to(REAL_PY)
        p = run_python_bin(engine / "scripts", {}, "/usr/bin:/bin")
        check("bundled tier exits 0", p.returncode == 0, p.stderr)
        expect = str(bundled_bin.resolve() / "python3")
        check(
            "bundled output is the normalized absolute bundle path (no '..')",
            p.stdout == expect and ".." not in p.stdout,
            f"got {p.stdout!r}, want {expect!r}",
        )

        # --- valid PATH python3 (no SUTANDO_PY, no bundle) ---
        pathbin = tmp / "pathbin"
        pathbin.mkdir()
        (pathbin / "python3").symlink_to(REAL_PY)
        p = run_python_bin(REPO / "scripts", {}, f"{pathbin}:/usr/bin:/bin")
        check("PATH tier exits 0", p.returncode == 0, p.stderr)
        check(
            "PATH tier output is the absolute PATH entry",
            p.stdout == str(pathbin.resolve() / "python3"),
            p.stdout,
        )

        # --- absent interpreter: fail closed with an actionable message ---
        # PATH with only the shell utilities sutando-config.sh itself needs —
        # no python3 anywhere, no bundle, no SUTANDO_PY.
        toolbin = tmp / "toolbin"
        toolbin.mkdir()
        for tool in ("dirname", "basename", "uname"):
            src = shutil.which(tool, path="/usr/bin:/bin")
            (toolbin / tool).symlink_to(src)
        p = run_python_bin(REPO / "scripts", {}, str(toolbin))
        check("absent interpreter exits non-zero", p.returncode != 0, str(p.returncode))
        check("absent interpreter prints nothing on stdout", p.stdout == "", p.stdout)
        check(
            "absent interpreter message is actionable",
            "python3" in p.stderr and ("SUTANDO_PY" in p.stderr or "brew install" in p.stderr),
            p.stderr,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f"\n{len(failures)} failure(s): {failures}")
        return 1
    print("\nAll python-bin contract tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
