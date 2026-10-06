"""Smoke test for skills/agy/scripts/gemini-run.sh.

Mocks the agy/gemini binaries and verifies:
  1. `agy` on PATH is used directly, with --approval-mode translated to agy's
     --mode / --dangerously-skip-permissions flags.
  2. `agy` found only via the ~/.local/bin fallback (not on PATH, matching the
     cron/bridge shells named in skills/claude-router/SKILL.md) is still resolved
     and invoked.
  3. A gemini-only environment (no agy anywhere) still succeeds via the legacy
     gemini backend, using the parent commit's original argv mapping
     (--approval-mode passed straight through, --include-directories instead of
     --add-dir). A wrapper that requires agy fails this in any environment lacking it.
  4. An unknown --approval-mode is rejected regardless of backend.
  5. Prompt is required unless --check is used.
  6. In agy's Gemini-key mode with no GEMINI_API_KEY in the env, the key is read from
     the vault and reaches agy only through its env; an env key wins, a vault miss
     is not fatal, and the vault is never consulted outside that mode.

Run: python3 tests/agy-wrapper.test.py
"""
import os
import subprocess
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "agy" / "scripts" / "gemini-run.sh"

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
    # agy is NOT on PATH -- only reachable via $HOME/.local/bin, as in the
    # cron/bridge shells (skills/claude-router/SKILL.md:36).
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


FAKE_VAULT = """#!/usr/bin/env python3
import os, sys
open(os.environ["VAULT_CALLS"], "a").write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["get", "GEMINI_API_KEY"] and os.environ.get("VAULT_HAS_KEY") == "1":
    print("vault-secret-123")
    sys.exit(0)
sys.exit(1)
"""

KEY_MOCK = "#!/bin/bash\nprintf '%s' \"${GEMINI_API_KEY:-<unset>}\" >\"$MOCK_OUT\"\n"


def run_vault_case(tmp: Path, name: str, provider: bool, env_key: str | None, vault_has: bool) -> tuple[int, str, str, str]:
    tree = tmp / f"tree_{name}"
    (tree / "skills" / "agy" / "scripts").mkdir(parents=True)
    (tree / "skills" / "secret-vault").mkdir(parents=True)
    script = tree / "skills" / "agy" / "scripts" / "gemini-run.sh"
    script.write_text(SCRIPT.read_text())
    (tree / "skills" / "secret-vault" / "secret-vault.py").write_text(FAKE_VAULT)
    home = tmp / f"home_{name}"
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"modelProvider": "gemini"}' if provider else "{}")
    bin_dir = tmp / f"bin_{name}"
    bin_dir.mkdir()
    agy = bin_dir / "agy"
    agy.write_text(KEY_MOCK)
    agy.chmod(0o755)
    mock_out, calls = tmp / f"{name}.key", tmp / f"{name}.calls"
    calls.write_text("")
    env = os.environ.copy()
    env.update(HOME=str(home), MOCK_OUT=str(mock_out), VAULT_CALLS=str(calls),
               VAULT_HAS_KEY="1" if vault_has else "0",
               PATH=":".join([str(bin_dir), os.path.dirname(os.path.realpath(__import__("sys").executable)), "/usr/bin", "/bin"]))
    env.pop("GEMINI_API_KEY", None)
    if env_key is not None:
        env["GEMINI_API_KEY"] = env_key
    proc = subprocess.run(["bash", str(script), "--", "hi"], env=env, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr, mock_out.read_text() if mock_out.exists() else "", calls.read_text()


def test_vault_key_in_gemini_mode(tmp: Path) -> None:
    rc, out, seen, calls = run_vault_case(tmp, "vault", provider=True, env_key=None, vault_has=True)
    assert rc == 0, out
    assert seen == "vault-secret-123", f"agy did not get the vault key: {seen!r}"
    assert "vault-secret-123" not in out, "the key leaked to the wrapper's output"
    assert calls.strip() == "get GEMINI_API_KEY", calls


def test_env_key_wins(tmp: Path) -> None:
    rc, out, seen, calls = run_vault_case(tmp, "envwins", provider=True, env_key="from-env", vault_has=True)
    assert rc == 0 and seen == "from-env", (rc, seen, out)
    assert calls == "", f"vault consulted although the env had a key: {calls!r}"


def test_vault_miss_not_fatal(tmp: Path) -> None:
    rc, out, seen, calls = run_vault_case(tmp, "miss", provider=True, env_key=None, vault_has=False)
    assert rc == 0 and seen == "<unset>", (rc, seen, out)


def test_no_vault_outside_gemini_mode(tmp: Path) -> None:
    rc, out, seen, calls = run_vault_case(tmp, "nomode", provider=False, env_key=None, vault_has=True)
    assert rc == 0 and seen == "<unset>", (rc, seen, out)
    assert calls == "", f"vault consulted outside Gemini-key mode: {calls!r}"


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
            test_vault_key_in_gemini_mode,
            test_env_key_wins,
            test_vault_miss_not_fatal,
            test_no_vault_outside_gemini_mode,
        ):
            fn(tmp)
            print(f"PASS {fn.__name__}")
    print("OK")


if __name__ == "__main__":
    main()
