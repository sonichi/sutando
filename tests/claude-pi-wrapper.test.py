"""Smoke test for skills/claude-pi/scripts/pi-run.sh and its claude-router wiring.

Mocks the pi binary in PATH and verifies:
  1. Default invocation uses -p with the kimi-coding provider/model defaults.
  2. --read-only restricts pi to the read tool.
  3. --mode json and --thinking are forwarded; text mode adds no --mode flag.
  4. Prompt is required unless --check is used.
  5. route-ai.sh routes "kimi"/word-"pi" prompts to the pi wrapper (dry-run),
     but does not fire on substrings like "api" or "pipeline".

Run: python3 tests/claude-pi-wrapper.test.py
"""
import os
import subprocess
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "claude-pi" / "scripts" / "pi-run.sh"
ROUTER = REPO / "skills" / "claude-router" / "scripts" / "route-ai.sh"


def make_mock_pi(tmpdir: Path) -> Path:
    bin_dir = tmpdir / "bin"
    bin_dir.mkdir()
    mock = bin_dir / "pi"
    mock.write_text(
        "#!/bin/bash\n"
        'if [[ "${1:-}" == "--version" ]]; then echo 0.0.0-mock; exit 0; fi\n'
        'if [[ "${1:-}" == "auth" ]]; then echo ready; exit 0; fi\n'
        "printf '%s\\n' \"$@\" >\"$PI_MOCK_OUT\"\n"
    )
    mock.chmod(0o755)
    return bin_dir


def run(script: Path, bin_dir: Path, *args: str, mock_out: Path) -> tuple[int, str]:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["PI_MOCK_OUT"] = str(mock_out)
    proc = subprocess.run(
        ["bash", str(script), *args], env=env, capture_output=True, text=True
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_defaults(bin_dir: Path, mock_out: Path) -> None:
    rc, _ = run(SCRIPT, bin_dir, "--", "Plain prompt", mock_out=mock_out)
    assert rc == 0
    argv = mock_out.read_text().splitlines()
    assert "-p" in argv, f"missing -p (non-interactive): {argv}"
    assert "kimi-coding" in argv, f"default provider missing: {argv}"
    assert "kimi-for-coding" in argv, f"default model missing: {argv}"
    assert "Plain prompt" in argv, f"prompt missing: {argv}"
    assert "--mode" not in argv, f"--mode leaked in text mode: {argv}"
    assert "--tools" not in argv, f"--tools leaked without --read-only: {argv}"


def test_read_only(bin_dir: Path, mock_out: Path) -> None:
    rc, _ = run(SCRIPT, bin_dir, "--read-only", "--", "x", mock_out=mock_out)
    assert rc == 0
    argv = mock_out.read_text().splitlines()
    i = argv.index("--tools")
    assert argv[i + 1] == "read", f"--read-only did not restrict tools: {argv}"


def test_mode_and_thinking_forwarded(bin_dir: Path, mock_out: Path) -> None:
    rc, _ = run(
        SCRIPT, bin_dir, "--mode", "json", "--thinking", "high", "--", "x",
        mock_out=mock_out,
    )
    assert rc == 0
    argv = mock_out.read_text().splitlines()
    assert argv[argv.index("--mode") + 1] == "json", f"mode not forwarded: {argv}"
    assert argv[argv.index("--thinking") + 1] == "high", f"thinking not forwarded: {argv}"


def test_prompt_required(bin_dir: Path, mock_out: Path) -> None:
    rc, out = run(SCRIPT, bin_dir, mock_out=mock_out)
    assert rc != 0, "expected non-zero exit without prompt"
    assert "prompt required" in out, f"missing guard message: {out}"


def route_engine(bin_dir: Path, mock_out: Path, prompt: str) -> str:
    rc, out = run(ROUTER, bin_dir, "--dry-run", "--", prompt, mock_out=mock_out)
    assert rc == 0, f"router failed on {prompt!r}: {out}"
    for line in out.splitlines():
        if line.startswith("engine: "):
            return line.split(": ", 1)[1]
    raise AssertionError(f"no engine line in: {out}")


def test_router_pi_routing(bin_dir: Path, mock_out: Path) -> None:
    assert route_engine(bin_dir, mock_out, "ask kimi about this design") == "pi"
    assert route_engine(bin_dir, mock_out, "use pi to check this") == "pi"
    assert route_engine(bin_dir, mock_out, "audit the api surface") != "pi"
    assert route_engine(bin_dir, mock_out, "clean up the pipeline") != "pi"


def main() -> None:
    assert SCRIPT.exists(), f"missing: {SCRIPT}"
    assert ROUTER.exists(), f"missing: {ROUTER}"
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        bin_dir = make_mock_pi(tmp)
        for fn in (
            test_defaults,
            test_read_only,
            test_mode_and_thinking_forwarded,
            test_prompt_required,
            test_router_pi_routing,
        ):
            mock_out = tmp / f"{fn.__name__}.argv"
            fn(bin_dir, mock_out)
            print(f"PASS {fn.__name__}")
    print("OK")


if __name__ == "__main__":
    main()
