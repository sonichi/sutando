"""Generate standalone distributions of the canonical room-message contract."""
from pathlib import Path
import sys


REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "src/room_message.py"
TARGETS = (
    "skills/agent-room-ops/room_message.py",
    "skills/task-progress/scripts/room_message.py",
    "packages/ag2-sparrow/ag2_sparrow/room_message.py",
)


def main() -> int:
    expected = SOURCE.read_bytes()
    if "--check" in sys.argv:
        drift = [name for name in TARGETS
                 if not (REPO / name).is_file() or (REPO / name).read_bytes() != expected]
        if drift:
            print("room-message contract drift: " + ", ".join(drift), file=sys.stderr)
            return 1
        print(f"ok: {len(TARGETS)} room-message distributions match canonical")
        return 0
    for name in TARGETS:
        (REPO / name).write_bytes(expected)
    print(f"synced {len(TARGETS)} room-message distributions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
