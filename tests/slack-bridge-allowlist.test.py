#!/usr/bin/env python3
"""
Behavioral test for src/slack-bridge.py's _is_path_sendable() — the
allowlist gate the bridge uses before uploading a file to Slack via
files_upload_v2.

Same security contract as the discord-bridge / telegram-bridge versions:
    1. File must exist.
    2. Real path (os.path.realpath) must equal-or-start-with an entry in
       SEND_ALLOWED_ROOTS, OR start with one of SEND_ALLOWED_PREFIXES.
    3. Fail-closed default — anything else returns False.

Why behavioral, not just structural: the structural test
(slack-bridge-access.test.py) guards "the function exists with the right
shape", but a future refactor could keep the regex-visible shape while
breaking the fail-closed default (e.g. changing `for root in
SEND_ALLOWED_ROOTS: if real.startswith(root)` into something that returns
True before the prefix check completes). Behavioral coverage shows the
function actually rejects unauthorized paths.

The bridge imports `slack_bolt.App` at module load and the real App
constructor hits `auth.test` against Slack with the token — which fails
on a fake token. This test monkey-patches `slack_bolt.App` with a stub
before importing, so we can exercise the bridge's pure-Python helpers
without network access. If `slack_bolt` is not installed, the test
skips silently (the structural test in slack-bridge-access.test.py is
still useful in that environment).

Run: python3 tests/slack-bridge-allowlist.test.py
Exit code: 0 on pass / skip, 1 on fail.
"""

import os
import sys
import tempfile
import types
from pathlib import Path


class _StubApp:
    """Stub for slack_bolt.App — accepts any constructor kwargs, provides
    .event() decorator, .client placeholder. Skips the auth.test that
    the real App fires on init."""

    def __init__(self, *a, **kw):
        self.client = types.SimpleNamespace()

    def event(self, _name):
        def decorator(fn):
            return fn
        return decorator


