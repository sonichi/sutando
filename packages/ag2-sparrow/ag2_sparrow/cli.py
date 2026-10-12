"""Console entry for `ag2-sparrow`: `ag2-sparrow hatch ...` runs the Hatch adapter;
anything else starts the bridge exactly as before (its main() reads no argv)."""
import sys


def main() -> None:
    if sys.argv[1:2] == ["hatch"]:
        from .adapters.hatch.cli import main as hatch_main
        sys.exit(hatch_main(sys.argv[2:]))
    # Imported here: the bridge reads its token and dirs at import time.
    from .remote_gateway_bridge import main as bridge_main
    bridge_main()
