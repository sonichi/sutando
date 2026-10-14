#!/usr/bin/env python3
"""Build ag2-sparrow.pyz: the whole ag2_sparrow package as one stdlib zipapp.

The archive starts the bridge (`ag2-sparrow`). The other console entry runs
from the same file without a dispatcher, since the bridge's main() reads no argv:

    PYTHONPATH=ag2-sparrow.pyz python3 -m ag2_sparrow.outbox_cli --help

Usage: build_zipapp.py [--output PATH]   (default: dist/ag2-sparrow.pyz)
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import zipapp

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1]
ENTRY = "ag2_sparrow.remote_gateway_bridge:main"
INTERPRETER = "/usr/bin/env python3"


def _include(rel: pathlib.Path) -> bool:
    # Only the package tree; tests/, tools/ and assets/ stay out of the archive.
    parts = rel.parts
    if not parts or parts[0] != "ag2_sparrow" or "__pycache__" in parts:
        return False
    return rel.suffix not in (".pyc", ".pyo")


def build(output: pathlib.Path) -> pathlib.Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    zipapp.create_archive(PKG_ROOT, target=output, interpreter=INTERPRETER,
                          main=ENTRY, filter=_include, compressed=True)
    return output


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--output", type=pathlib.Path,
                    default=PKG_ROOT / "dist" / "ag2-sparrow.pyz")
    out = build(ap.parse_args(argv).output.resolve())
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
