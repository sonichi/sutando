#!/usr/bin/env python3
"""Guards for /displays enumeration in screen-capture-server.py.

The display index is what every capture route takes, so it is probed from
`screencapture -D<n>` rather than inferred from system_profiler ordering, which
is not documented to match. Names are decoration matched back by aspect ratio.

The case that matters: a Retina panel reports 1512x982 points to the profiler
and captures at 3024x1964 pixels. Matching on size would fail; matching on
aspect ratio is what makes the name land on the right display.

Run: python3 tests/screen-capture-display-list.py
"""
import importlib.util
import os
import struct
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src", "screen-capture-server.py")

spec = importlib.util.spec_from_file_location("scs", SRC)
scs = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(scs)
except SystemExit:
    pass

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def _png(path, width, height):
    """Smallest valid PNG carrying a real IHDR, so _png_size reads true bytes."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr
    chunk += struct.pack(">I", zlib.crc32(b"IHDR" + ihdr) & 0xFFFFFFFF)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk)


# --- _png_size reads dimensions without an image library ---------------------
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "a.png")
    _png(p, 3024, 1964)
    check("_png_size retina", scs._png_size(p), (3024, 1964))

    junk = os.path.join(d, "b.png")
    with open(junk, "wb") as fh:
        fh.write(b"not a png at all")
    check("_png_size on non-PNG is (0,0), not a raise", scs._png_size(junk), (0, 0))

    check("_png_size on missing file is (0,0)", scs._png_size(os.path.join(d, "nope.png")), (0, 0))

# --- name matching -----------------------------------------------------------
NAMES = [
    {"name": "Color LCD", "aspect": 1512 / 982, "is_main": True},    # 1.5397
    {"name": "U28E510", "aspect": 1920 / 1080, "is_main": False},    # 1.7778
]

used = set()
builtin = {"index": 1, "width": 3024, "height": 1964}                # 1.5397, 2x points
scs._attach_name(builtin, NAMES, used)
check("retina panel matches on aspect despite 2x size", builtin.get("name"), "Color LCD")
check("main flag carried through", builtin.get("is_main"), True)

external = {"index": 2, "width": 3840, "height": 2160}               # 1.7778, 2x points
scs._attach_name(external, NAMES, used)
check("4K external matches its own profiler entry", external.get("name"), "U28E510")
check("external is not main", external.get("is_main"), False)

# A name is claimed once — two same-aspect displays must not both take it.
used2 = set()
one_name = [{"name": "Only", "aspect": 16 / 9, "is_main": False}]
a = {"index": 1, "width": 3840, "height": 2160}
b = {"index": 2, "width": 1920, "height": 1080}
scs._attach_name(a, one_name, used2)
scs._attach_name(b, one_name, used2)
check("first same-aspect display claims the name", a.get("name"), "Only")
check("second gets no name rather than a duplicate", b.get("name"), None)
check("unnamed display keeps its index", b["index"], 2)

# An aspect far from every candidate stays unnamed rather than guessing.
used3 = set()
odd = {"index": 1, "width": 1000, "height": 2000}                    # 0.5, portrait
scs._attach_name(odd, NAMES, used3)
check("no name is attached beyond tolerance", odd.get("name"), None)

# A zero-size probe cannot be matched and must not divide by zero.
used4 = set()
zero = {"index": 1, "width": 0, "height": 0}
scs._attach_name(zero, NAMES, used4)
check("zero-size display is left unnamed", zero.get("name"), None)

# --- profiler parsing is failure-tolerant ------------------------------------
check("_profiler_display_names returns a list", isinstance(scs._profiler_display_names(), list), True)

if failures:
    print(f"screen-capture display list: {len(failures)} FAILURE(S)")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("screen-capture display list: all checks passed")
