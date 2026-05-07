"""Smoke test for skills/openai-tts/scripts/synthesize.sh.

Mocks `curl` in PATH so we can verify argument plumbing + payload shape
without hitting the OpenAI API. Validates:
  1. --help renders the option list.
  2. Default voice = coral, default model = tts-1-hd.
  3. --voice / --model / --out are honored.
  4. --file reads input from a path.
  5. OPENAI_API_KEY is read from the env (we set it).

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
    """Create a fake `curl` that writes a 1-byte mp3 to its -o target,
    captures the JSON payload to a sentinel file, and returns HTTP 200."""
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
echo -n "200"
""")
    mock.chmod(0o755)
    return bin_dir


def run(bin_dir: Path, *args: str, env_extra: dict | None = None) -> tuple[int, str]:
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["OPENAI_API_KEY"] = "test-key-xyz"
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_help(bin_dir: Path, _capture: Path) -> None:
    rc, out = run(bin_dir, "--help")
    assert rc == 0
    for needle in ["Usage:", "--voice", "--out", "--file", "--model"]:
        assert needle in out, f"missing {needle!r} in --help"


def test_defaults(bin_dir: Path, capture: Path, tmp: Path) -> None:
    out_path = tmp / "default.mp3"
    rc, _ = run(bin_dir, "--out", str(out_path), "--", "Hello world")
    assert rc == 0, _
    payload = json.loads(capture.read_text())
    assert payload["voice"] == "coral", payload
    assert payload["model"] == "tts-1-hd", payload
    assert payload["input"] == "Hello world", payload
    assert out_path.read_text() == "FAKE_MP3"


def test_voice_and_model_overrides(bin_dir: Path, capture: Path, tmp: Path) -> None:
    out_path = tmp / "ash.mp3"
    rc, _ = run(bin_dir, "--voice", "ash", "--model", "tts-1", "--out", str(out_path), "--", "x")
    assert rc == 0, _
    payload = json.loads(capture.read_text())
    assert payload["voice"] == "ash"
    assert payload["model"] == "tts-1"


def test_file_input(bin_dir: Path, capture: Path, tmp: Path) -> None:
    txt = tmp / "script.txt"
    txt.write_text("Multi\nline\nscript")
    out_path = tmp / "from-file.mp3"
    rc, _ = run(bin_dir, "--out", str(out_path), "--file", str(txt))
    assert rc == 0, _
    payload = json.loads(capture.read_text())
    assert payload["input"] == "Multi\nline\nscript"


def test_missing_text_errors(bin_dir: Path, _capture: Path) -> None:
    rc, _ = run(bin_dir, "--voice", "coral")
    assert rc != 0


def main() -> None:
    assert SCRIPT.exists(), f"missing: {SCRIPT}"
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        capture = tmp / "payload.json"
        bin_dir = make_mock_curl(tmp, capture)
        test_help(bin_dir, capture); print("PASS test_help")
        test_defaults(bin_dir, capture, tmp); print("PASS test_defaults")
        test_voice_and_model_overrides(bin_dir, capture, tmp); print("PASS test_voice_and_model_overrides")
        test_file_input(bin_dir, capture, tmp); print("PASS test_file_input")
        test_missing_text_errors(bin_dir, capture); print("PASS test_missing_text_errors")
    print("OK")


if __name__ == "__main__":
    main()
