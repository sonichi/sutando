"""A `python -c` child traced by the coverage runner, like the test that starts it.

    argv = [sys.executable, "-c", covered_code(code)]

Under SUTANDO_TEST_SUBPROCESS_COVERAGE=1 the code is prefixed with a coverage start that uses the
repository's .coveragerc, resolves its relative source from the repository root and writes its
fragment beside the parent's, whatever the child's cwd or environment; otherwise the code is
returned unchanged. The prefix depends on nothing in the child's environment, so a child started
under a clean environment (a tmux pane, `env -i`) is measured too. Product code never knows.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
# Nothing on the child's stderr is coverage's own: tests assert on that stream.
QUIET = ["no-data-collected", "module-not-measured", "module-not-imported", "already-imported",
         "include-ignored", "no-sysmon", "no-ctracer", "couldnt-parse"]


def covered_code(code: str) -> str:
    if os.environ.get("SUTANDO_TEST_SUBPROCESS_COVERAGE") != "1":
        return code
    try:
        import coverage
    except ImportError:
        return code
    from _helpers.hermetic_env import coverage_bootstrap
    boot = coverage_bootstrap(os.path.dirname(os.path.abspath(coverage.__file__)))
    data = os.path.abspath(os.environ.get("COVERAGE_FILE", ".coverage"))
    q = json.dumps
    return ("import os as _o, sys as _s, atexit as _a; _s.path.insert(0, %s); import coverage as _cv; "
            "_w = _o.getcwd(); _o.chdir(%s); _c = _cv.Coverage(config_file=%s, data_file=%s); "
            "_c.set_option(\"run:disable_warnings\", %s); _c.start(); "
            "_o.chdir(_w); _s.path.remove(%s); _a.register(lambda: (_c.stop(), _c.save()))\n"
            % (q(boot), q(str(REPO)), q(str(REPO / ".coveragerc")), q(data), q(QUIET), q(boot))) + code
