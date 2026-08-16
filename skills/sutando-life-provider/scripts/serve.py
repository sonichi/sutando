#!/usr/bin/env python3
"""Launch the runtime API with this optional provider injected."""

from __future__ import annotations

import sys
from pathlib import Path


_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parents[2]
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_REPO / "src" / "runtime-api"))

from discord_provider import registry_inputs as discord_registry_inputs  # noqa: E402
from github_provider import registry_inputs as github_registry_inputs  # noqa: E402
from server import main as runtime_main  # noqa: E402


def main() -> None:
    runtime_main([github_registry_inputs, discord_registry_inputs])


if __name__ == "__main__":
    main()
