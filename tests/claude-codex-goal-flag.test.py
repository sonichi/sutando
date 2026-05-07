"""Smoke test for skills/claude-codex/scripts/codex-run.sh --goal flag.

Mocks the codex binary in PATH and verifies:
  1. --goal is documented in --help.
  2. --goal prepends "/goal " to the prompt and enables --full-auto.
  3. Default mode is unaffected by --goal logic.
  4. --goal --review fails fast (semantically incompatible).
  5. --goal -- "/goal X" does not double-prefix.

Run: python3 tests/claude-codex-goal-flag.test.py
"""
import os
import subprocess
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "claude-codex" / "scripts" / "codex-run.sh"


def make_mock_codex(tmpdir: Path) -> Path:
    bin_dir = tmpdir / "bin"
    bin_dir.mkdir()
    mock = bin_dir / "codex"
    mock.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\" >\"$CODEX_MOCK_OUT\"\n")
    mock.chmod(0o755)
    return bin_dir


def run(env_path_dir: Path, *args: str, mock_out: Path) -> tuple[int, str]:
    env = os.environ.copy()
    env["PATH"] = f"{env_path_dir}:{env['PATH']}"
    env["CODEX_MOCK_OUT"] = str(mock_out)
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_help_includes_goal(bin_dir: Path, mock_out: Path) -> None:
    rc, out = run(bin_dir, "--help", mock_out=mock_out)
    assert rc == 0
    assert "--goal" in out, f"--goal absent from --help: {out}"


def test_goal_prepends_and_full_auto(bin_dir: Path, mock_out: Path) -> None:
    rc, _ = run(bin_dir, "--goal", "--", "Build something cool", mock_out=mock_out)
    assert rc == 0
    argv = mock_out.read_text().splitlines()
    assert "exec" in argv, f"expected exec subcommand, got: {argv}"
    assert "--full-auto" in argv, f"--goal did not enable --full-auto: {argv}"
    assert "/goal Build something cool" in argv, f"prompt not prefixed: {argv}"


def test_default_mode_unchanged(bin_dir: Path, mock_out: Path) -> None:
    rc, _ = run(bin_dir, "--", "Plain prompt", mock_out=mock_out)
    assert rc == 0
    argv = mock_out.read_text().splitlines()
    assert "Plain prompt" in argv, f"plain prompt was modified: {argv}"
    assert "--full-auto" not in argv, f"--full-auto leaked: {argv}"


def test_skip_git_repo_check_always_present(bin_dir: Path, mock_out: Path) -> None:
    """Regression for the non-git-workdir hang: the wrapper must always pass
    --skip-git-repo-check so codex doesn't bail when invoked outside a repo."""
    rc, _ = run(bin_dir, "--", "any prompt", mock_out=mock_out)
    assert rc == 0
    argv = mock_out.read_text().splitlines()
    assert "--skip-git-repo-check" in argv, f"missing --skip-git-repo-check: {argv}"


def test_goal_review_combo_rejected(bin_dir: Path, mock_out: Path) -> None:
    rc, out = run(bin_dir, "--goal", "--review", "--uncommitted", mock_out=mock_out)
    assert rc != 0, "expected non-zero exit for --goal --review"
    assert "cannot be combined with --review" in out, f"missing guard message: {out}"


def test_goal_no_double_prefix(bin_dir: Path, mock_out: Path) -> None:
    rc, _ = run(bin_dir, "--goal", "--", "/goal already prefixed", mock_out=mock_out)
    assert rc == 0
    argv = mock_out.read_text().splitlines()
    assert "/goal already prefixed" in argv, f"prompt mangled: {argv}"
    assert "/goal /goal already prefixed" not in argv, f"double prefix: {argv}"


def main() -> None:
    assert SCRIPT.exists(), f"missing: {SCRIPT}"
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        bin_dir = make_mock_codex(tmp)
        for fn in (
            test_help_includes_goal,
            test_goal_prepends_and_full_auto,
            test_default_mode_unchanged,
            test_goal_review_combo_rejected,
            test_goal_no_double_prefix,
            test_skip_git_repo_check_always_present,
        ):
            mock_out = tmp / f"{fn.__name__}.argv"
            fn(bin_dir, mock_out)
            print(f"PASS {fn.__name__}")
    print("OK")


if __name__ == "__main__":
    main()
