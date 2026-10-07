"""A clean subprocess-env builder for tests that launch the real
src/watch-tasks-stream.sh.

A live pool worker's own shell exports real SUTANDO_* operational state
(SUTANDO_INBOX_RESOLVER, SUTANDO_WORKSPACE_DIR, SUTANDO_TASKS_DIR, ...) that
`dict(os.environ)` inherits wholesale into the watcher subprocess under test.
A test that only pops one or two names still leaks the rest, producing
false pass/fail that depends on which vars happen to be set in the invoking
shell rather than on the code under test. See issue #4649.
"""
import os

_DROP_PREFIXES = ("SUTANDO_", "TMUX", "GIT_")
_DROP_EXACT = {"AGENT_ID", "AG2_AGENT_NAME", "CLAUDECODE"}


def clean_env():
    """A copy of the current environment with every leak-prone name dropped.
    Callers layer their own SUTANDO_* overrides on top of this dict, never
    on top of dict(os.environ)."""
    return {k: v for k, v in os.environ.items()
            if not any(k.startswith(p) for p in _DROP_PREFIXES) and k not in _DROP_EXACT}
