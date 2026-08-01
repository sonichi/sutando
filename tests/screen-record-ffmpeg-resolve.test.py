#!/usr/bin/env python3
"""Process-boundary evidence for the screen-record ffmpeg resolver (#2368).

The three call sites in `skills/screen-record/scripts/record.py` used to hardcode
`/opt/homebrew/bin/ffmpeg`. Two of them fail SILENTLY when that path is absent:
`_list_audio_devices()` swallows the exception and returns `[]`, and the
volumedetect guard sits inside `except Exception: pass` — so the very warning it
exists to emit can never fire. Only `start()` surfaced anything.

Static resolver assertions cannot show that. This runs the REAL functions against
a REAL child process and asserts the resolved binary is actually executed.

Fixture design follows the pattern @john-the-dev accepted on #2475 (the sibling
"resolve python instead of hardcoding" fix): a SELF-CONTAINED `/bin/sh` fake, so
the test never depends on an ambient `ffmpeg` and passes on a host that has none.
His approval there recorded the reasoning verbatim — "I did not reproduce on a
physical no-CLT macOS host, so the controlled process-boundary tests remain the
evidence for that environment."

The discriminator that matters: the fake is installed on a temp PATH entry that is
**not** `/opt/homebrew/bin`. On the pre-fix code the hardcoded literal cannot see
it, so the audio path returns `[]` and these tests fail. A resolved value equal to
the removed literal would prove nothing; a resolved value somewhere else proves
resolution.

Run: python3 tests/screen-record-ffmpeg-resolve.test.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RECORD_PY = REPO / "skills" / "screen-record" / "scripts" / "record.py"

# What avfoundation actually prints on STDERR for -list_devices.
FAKE_FFMPEG = r"""#!/bin/sh
for a in "$@"; do
  case "$a" in
    -list_devices)
      cat >&2 <<'EOF'
[AVFoundation indev @ 0x7f8] AVFoundation video devices:
[AVFoundation indev @ 0x7f8] [0] FaceTime HD Camera
[AVFoundation indev @ 0x7f8] AVFoundation audio devices:
[AVFoundation indev @ 0x7f8] [0] MacBook Air Microphone
[AVFoundation indev @ 0x7f8] [1] External Mic
EOF
      exit 1 ;;
    volumedetect)
      echo "[Parsed_volumedetect_0 @ 0x60] mean_volume: -91.0 dB" >&2
      exit 0 ;;
  esac
done
exit 0
"""

failures = []


def ok(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
    else:
        failures.append(name)
        print(f"  FAIL {name}{('  ' + detail) if detail else ''}")


def load_record():
    """Import record.py FRESH so module-level `FFMPEG = shutil.which(...)`
    re-resolves against the current PATH."""
    sys.modules.pop("_rec_under_test", None)
    spec = importlib.util.spec_from_file_location("_rec_under_test", RECORD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    assert RECORD_PY.exists(), f"missing {RECORD_PY}"
    tmp = tempfile.mkdtemp(prefix="sutando-ffprobe-")
    fake = Path(tmp) / "ffmpeg"
    fake.write_text(FAKE_FFMPEG)
    fake.chmod(0o755)

    orig_path = os.environ.get("PATH", "")

    # ---- resolvable on a NON-/opt/homebrew PATH entry -----------------------
    os.environ["PATH"] = f"{tmp}:{orig_path}"
    rec = load_record()

    # getattr, not attribute access: on the PRE-FIX code there is no module-level
    # FFMPEG at all, and a hard AttributeError would abort before the BEHAVIOURAL
    # checks below — which are the ones that actually demonstrate the bug.
    resolved = getattr(rec, "FFMPEG", None)
    ok("resolves the ffmpeg that is actually on PATH", resolved == str(fake),
       f"got {resolved!r}")
    ok("resolved path is NOT the removed /opt/homebrew literal "
       "(so this discriminates resolution from the old hardcode)",
       bool(resolved) and not resolved.startswith("/opt/homebrew/"), f"got {resolved!r}")

    # THE point: the audio path EXECUTES the resolved binary instead of
    # silently returning [] — the failure mode the hardcode caused.
    devs = rec._list_audio_devices()
    ok("_list_audio_devices() executes the resolved binary and PARSES devices "
       "(not the silent [])", devs == [(0, "MacBook Air Microphone"), (1, "External Mic")],
       f"got {devs!r}")
    ok("_has_audio_device() is True — the guard downstream of it works", rec._has_audio_device())

    picked = rec._pick_audio_device()
    ok("_pick_audio_device() returns a real index, not None", picked is not None,
       f"got {picked!r}")

    # volumedetect: the guard lives inline inside stop() under `except: pass`,
    # so exercise the same command shape it runs and assert the parse it depends on.
    # Use the module's OWN resolved value with no fallback. An earlier draft wrote
    # `resolved or "ffmpeg"`, which made this assertion pass on the pre-fix code too
    # (it silently used the PATH fake instead of the hardcode) — an assertion that
    # passes in both directions is decoration, not evidence.
    try:
        r = subprocess.run([resolved, "-i", "x.mov", "-af", "volumedetect",
                            "-vn", "-f", "null", "/dev/null"],
                           capture_output=True, text=True, timeout=10)
        stderr = r.stderr
    except (FileNotFoundError, TypeError):
        stderr = ""  # pre-fix: no resolver at all (TypeError on None) -> guard blind
    mean_db = None
    for line in stderr.splitlines():
        if "mean_volume:" in line:
            mean_db = float(line.split("mean_volume:", 1)[1].strip().split()[0])
            break
    ok("volume-detect path executes and its mean_volume parses "
       "(the silence warning can actually fire)", mean_db == -91.0, f"got {mean_db!r}")

    # ---- CONTROL: no ffmpeg anywhere -> documented graceful degrade ---------
    os.environ["PATH"] = "/nonexistent"
    rec2 = load_record()
    ok("CONTROL: with no ffmpeg on PATH, falls back to the bare name",
       getattr(rec2, "FFMPEG", None) == "ffmpeg",
       f"got {getattr(rec2, 'FFMPEG', None)!r}")
    ok("CONTROL: and the audio path degrades to [] without raising",
       rec2._list_audio_devices() == [])

    os.environ["PATH"] = orig_path

    print(f"\n{'PASS' if not failures else 'FAIL'} — "
          f"{8 - len(failures)}/8 checks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
