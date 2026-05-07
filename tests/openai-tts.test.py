"""Smoke test for skills/openai-tts/scripts/synthesize.sh.

Mocks `curl` in PATH so we can verify argument plumbing + payload shape
without hitting the OpenAI API. Validates:
  1. Default voice = coral, default model = tts-1-hd.
  2. --voice override is honored.
  3. --out path override is honored.
  4. Missing text errors out.

Run: python3 tests/openai-tts.test.py
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "openai-tts" / "scripts" / "synthesize.sh"


def make_mock_curl(tmpdir: Path, capture_payload: Path) -> Path:
    """Fake `curl` that writes 'FAKE_MP3' to its -o target,
    captures the JSON payload, and prints '200' (the status code synthesize.sh
    is not actually checking — but we still want a clean stdout for set -e)."""
    bin_dir = tmpdir / "bin"
    bin_dir.mkdir()
    mock = bin_dir / "curl"
    mock.write_text(f"""#!/bin/bash
set -e
OUT=""
PAYLOAD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) OUT="$2"; shift 2 ;;
    -d) PAYLOAD="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[[ -n "$OUT" ]] && printf "FAKE_MP3" > "$OUT"
printf "%s" "$PAYLOAD" > "{capture_payload}"
""")
    mock.chmod(0o755)
    return bin_dir


def run(bin_dir: Path, *args: str) -> tuple[int, str]:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["OPENAI_API_KEY"] = "test-key-xyz"
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_defaults(bin_dir: Path, capture: Path, tmp: Path) -> None:
    out_path = tmp / "default.mp3"
    rc, _ = run(bin_dir, "--out", str(out_path), "--", "Hello world")
    assert rc == 0, _
    payload = json.loads(capture.read_text())
    assert payload["voice"] == "coral", payload
    assert payload["model"] == "tts-1-hd", payload
    assert payload["input"] == "Hello world", payload
    assert out_path.read_text() == "FAKE_MP3"


def test_voice_override(bin_dir: Path, capture: Path, tmp: Path) -> None:
    out_path = tmp / "ash.mp3"
    rc, _ = run(bin_dir, "--voice", "ash", "--out", str(out_path), "--", "Hi")
    assert rc == 0, _
    payload = json.loads(capture.read_text())
    assert payload["voice"] == "ash"


def test_out_path_honored(bin_dir: Path, capture: Path, tmp: Path) -> None:
    out_path = tmp / "nested" / "weird-name.mp3"
    rc, _ = run(bin_dir, "--out", str(out_path), "--", "x")
    assert rc == 0, _
    assert out_path.exists()
    assert out_path.read_text() == "FAKE_MP3"


def test_missing_text_errors(bin_dir: Path, _capture: Path, _tmp: Path) -> None:
    rc, _ = run(bin_dir, "--voice", "coral")
    assert rc != 0


def main() -> None:
    assert SCRIPT.exists(), f"missing: {SCRIPT}"
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        capture = tmp / "payload.json"
        bin_dir = make_mock_curl(tmp, capture)
        test_defaults(bin_dir, capture, tmp); print("PASS test_defaults")
        test_voice_override(bin_dir, capture, tmp); print("PASS test_voice_override")
        test_out_path_honored(bin_dir, capture, tmp); print("PASS test_out_path_honored")
        test_missing_text_errors(bin_dir, capture, tmp); print("PASS test_missing_text_errors")
    print("OK")


if __name__ == "__main__":
    main()
