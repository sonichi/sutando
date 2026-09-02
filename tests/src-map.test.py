#!/usr/bin/env python3
"""Regression checks for scripts/gen-src-map.py."""

import contextlib
import importlib.util
import io
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_src_map", REPO / "scripts" / "gen-src-map.py")
gen_src_map = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen_src_map)

failures = []
checks_run = 0


def check(name: str, condition: bool) -> None:
    global checks_run
    checks_run += 1
    print(("  ok  " if condition else "  FAIL ") + name)
    if not condition:
        failures.append(name)


rows = dict(gen_src_map.collect())
check(
    "Swift modules are indexed",
    {
        "src/Sutando/main.swift",
        "src/Sutando/SutandoConfig.swift",
        "src/scroll-wheel.swift",
    }.issubset(rows),
)
check(
    "TypeScript reference directive is skipped",
    rows["src/web-voice-transport.ts"].startswith("web-voice-transport —"),
)
check(
    "wrapped purpose is joined",
    rows["src/inject-framing.ts"].endswith("MatrixRTC conversation daemon)."),
)
check(
    "block-comment closer is stripped",
    rows["src/observability/claude/_map-util.ts"]
    == "Shared helpers for the Claude Code mappers.",
)
check(
    "Swift MARK header becomes a purpose",
    rows["src/Sutando/main.swift"] == "Sutando Drop Menu Bar App",
)

malformed = [
    (path, purpose)
    for path, purpose in rows.items()
    if "<reference" in purpose
    or purpose.endswith("*/")
    or re.search(r"(?:\b(?:the|and|a|an|to|from|into|of|for|with)|[,(:;—-])$", purpose)
    or purpose.count("(") != purpose.count(")")
    or purpose.count('"') % 2
    or purpose.count("`") % 2
]
check("generated purposes contain no obvious fragments", not malformed)

sync = subprocess.run(
    ["python3", str(REPO / "scripts" / "gen-src-map.py"), "--check"],
    cwd=REPO,
    capture_output=True,
    text=True,
)
check("docs/src-map.md matches generated output", sync.returncode == 0)

# --- render() and main(), driven in-process ---------------------------------
# The checks above exercise collect()/purpose() over the real tree. render() and
# main() were untested, leaving the generator well under the repo's 95% diff-
# coverage gate. Drive both directly.

_rendered = gen_src_map.render([("src/a.ts", "Alpha module."), ("src/sub/b.py", "")])
check("render: documented row emitted", "- **`a.ts`** — Alpha module." in _rendered)
check("render: undocumented row flagged", "_(no header comment)_" in _rendered)
check("render: undocumented count surfaced", "1 without a usable header comment." in _rendered)
check("render: grouped by directory", "## `src/sub/`" in _rendered)

# main() writes to module-global OUT and prints OUT.relative_to(REPO), so the
# redirected path must live UNDER the repo (as it always does in production).
# Use a temp dir inside REPO, restore argv/OUT and remove it no matter what.
_orig_out, _orig_argv = gen_src_map.OUT, sys.argv
_tmpdir = Path(tempfile.mkdtemp(dir=str(REPO)))
try:
    _tmp = _tmpdir / "src-map.md"
    gen_src_map.OUT = _tmp

    sys.argv = ["gen-src-map.py"]
    check("main: write mode returns 0", gen_src_map.main() == 0)
    check("main: write mode created the file", _tmp.exists())

    sys.argv = ["gen-src-map.py", "--check"]
    check("main: --check passes when in sync", gen_src_map.main() == 0)

    _tmp.write_text("stale\n", encoding="utf-8")
    check("main: --check fails when stale", gen_src_map.main() == 1)

    _tmp.unlink()
    check("main: --check fails when file missing", gen_src_map.main() == 1)

    # The tally is the only place the module counts survive, so the branch that
    # names undocumented modules has to be exercised where it is now printed.
    sys.argv = ["gen-src-map.py"]
    _orig_collect = gen_src_map.collect
    try:
        gen_src_map.collect = lambda: [("src/a.ts", "Alpha module."), ("src/b.py", "")]
        _out = io.StringIO()
        with contextlib.redirect_stdout(_out):
            gen_src_map.main()
        check("main: tally names the undocumented count",
              "1 without a usable header comment" in _out.getvalue())

        gen_src_map.collect = lambda: [("src/a.ts", "Alpha module.")]
        _out = io.StringIO()
        with contextlib.redirect_stdout(_out):
            gen_src_map.main()
        check("main: tally omits the clause when all are documented",
              "without a usable header comment" not in _out.getvalue())
    finally:
        gen_src_map.collect = _orig_collect
finally:
    gen_src_map.OUT, sys.argv = _orig_out, _orig_argv
    shutil.rmtree(_tmpdir, ignore_errors=True)

# --- purpose() edge branches ------------------------------------------------
with tempfile.TemporaryDirectory() as _td:
    _d = Path(_td)

    def _mk(name: str, body: str) -> Path:
        p = _d / name
        p.write_text(body, encoding="utf-8")
        return p

    check("purpose: unreadable path yields empty", gen_src_map.purpose(_d) == "")
    check("purpose: code-first file has no purpose",
          gen_src_map.purpose(_mk("code.ts", "export const x = 1;\n")) == "")
    check("purpose: closed /* */ block",
          gen_src_map.purpose(_mk("blk.ts", "/* Block-comment purpose. */\nconst y = 1;\n"))
          .startswith("Block-comment purpose"))
    check("purpose: single-line triple-quote docstring",
          gen_src_map.purpose(_mk("one.py", "'''One-line docstring purpose.'''\nx = 1\n"))
          .startswith("One-line docstring purpose"))
    check("purpose: Usage block returns the label line",
          gen_src_map.purpose(_mk("us.sh", "# Do the thing\n# Usage: foo bar\nx=1\n")) == "Do the thing")
    check("purpose: swift import skipped before MARK header",
          gen_src_map.purpose(_mk("s.swift", "import Foo\n\n// MARK: - Widget View\nlet z = 1\n")) == "Widget View")
    check("purpose: empty comment block yields empty",
          gen_src_map.purpose(_mk("e.ts", "//\n//\nconst q = 1;\n")) == "")
    check("purpose: boilerplate-only file yields empty",
          gen_src_map.purpose(_mk("o.py", "#!/usr/bin/env python3\n\n\n")) == "")
    check("purpose: unterminated /* still yields text",
          gen_src_map.purpose(_mk("u.ts", "/* start no close\n" + "x\n" * 45)) != "")

if failures:
    for path, purpose in malformed:
        print(f"  malformed {path}: {purpose}")
    if sync.returncode != 0:
        print(sync.stderr)
    print(f"Results: {len(failures)} failed, {checks_run - len(failures)} passed")
    raise SystemExit(1)

print(f"Results: {checks_run} passed")
