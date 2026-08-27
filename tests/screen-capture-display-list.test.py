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
import json
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


class _Run:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def _with_run(fake):
    """Swap the module's subprocess.run for the duration of a block."""
    real = scs.subprocess.run
    scs.subprocess.run = fake
    return real


# --- _profiler_display_names parses the profiler's own shape -----------------
PROFILER_JSON = json.dumps({
    "SPDisplaysDataType": [{
        "spdisplays_ndrvs": [
            {"_name": "Color LCD", "_spdisplays_resolution": "1512 x 982",
             "spdisplays_main": "spdisplays_yes"},
            {"_name": "U28E510", "spdisplays_pixelresolution": "1920x1080"},
        ],
    }],
})

_real = _with_run(lambda *a, **k: _Run(0, PROFILER_JSON))
try:
    got = scs._profiler_display_names()
finally:
    scs.subprocess.run = _real
check("profiler yields one entry per monitor", len(got), 2)
check("point resolution parses to an aspect", round(got[0]["aspect"], 4), round(1512 / 982, 4))
check("main display flagged from spdisplays_main", got[0]["is_main"], True)
check("second monitor is not main", got[1]["is_main"], False)
check("pixelresolution key is honored too", round(got[1]["aspect"], 4), round(1920 / 1080, 4))

# A monitor with no parseable resolution must not divide by zero.
_real = _with_run(lambda *a, **k: _Run(0, json.dumps(
    {"SPDisplaysDataType": [{"spdisplays_ndrvs": [{"_name": "Odd", "_spdisplays_resolution": "n/a"}]}]})))
try:
    odd_names = scs._profiler_display_names()
finally:
    scs.subprocess.run = _real
check("unparseable resolution yields aspect 0.0, not a raise", odd_names[0]["aspect"], 0.0)
check("unparseable entry still carries its name", odd_names[0]["name"], "Odd")

# Non-zero exit and a hard raise both degrade to [] — names are decoration.
_real = _with_run(lambda *a, **k: _Run(1, ""))
try:
    check("profiler non-zero exit yields []", scs._profiler_display_names(), [])
finally:
    scs.subprocess.run = _real


def _boom(*a, **k):
    raise OSError("system_profiler missing")


_real = _with_run(_boom)
try:
    check("profiler raise yields [] rather than propagating", scs._profiler_display_names(), [])
finally:
    scs.subprocess.run = _real

# --- list_displays probes screencapture and stops at the first gap -----------
with tempfile.TemporaryDirectory() as probe_dir:
    real_dir = scs.DIR
    scs.DIR = probe_dir

    SIZES = {1: (3024, 1964), 2: (3840, 2160)}   # two attached displays, then a gap

    def fake_capture(argv, **kwargs):
        if argv[0] == "system_profiler":
            return _Run(0, PROFILER_JSON)
        idx = int([a for a in argv if a.startswith("-D")][0][2:])
        if idx not in SIZES:
            return _Run(1, "")                    # display 3 does not exist
        _png(argv[-1], *SIZES[idx])
        return _Run(0, "")

    _real = _with_run(fake_capture)
    try:
        found = scs.list_displays()
        leftover = os.listdir(probe_dir)
    finally:
        scs.subprocess.run = _real
        scs.DIR = real_dir

    check("probing stops at the first missing display", len(found), 2)
    check("index is the screencapture -D argument", [d["index"] for d in found], [1, 2])
    check("dimensions come from the probe's real PNG bytes",
          [(d["width"], d["height"]) for d in found], [(3024, 1964), (3840, 2160)])
    check("profiler name lands on the retina panel by aspect", found[0].get("name"), "Color LCD")
    check("profiler name lands on the 4K external", found[1].get("name"), "U28E510")
    check("probe files are cleaned up", leftover, [])

# A display PRESENT beyond the gap is what makes this load-bearing: without it,
# `break` and `continue` both yield [1,2] because everything past the gap fails.
with tempfile.TemporaryDirectory() as probe_dir:
    real_dir = scs.DIR
    scs.DIR = probe_dir
    ISLAND = {1: (3024, 1964), 2: (3840, 2160), 4: (1280, 720)}   # 3 missing, 4 present

    def fake_island(argv, **kwargs):
        if argv[0] == "system_profiler":
            return _Run(0, PROFILER_JSON)
        idx = int([a for a in argv if a.startswith("-D")][0][2:])
        if idx not in ISLAND:
            return _Run(1, "")
        _png(argv[-1], *ISLAND[idx])
        return _Run(0, "")

    _real = _with_run(fake_island)
    try:
        island = scs.list_displays()
    finally:
        scs.subprocess.run = _real
        scs.DIR = real_dir
    check("enumeration stops at the gap and ignores display 4",
          [d["index"] for d in island], [1, 2])

# A probe that raises must still clean up and must not take the listing down.
with tempfile.TemporaryDirectory() as probe_dir:
    real_dir = scs.DIR
    scs.DIR = probe_dir
    _real = _with_run(_boom)
    try:
        raised = None
        try:
            scs.list_displays()
        except Exception as e:   # noqa: BLE001 - asserting it DOES propagate or not
            raised = type(e).__name__
    finally:
        scs.subprocess.run = _real
        scs.DIR = real_dir
    check("a raising probe surfaces as OSError to the handler's try", raised, "OSError")

# --- /displays route ---------------------------------------------------------
class _FakeHandler(scs.Handler):
    """Exercises _handle_displays without binding a socket."""

    def __init__(self, authorized=True):    # noqa: D107 - deliberately skips BaseHTTPRequestHandler.__init__
        self.sent = None
        self._authorized = authorized

    def _require_capture_token(self):
        return self._authorized

    def _send_json(self, status, payload):
        self.sent = (status, payload)


h = _FakeHandler()
_real = _with_run(lambda *a, **k: _Run(1, ""))    # no displays probe successfully
try:
    h._handle_displays()
finally:
    scs.subprocess.run = _real
check("/displays answers 200 with a status envelope", h.sent[0], 200)
check("/displays reports ok", h.sent[1]["status"], "ok")
check("/displays returns a displays list", isinstance(h.sent[1]["displays"], list), True)

h_err = _FakeHandler()
_real = _with_run(_boom)
try:
    h_err._handle_displays()
finally:
    scs.subprocess.run = _real
check("enumeration failure answers 500, not a dead server", h_err.sent[0], 500)
check("500 body names the failure", h_err.sent[1]["status"], "error")

h_unauth = _FakeHandler(authorized=False)
h_unauth._handle_displays()
check("an unauthorized caller gets no display listing", h_unauth.sent, None)


# --- do_GET routes /displays to the listing, not to a capture ----------------
class _RouteHandler(scs.Handler):
    """Records which route do_GET dispatched to, without binding a socket."""

    def __init__(self, path):    # noqa: D107 - deliberately skips the base __init__
        self.path = path
        self.routed = None

    def _handle_capture(self):
        self.routed = "capture"

    def _handle_capture_video(self):
        self.routed = "capture-video"

    def _handle_displays(self):
        self.routed = "displays"


for path, want in [
    ("/displays", "displays"),
    ("/displays?token=x", "displays"),
    ("/capture", "capture"),
    ("/capture-video", "capture-video"),
]:
    r = _RouteHandler(path)
    r.do_GET()
    check(f"do_GET routes {path}", r.routed, want)

if failures:
    print(f"screen-capture display list: {len(failures)} FAILURE(S)")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("screen-capture display list: all checks passed")
