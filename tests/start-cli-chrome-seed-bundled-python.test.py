#!/usr/bin/env python3
"""Regression test for the Chrome-onboarding seed interpreter resolution in
start-cli.sh (sonichi#2433 review, marklysze blocker).

The seed that writes `hasCompletedClaudeInChromeOnboarding` runs an inline
python. On a FRESH Mac there is no real system `python3` — bare `python3`
resolves to Apple's Xcode-CLT stub, which prints an "install developer tools"
notice and returns nothing. If the seed uses bare `python3` (guarded by
`command -v python3`, which the stub satisfies), the seed silently no-ops on
exactly the fresh-Mac environment it exists to repair — the detached core then
hangs at the Chrome acknowledgement prompt and "Say hello" times out.

The fix resolves the interpreter into `$PY` (SUTANDO_PY → bundled
`<engine>/runtime/python` → system python3) and uses `"$PY"` for the seed, with
a `"$PY" -c 'import sys'` guard (proves it RUNS, not just that a name exists).

This test is SOURCE-TIED, not a full launch of start-cli.sh: it extracts the
seed's PY heredoc verbatim from the script and reproduces the script's own
resolver + guard in a small shell harness, then runs the extracted seed under a
simulated clean-Mac env — a working interpreter in SUTANDO_PY, and a `python3`
stub on PATH that mimics the CLT shim (emits the notice, returns nothing, exit
1). It asserts the seed writes `hasCompletedClaudeInChromeOnboarding: true`.

Drift is caught by `_resolver_and_guard_snippet()` and the verbatim heredoc
extraction — if start-cli.sh's resolver, guard, or seed block changes shape,
those assertions fail. If the guard regresses to bare `python3`, the stub wins on
PATH under the reproduced harness and the flag is never written — the test fails.
(A full end-to-end launch is avoided so the run stays hermetic and fast — it
would spawn tmux/claude and need a real bundled runtime tree.)

Run: python3 tests/start-cli-chrome-seed-bundled-python.test.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "src" / "agent" / "claude" / "cli" / "start-cli.sh"

# The CLT-shim stub: what bare `python3` does on a genuinely fresh Mac.
CLT_STUB = (
    "#!/bin/sh\n"
    'echo "xcode-select: note: No developer tools were found, requesting install." >&2\n'
    "exit 1\n"
)


def _resolver_and_guard_snippet() -> str:
    """Sanity-check the source still resolves $PY and guards the seed with it
    (fails loudly if the fix is reverted to bare python3)."""
    txt = SCRIPT.read_text()

    # The $PY cascade moved into scripts/python-binary.sh (#2599) so three
    # callers stop duplicating it. Asserting the LITERAL here made this test
    # location-tied: it failed on a refactor that preserved the behaviour
    # exactly. Assert the BEHAVIOUR instead, by running the resolver — that
    # survives the logic moving again, and is what "honors SUTANDO_PY" means.
    resolver = REPO / "scripts" / "python-binary.sh"
    assert resolver.is_file(), "scripts/python-binary.sh missing — start-cli sources it"
    with tempfile.TemporaryDirectory() as _d:
        _explicit = Path(_d) / "explicit-python3"
        _explicit.write_text("#!/bin/sh\nexit 0\n")
        _explicit.chmod(0o755)
        _got = subprocess.run(
            ["bash", "-c", f'. "{resolver}"; resolve_python "{REPO}"'],
            capture_output=True, text=True,
            env={**os.environ, "SUTANDO_PY": str(_explicit), "PATH": "/usr/bin:/bin"},
        ).stdout.strip()
    assert _got == str(_explicit), \
        f"the resolver no longer honors $SUTANDO_PY for $PY (got {_got!r})"
    assert 'runtime/python/bin/python3' in resolver.read_text(), \
        "the resolver lost the bundled-python tier"

    # This one stays source-tied on purpose: it is about start-cli.sh's OWN
    # invocation, not the cascade.
    assert '"$PY" - <<' in txt or "\"$PY\" -" in txt, \
        "the Chrome seed no longer invokes the resolved \"$PY\""
    # And that no bare `python3 ` invocation remains in the seed/daemon launches
    # (comments are fine).
    code_lines = [ln for ln in txt.splitlines() if not ln.lstrip().startswith("#")]
    bad = [ln for ln in code_lines if re.search(r'(?<![\"\w.])python3\s', ln)
           and "bash/python3/node" not in ln]
    assert not bad, f"bare `python3` still invoked in start-cli.sh: {bad}"
    return txt


def _seed_program() -> str:
    """Extract the embedded seed program verbatim (source-tied)."""
    txt = SCRIPT.read_text()
    m = re.search(r"<<'PY'.*?\n(.*?)\nPY\b", txt, re.S)
    assert m, "seed PY heredoc not found — did the seed block move?"
    prog = m.group(1)
    assert "hasCompletedClaudeInChromeOnboarding" in prog, \
        "extracted seed program no longer references hasCompletedClaudeInChromeOnboarding"
    return prog


def _resolve_and_run_seed(prog: str, ccd: Path, sutando_py: str, path_with_stub: str,
                          home: Path) -> dict | None:
    """Reproduce start-cli.sh's resolver + guard in a tiny shell harness, then run
    the ACTUAL extracted seed program with the resolved interpreter — under a PATH
    whose `python3` is the CLT stub. Returns the seeded .claude.json or None."""
    prog_file = ccd / "_seed.py"
    prog_file.write_text(prog)
    harness = f"""