def _load_module():
    """Import slack-bridge.py with stubbed slack_bolt + env vars, so we
    can exercise pure-Python helpers without network access. Returns the
    module or None if even the stub setup fails."""
    os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token-for-helper-only")
    os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test-token-for-helper-only")
    os.environ.setdefault("SUTANDO_WORKSPACE", tempfile.mkdtemp(prefix="sutando-test-slack-allowlist-"))

    # Monkey-patch slack_bolt BEFORE importing the bridge. If slack_bolt
    # isn't installed at all, fabricate the whole module tree so the
    # bridge's `from slack_bolt import App` succeeds.
    try:
        import slack_bolt as _real_bolt
        # Real lib installed — only patch App.
        _real_bolt.App = _StubApp
    except ImportError:
        # Not installed — fabricate the bare minimum so the bridge imports.
        stub_bolt = types.ModuleType("slack_bolt")
        stub_bolt.App = _StubApp
        sys.modules["slack_bolt"] = stub_bolt
        adapter_pkg = types.ModuleType("slack_bolt.adapter")
        sys.modules["slack_bolt.adapter"] = adapter_pkg
        sm_mod = types.ModuleType("slack_bolt.adapter.socket_mode")
        sm_mod.SocketModeHandler = object
        sys.modules["slack_bolt.adapter.socket_mode"] = sm_mod

    # Also stub the submodule that the bridge imports directly.
    if "slack_bolt.adapter.socket_mode" not in sys.modules:
        adapter_pkg = types.ModuleType("slack_bolt.adapter")
        sys.modules["slack_bolt.adapter"] = adapter_pkg
        sm_mod = types.ModuleType("slack_bolt.adapter.socket_mode")
        sm_mod.SocketModeHandler = object
        sys.modules["slack_bolt.adapter.socket_mode"] = sm_mod

    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    bridge_path = repo / "src" / "slack-bridge.py"
    if not bridge_path.exists():
        print(f"FAIL: {bridge_path} not found", file=sys.stderr)
        sys.exit(1)
    spec = importlib.util.spec_from_file_location("slack_bridge_under_test", bridge_path)
    sys.path.insert(0, str(repo / "src"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_cases(is_path_sendable, workspace) -> int:
    """Evaluate every allowlist case, and ALWAYS remove what this run created.

    Split out of `main()` so the FAILURE path is testable — a caller can pass a
    predicate that raises and then assert the carrier paths are clean. That
    regression is `_failure_path_leaves_nothing_behind()` below.

    Cleanup is in a `finally`, not at the end of the happy path. The first cut
    unlinked only after all case evaluation, so any exception — in
    `_is_path_sendable`, in a case, or in the symlink section — exited before
    cleanup and left fixtures under `notes/`, a vault-carrier path. That is the
    very pollution class this file exists to close, and it failed exactly when it
    mattered most: on an aborted run. john-the-dev and qingyun-wu each reproduced
    it by fault injection on head 262aeb9e (#2614); both were right.

    `created` is appended immediately after each successful create, so a partial
    setup (first fixture made, second raising) is cleaned up too.
    """
    created: "list[Path]" = []
    try:
        return _cases_body(is_path_sendable, workspace, created)
    finally:
        # Reversed so a symlink goes before anything it was derived from. Each
        # unlink is independently guarded: one failure must not strand the rest.
        for leftover in reversed(created):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:  # pragma: no cover — surfaced, never silent
                print(f"  WARN: could not remove fixture {leftover}: {exc}", file=sys.stderr)


def _cases_body(is_path_sendable, workspace, created: "list[Path]") -> int:
    # Fixtures go in the LIVE workspace, because that is where the allowlist
    # roots are: `_is_path_sendable` resolves against `resolve_workspace()`, so a
    # file under a tmpdir is not sendable and the "allowed" cases cannot be tested
    # at all. Given that, the fixture NAMES must be unique — a fixed `ok.md` both
    # collides with any real owner file of that name and makes cleanup a deletion
    # hazard (john-the-dev, #2614: pre-seeding `notes/ok.md` with owner bytes and
    # running main() destroyed it). `mkstemp` gives a name nothing else can hold,
    # so there is no owner state to overwrite and none to restore.
    notes_dir = workspace / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = workspace / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    results_dir = workspace / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir = workspace / "slack-inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    # Register with `created` in the same breath as the create. Anything between
    # the two is a window where an exception strands the file.
    _fd, _p = tempfile.mkstemp(prefix="allowlist-test-", suffix=".md", dir=str(notes_dir))
    os.close(_fd)
    allowed_file = Path(_p)
    created.append(allowed_file)
    allowed_file.write_text("ok")
    _fd, _p = tempfile.mkstemp(prefix="allowlist-test-", suffix=".png", dir=str(inbox_dir))
    os.close(_fd)
    inbox_file = Path(_p)
    created.append(inbox_file)
    inbox_file.write_text("binary")

    # Regression for the deletion hazard: whatever lives at the two names the
    # old fixtures hard-coded must be byte-identical when this test finishes —
    # including the case where the owner genuinely has a file there. Recorded
    # BEFORE the run, asserted after; nothing is created, so an absent path
    # must stay absent.
    _guarded = {}
    for _name in (notes_dir / "ok.md", inbox_dir / "downloaded.png"):
        _guarded[_name] = _name.read_bytes() if _name.is_file() else None

    # /tmp/sutando-* allowed-prefix fixture. dir="/tmp" forces real /tmp
    # — without it macOS tempfile uses $TMPDIR (/var/folders/...), which
    # is NOT in the allowlist's prefix list.
    tmp_sutando = Path(tempfile.mkdtemp(prefix="sutando-test-allowed-", dir="/tmp"))
    tmp_sutando_file = tmp_sutando / "ok.txt"
    tmp_sutando_file.write_text("ok")

    # Disallowed: a file outside any allowed root or prefix
    not_allowed_dir = Path(tempfile.mkdtemp(prefix="other-not-sutando-"))
    not_allowed_file = not_allowed_dir / "secret.txt"
    not_allowed_file.write_text("secret")

    cases = [
        # (path, expected, label)
        (str(allowed_file), True, "allowed file in $WORKSPACE/notes/"),
        (str(inbox_file), True, "allowed file in $WORKSPACE/slack-inbox/"),
        (str(tmp_sutando_file), True, "allowed file at /tmp/sutando-* prefix"),
        (str(not_allowed_file), False, "disallowed file outside any allowed root"),
        (str(workspace / "notes" / "does-not-exist"), False, "missing file in allowed root"),
        ("/etc/passwd", False, "fail-closed for sensitive system file"),
        ("relative/path.txt", False, "fail-closed for relative path"),
        ("", False, "fail-closed for empty string"),
    ]

    failed = 0
    for path, expected, label in cases:
        actual = is_path_sendable(path)
        if actual != expected:
            print(f"FAIL: {label} → expected {expected}, got {actual} (path={path!r})", file=sys.stderr)
            failed += 1
        else:
            print(f"  OK: {label}")

    # Symlink traversal — the CodeQL-recognized sanitizer is realpath.
    # A symlink in an allowed dir pointing OUT must return False.
    if hasattr(os, "symlink"):
        # Derived from the already-unique mkstemp name rather than a fixed
        # `evil-link`: a hard-coded name in a carrier path is the same collision
        # hazard the fixtures themselves were fixed for, and `symlink_to` on an
        # existing owner path would raise mid-run.
        symlink_path = notes_dir / f"{allowed_file.stem}-evil-link"
        symlink_path.symlink_to(not_allowed_file)
        created.append(symlink_path)
        actual = is_path_sendable(str(symlink_path))
        label = "symlink in allowed root targeting non-allowed file"
        if actual is not False:
            print(f"FAIL: {label} → expected False, got {actual}", file=sys.stderr)
            failed += 1
        else:
            print(f"  OK: {label}")

    # No happy-path unlink block here any more: every fixture is registered in
    # `created` and removed by `_run_cases`'s `finally`, which runs on the
    # exception path too. A cleanup that only fires on a normal return is the
    # defect this PR was opened to fix.

    # Prove we touched nothing the owner had at the hard-coded names.
    for _name, _orig in _guarded.items():
        _now = _name.read_bytes() if _name.is_file() else None
        if _now != _orig:
            print(
                f"FAIL: pre-existing owner file changed: {_name} "
                f"({'absent' if _orig is None else f'{len(_orig)} B'} -> "
                f"{'absent' if _now is None else f'{len(_now)} B'})",
                file=sys.stderr,
            )
            failed += 1
        else:
            print(f"  OK: left {_name.name} untouched "
                  f"({'absent' if _orig is None else 'pre-existing, byte-identical'})")

    return failed


def _failure_path_leaves_nothing_behind(workspace) -> int:
    """THE regression: an aborted run must strand nothing in the carrier paths.

    Requested by john-the-dev and qingyun-wu on #2614 after both fault-injected a
    raising `_is_path_sendable` on head 262aeb9e and got:

        LEAKED_FIXTURES ['notes/allowlist-test-*.md', 'slack-inbox/allowlist-test-*.png']

    The guarded-name byte comparison could not catch this — it only proves the
    two FIXED owner names are unchanged on a normal return, and says nothing
    about unique fixtures on the exception path.

    Deliberately asserted by scanning the directories for the `allowlist-test-`
    prefix rather than by checking the paths `_run_cases` happened to hand back:
    on the failure path it hands nothing back, and a leak we forgot to track is
    exactly the leak worth catching.
    """
    notes_dir, inbox_dir = workspace / "notes", workspace / "slack-inbox"
    before = {p for d in (notes_dir, inbox_dir) if d.is_dir() for p in d.glob("allowlist-test-*")}

    def _raises(_path):
        raise RuntimeError("injected: predicate fails mid-run")

    try:
        _run_cases(_raises, workspace)
    except RuntimeError:
        pass                      # expected — the point is what it left behind
    else:
        print("FAIL: injected fault did not propagate; regression is vacuous", file=sys.stderr)
        return 1

    leaked = sorted(
        p for d in (notes_dir, inbox_dir) if d.is_dir()
        for p in d.glob("allowlist-test-*") if p not in before
    )
    if leaked:
        print(f"FAIL: aborted run left {len(leaked)} fixture(s) in carrier paths:", file=sys.stderr)
        for p in leaked:
            print(f"       {p}", file=sys.stderr)
        return 1
    print("  OK: aborted run left no fixtures in notes/ or slack-inbox/")
    return 0


def main() -> int:
    try:
        mod = _load_module()
    except Exception as e:
        print(f"FAIL: could not load slack-bridge.py for testing: {e}", file=sys.stderr)
        return 1

    workspace = Path(mod.REPO)
    failed = _run_cases(mod._is_path_sendable, workspace)
    failed += _failure_path_leaves_nothing_behind(workspace)

    if failed:
        print(f"\nFAIL: {failed} case(s) failed", file=sys.stderr)
        return 1

    print("\nPASS: _is_path_sendable() enforces the allowlist correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
