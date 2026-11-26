"""claude-local's /exit reaches a pane a viewer left in copy mode.

2026-11-26, review of ag2-space/ag2space-cinny-desktop#1317: the app's terminal tab now takes typing,
and a scroll there leaves the agent's tmux pane in copy mode, where /exit was swallowed and the stop
came back uncertain. The real-tmux case lives with the adapter's launcher tests; this runs it from the
repo suite so the changed launcher lines are measured.

Run:
    python3 tests/claude-local-exit-copy-mode.test.py
"""
import importlib.util
import sys
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "packages" / "ag2-sparrow"
sys.path.insert(0, str(PKG))
spec = importlib.util.spec_from_file_location("claude_code_adapter_tests", PKG / "tests" / "test_claude_code_adapter.py")
adapter_tests = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter_tests
spec.loader.exec_module(adapter_tests)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([adapter_tests.LauncherTmux(name) for name in (
        "test_exit_reaches_a_pane_a_viewer_left_in_copy_mode",
        # The plain stop: /exit, sent with copy-mode -q in front, still reaches the pane.
        "test_stop_sends_exit_then_kills_the_session_through_the_fence_and_frees_the_root",
    )])


if __name__ == "__main__":
    unittest.main()
