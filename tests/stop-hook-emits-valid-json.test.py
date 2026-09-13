#!/usr/bin/env python3
"""The Stop hook's block decision must be parseable JSON.

`src/check-pending-tasks.sh` built its response by hand, and the `\n` in the
printf FORMAT string was emitted as a raw newline inside a JSON string value.
Raw control characters are illegal there, so the client could not parse the
response and printed `stop hook error` instead. The guard exists to stop the
agent going idle with unanswered tasks; because every block decision was
unparseable, it had never once fired.

The empty-queue path returns `{}` and always parsed, which is why nothing
caught this: the failure appears ONLY when the hook has something to say. So
this asserts against a NON-EMPTY queue, and pins that the block actually fires
(a fixture that produced `{}` would pass while proving nothing).

Also pins body fidelity. The old escaping ran `sed 's/"/\\"/g' | tr '\n' ' '`,
which left backslashes unescaped — a task body containing one would break the
JSON again by a different route.

This asserts the SEMANTICS (the response parses). `check-pending-tasks-workspace.test.sh`
asserts the WIRE SHAPE, matching the literal `"decision":"block"` — so the encoder must keep
compact separators and `ensure_ascii=False`. Both properties are required; neither implies
the other, and a first pass at this fix passed here while breaking that sibling suite.

Run: python3 tests/stop-hook-emits-valid-json.test.py
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

HOOK = pathlib.Path(__file__).resolve().parent.parent / "src" / "check-pending-tasks.sh"
RESOLVE = 'WORKSPACE="$(bash "$REPO_DIR/scripts/sutando-config.sh" workspace 2>/dev/null)"'
REPO_LINE = 'REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"'
REPO = HOOK.resolve().parent.parent


def _stub(ws: pathlib.Path) -> pathlib.Path:
    """The hook, pinned to this repo and the given workspace.

    REPO_DIR must be pinned too: run from a temp dir, `dirname $0/..` points
    outside the repo, so `sutando-config.sh` is never found and the interpreter
    cascade silently falls back to PATH — the test would then measure the
    fallback rather than the contract.
    """
    src = HOOK.read_text()
    assert RESOLVE in src and REPO_LINE in src, "hook layout moved; update this test"
    src = src.replace(REPO_LINE, f'REPO_DIR="{REPO}"').replace(RESOLVE, f'WORKSPACE="{ws}"')
    stub = ws / "hook.sh"
    stub.write_text(src)
    return stub

BODY = 'a body with "quotes", a backslash \\ and\na second line\n'


def _run(workspace: pathlib.Path) -> str:
    """Run the real hook against `workspace`, pinning its resolver to it."""
    stub = _stub(workspace)
    out = subprocess.run(
        ["bash", str(stub)], capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    assert out.returncode == 0, f"hook exited {out.returncode}: {out.stderr}"
    return out.stdout


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp)
        (ws / "tasks").mkdir()
        (ws / "results").mkdir()

        # Empty queue: the path that always parsed, kept so a regression here is visible too.
        assert json.loads(_run(ws)) == {}, "empty queue must emit {}"

        (ws / "tasks" / "task-1.txt").write_text(f"id: task-1\ntask: {BODY}")
        raw = _run(ws)

        decision = json.loads(raw)  # the assertion: unparseable output fails here
        assert decision["decision"] == "block", (
            f"fixture did not make the guard fire: {decision!r}"
        )

        ctx = decision["additionalContext"]
        for label, needle in (("quotes", '"quotes"'), ("backslash", "\\"), ("newline", "\n")):
            assert needle in ctx, f"task body lost its {label}"

    _test_broken_path_still_blocks()
    _test_result_readiness_matches_the_delivery_owner()
    print("stop-hook-emits-valid-json: PASS")


def _test_broken_path_still_blocks() -> None:
    """A configured interpreter must win over a broken `python3` on PATH.

    `scripts/python-binary.sh` resolves $SUTANDO_PY, then the bundled runtime,
    then PATH — so an install with a configured Python must not be defeated by
    whatever `python3` happens to resolve to. A bare `python3` in the hook
    ignored that cascade and emitted nothing for a nonempty queue.

    PATH is shadowed, not emptied: emptying removes `cat` and the config helper,
    so the hook would fail for reasons unrelated to the contract under test.
    """
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp)
        (ws / "tasks").mkdir()
        (ws / "results").mkdir()
        (ws / "tasks" / "task-1.txt").write_text("id: task-1\ntask: answer me\n")

        stub = _stub(ws)

        shim = ws / "bin"
        shim.mkdir()
        broken = shim / "python3"
        broken.write_text("#!/bin/sh\necho 'wrong interpreter' >&2\nexit 127\n")
        broken.chmod(0o755)

        env = dict(
            os.environ,
            PATH=f"{shim}:{os.environ.get('PATH', '')}",
            SUTANDO_PY=sys.executable,
        )
        out = subprocess.run(["/bin/bash", str(stub)], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, env=env)
        assert out.returncode == 0, f"hook exited {out.returncode}: {out.stderr}"
        assert json.loads(out.stdout)["decision"] == "block", (
            f"a broken python3 on PATH defeated the configured interpreter: {out.stdout!r}"
        )


def _test_result_readiness_matches_the_delivery_owner() -> None:
    """Readiness is owned by src/delivery/readiness.py, the policy every delivery
    consumer uses. A local `tr -d '[:space:]'` diverges from it under LC_ALL=C:
    NBSP/EM SPACE and undecodable bytes read as content, so Stop succeeds on a
    result no consumer will ever send.

    LC_ALL=C is set deliberately. Under a UTF-8 locale BSD tr yields empty for
    these inputs and coincides with the helper, so the cases cannot tell the two
    apart — an earlier version of this test inherited UTF-8 and passed against
    the very implementation it was written to reject.

    `[no-send]` must stay ready: deliberate protocol, not an absent reply.
    """
    env = dict(os.environ, LC_ALL="C", LANG="C")
    cases = [
        (b"", True, "empty"),
        (b"  \n\t\n", True, "ascii whitespace"),
        ("\u00a0\u2003\n".encode(), True, "NBSP + EM SPACE"),
        (b"\xff\xfe\n", True, "undecodable bytes"),
        (b"[no-send]\n", False, "no-send protocol"),
        (b"a real answer\n", False, "real reply"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        ws = pathlib.Path(tmp)
        (ws / "tasks").mkdir()
        (ws / "results").mkdir()
        (ws / "tasks" / "task-1.txt").write_text("id: task-1\ntask: answer me\n")
        stub = _stub(ws)
        for body, should_block, label in cases:
            (ws / "results" / "task-1.txt").write_bytes(body)
            out = subprocess.run(["/bin/bash", str(stub)], capture_output=True,
                                 text=True, stdin=subprocess.DEVNULL, env=env)
            assert out.returncode == 0, f"{label}: hook exited {out.returncode}"
            blocked = json.loads(out.stdout or "{}") != {}
            assert blocked is should_block, (
                f"{label}: blocked={blocked}, expected {should_block}"
            )


if __name__ == "__main__":
    main()
