"""CI guard: the packaged modules must match sonichi/sutando src/ verbatim."""
import subprocess
import sys
from pathlib import Path

def test_package_in_sync_with_src():
    tool = Path(__file__).resolve().parent / "sync_from_src.py"
    r = subprocess.run([sys.executable, str(tool), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

if __name__ == "__main__":
    test_package_in_sync_with_src(); print("PASS — package in sync with src/")