set -eu
REPO='{REPO}'
if [ -n "${{SUTANDO_PY:-}}" ] && [ -x "${{SUTANDO_PY}}" ]; then
  PY="$SUTANDO_PY"
elif [ -x "$REPO/../runtime/python/bin/python3" ]; then
  PY="$REPO/../runtime/python/bin/python3"
else
  PY="python3"
fi
if "$PY" -c 'import sys' > /dev/null 2>&1; then
  _ccd="{ccd}" _cwd="" _accept_bypass="" "$PY" '{prog_file}' || echo "seed-skipped"
else
  echo "guard-skipped"
fi
"""
    env = dict(os.environ)
    env["SUTANDO_PY"] = sutando_py
    env["PATH"] = path_with_stub
    env["HOME"] = str(home)
    r = subprocess.run(["bash", "-c", harness], env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"harness errored: {r.stderr}"
    assert "guard-skipped" not in r.stdout, \
        "the seed was skipped — $PY did not resolve to a working interpreter"
    assert "seed-skipped" not in r.stdout, f"the seed program errored: {r.stdout} {r.stderr}"
    target = ccd / ".claude.json"
    return json.loads(target.read_text()) if target.exists() else None


class TestChromeSeedBundledPython(unittest.TestCase):
    def setUp(self):
        _resolver_and_guard_snippet()
        self.prog = _seed_program()

    def test_seed_writes_flag_when_system_python3_is_a_stub(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # PATH `python3` = the fresh-Mac CLT stub.
            stubdir = td / "stubbin"
            stubdir.mkdir()
            (stubdir / "python3").write_text(CLT_STUB)
            os.chmod(stubdir / "python3", 0o755)
            path_with_stub = f"{stubdir}:/usr/bin:/bin"

            # SUTANDO_PY = a genuinely working interpreter (this test's own).
            sutando_py = sys.executable

            ccd = td / "cfg"
            ccd.mkdir()
            cfg = _resolve_and_run_seed(self.prog, ccd, sutando_py, path_with_stub, td)
            assert cfg is not None, ".claude.json should be created by the seed"
            self.assertIs(
                cfg.get("hasCompletedClaudeInChromeOnboarding"), True,
                "the Chrome-onboarding flag must be seeded even when bare python3 is a stub",
            )
            self.assertIs(cfg.get("hasCompletedOnboarding"), True)

    def test_reverting_to_bare_python3_would_fail(self):
        """Control: with the CLT stub on PATH and NO working SUTANDO_PY / bundled
        fallback, the guard must skip (proving the stub really is unusable — i.e.
        the test's stub faithfully reproduces the failure mode)."""
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            stubdir = td / "stubbin"
            stubdir.mkdir()
            (stubdir / "python3").write_text(CLT_STUB)
            os.chmod(stubdir / "python3", 0o755)
            ccd = td / "cfg"
            ccd.mkdir()
            prog_file = ccd / "_seed.py"
            prog_file.write_text(self.prog)
            # Force PY="python3" (the stub) by unsetting SUTANDO_PY and pointing
            # the fallback at a nonexistent runtime.
            harness = f"""
set -eu
PY="python3"
if "$PY" -c 'import sys' > /dev/null 2>&1; then
  _ccd="{ccd}" _cwd="" _accept_bypass="" "$PY" '{prog_file}'
else
  echo "guard-skipped"
fi
"""
            env = dict(os.environ)
            env["PATH"] = f"{stubdir}:/usr/bin:/bin"
            env.pop("SUTANDO_PY", None)
            r = subprocess.run(["bash", "-c", harness], env=env,
                               capture_output=True, text=True, timeout=30)
            self.assertIn("guard-skipped", r.stdout,
                          "the CLT stub must be unusable so the guard skips — "
                          "otherwise the primary test proves nothing")
            self.assertFalse((ccd / ".claude.json").exists(),
                             "no flag should be written when only the stub is available")


if __name__ == "__main__":
    unittest.main()
