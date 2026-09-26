"""Smoke test for skills/claude-gemini/scripts/gemini-run.sh.

Mocks the agy/gemini binaries and verifies:
  1. `agy` on PATH is used directly, with --approval-mode translated to agy's
     --mode / --dangerously-skip-permissions flags.
  2. `agy` found only via the ~/.local/bin fallback (not on PATH, matching the
     cron/bridge shells named in skills/claude-router/SKILL.md) is still resolved
     and invoked -- the fix for keweichen's PATH-resolution blocker on PR #4267.
  3. A gemini-only environment (no agy anywhere) still succeeds via the legacy
     gemini backend, using the parent commit's original argv mapping
     (--approval-mode passed straight through, --include-directories instead of
     --add-dir) -- the fix for the backward-compat blocker on PR #4267. This is a
     regression pin: it must fail against the pre-fix PR #4267 head, which hard-fails
     any environment lacking agy.
  4. An unknown --approval-mode is rejected regardless of backend.
  5. Prompt is required unless --check is used.

Run: python3 tests/claude-gemini-wrapper.test.py
"""
import os
import subprocess
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "claude-gemini" / "scripts" / "gemini-run.sh"

MOCK_BODY = "#!/bin/bash\nprintf '%s\\n' \"$@\" >\"$MOCK_OUT\"\n"


def make_mock(bin_dir: Path, name: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    mock = bin_dir / name
    mock.write_text(MOCK_BODY)
    mock.chmod(0o755)
    return mock


def run(home: Path, path_dirs: list[Path], *args: str, mock_out: Path) -> tuple[int, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PATH"] = ":".join([str(d) for d in path_dirs] + ["/usr/bin", "/bin"])
    env["MOCK_OUT"] = str(mock_out)
    env.pop("GEMINI_API_KEY", None)
    env.pop("GOOGLE_API_KEY", None)
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_agy_on_path(tmp: Path) -> None:
    home = tmp / "home_agy_on_path"
    bin_dir = tmp / "path_bin_1"
    make_mock(bin_dir, "agy")
    mock_out = tmp / "agy_on_path.argv"
    rc, out = run(home, [bin_dir], "--approval-mode", "auto_edit", "--", "hi", mock_out=mock_out)
    assert rc == 0, f"expected success with agy on PATH: {out}"
    argv = mock_out.read_text().splitlines()
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "accept-edits", (
        f"auto_edit did not map to --mode accept-edits: {argv}"
    )
    assert "--prompt" in argv and "hi" in argv, f"prompt missing: {argv}"


def test_agy_local_bin_fallback(tmp: Path) -> None:
    # agy is NOT on PATH -- only reachable via $HOME/.local/bin, the cron/bridge
    # shape keweichen's review reproduced (skills/claude-router/SKILL.md:36).
    home = tmp / "home_agy_fallback"
    make_mock(home / ".local" / "bin", "agy")
    mock_out = tmp / "agy_fallback.argv"
    rc, out = run(home, [], "--", "hi", mock_out=mock_out)
    assert rc == 0, f"expected agy fallback resolution via ~/.local/bin to succeed: {out}"
    argv = mock_out.read_text().splitlines()
    assert "--prompt" in argv and "hi" in argv, f"prompt missing via fallback: {argv}"
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "plan", (
        f"default plan mode not applied via fallback: {argv}"
    )


def test_gemini_only_backward_compat(tmp: Path) -> None:
    # No agy anywhere (neither PATH nor ~/.local/bin) -- only the legacy gemini
    # CLI is installed. Must still succeed via the preserved fallback.
    home = tmp / "home_gemini_only"
    bin_dir = tmp / "path_bin_2"
    make_mock(bin_dir, "gemini")
    mock_out = tmp / "gemini_only.argv"
    rc, out = run(
        home, [bin_dir],
        "--approval-mode", "yolo", "--include-directory", "/tmp/x", "--", "hi",
        mock_out=mock_out,
    )
    assert rc == 0, f"gemini-only environment must still succeed (backward compat): {out}"
    argv = mock_out.read_text().splitlines()
    assert "--approval-mode" in argv and argv[argv.index("--approval-mode") + 1] == "yolo", (
        f"legacy gemini backend must pass --approval-mode through unmapped: {argv}"
    )
    assert "--include-directories" in argv, f"legacy gemini backend must use --include-directories: {argv}"
    assert "--add-dir" not in argv, f"legacy gemini backend must not use agy's --add-dir: {argv}"
    assert "--dangerously-skip-permissions" not in argv, (
        f"legacy gemini backend must not emit agy-only flags: {argv}"
    )


def test_neither_backend_fails(tmp: Path) -> None:
    home = tmp / "home_neither"
    mock_out = tmp / "neither.argv"
    rc, out = run(home, [], "--", "hi", mock_out=mock_out)
    assert rc != 0, "expected failure when neither agy nor gemini is available"
    assert "not found" in out, f"missing not-found guard message: {out}"


def test_unknown_approval_mode_rejected(tmp: Path) -> None:
    home = tmp / "home_bad_mode"
    bin_dir = tmp / "path_bin_3"
    make_mock(bin_dir, "agy")
    mock_out = tmp / "bad_mode.argv"
    rc, out = run(home, [bin_dir], "--approval-mode", "bogus", "--", "hi", mock_out=mock_out)
    assert rc != 0, "expected failure for unknown --approval-mode"
    assert "unknown --approval-mode" in out, f"missing guard message: {out}"


def test_prompt_required(tmp: Path) -> None:
    home = tmp / "home_no_prompt"
    bin_dir = tmp / "path_bin_4"
    make_mock(bin_dir, "agy")
    mock_out = tmp / "no_prompt.argv"
    rc, out = run(home, [bin_dir], mock_out=mock_out)
    assert rc != 0, "expected non-zero exit without prompt"
    assert "prompt required" in out, f"missing guard message: {out}"


def main() -> None:
    assert SCRIPT.exists(), f"missing: {SCRIPT}"
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for fn in (
            test_agy_on_path,
            test_agy_local_bin_fallback,
            test_gemini_only_backward_compat,
            test_neither_backend_fails,
            test_unknown_approval_mode_rejected,
            test_prompt_required,
        ):
            fn(tmp)
            print(f"PASS {fn.__name__}")
    print("OK")


if __name__ == "__main__":
    main()
