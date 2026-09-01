#!/usr/bin/env python3
"""discord-read.py must render FORWARDED message content, not a blank line.

A Discord forward carries empty top-level `content`; the payload lives in
`message_snapshots[0].message`. `discord-bridge.py` already reads that shape and
`discord_addressee.py` documents it — the READER did not, so every forwarded
message printed as an empty body.

That reader is what the context-reconstruct step runs on every pass, so the gap
is silent and systemic: any decision forwarded into a channel rather than typed
was invisible.

Measured 2026-07-31 in #research-eval: two forwards rendered blank while holding
the only record of a live AMA failure (a 1,602-char session transcript plus the
reporter's own words). They were nearly deleted on the strength of that blank
output.
"""
import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("dr", REPO / "src" / "discord-read.py")
dr = importlib.util.module_from_spec(spec)
sys.modules["dr"] = dr
spec.loader.exec_module(dr)

failures = []


def check(label, cond, detail=""):
    print(("  ok  " if cond else "  FAIL ") + label + ("" if cond else f" — {detail}"))
    if not cond:
        failures.append(label)


def fwd(inner_text, attachments=None, embeds=None, outer=""):
    return {
        "content": outer,
        "message_snapshots": [{"message": {
            "content": inner_text,
            "attachments": attachments or [],
            "embeds": embeds or [],
        }}],
    }


# --- the regression itself -------------------------------------------------
out = dr._render(fwd("I tried this morning and it started bluffing"))
check("A1 forwarded text is rendered, not dropped",
      "bluffing" in out, f"got {out!r}")
check("A2 the forward is LABELLED, not silently attributed to the forwarder",
      "[forwarded]" in out, f"got {out!r}")

# A forward is the substance someone moved deliberately — it must not be clipped
# at the ordinary 200-char scan limit. The live case was 1,602 chars.
long_body = "x" * 1602
out = dr._render(fwd(long_body))
check("A3 forwarded body is NOT clipped at CLIP (live case was 1602 chars)",
      out.count("x") == 1602, f"rendered {out.count('x')} of 1602")

# --- media-only forwards ---------------------------------------------------
out = dr._render(fwd("", attachments=[{"filename": "session.png"}]))
check("B1 attachment-only forward names the file",
      "session.png" in out, f"got {out!r}")
out = dr._render(fwd("", embeds=[{"title": "Deck v3"}]))
check("B2 embed-only forward names the embed",
      "Deck v3" in out, f"got {out!r}")
out = dr._render(fwd(""))
check("B3 a genuinely bodyless forward says so rather than printing blank",
      "no readable body" in out, f"got {out!r}")

# --- a forward WITH the forwarder's own comment ----------------------------
out = dr._render(fwd("see this", outer="worth a look"))
check("C1 forwarder's own text is kept alongside the forwarded body",
      "worth a look" in out and "see this" in out, f"got {out!r}")

# --- controls: ordinary messages must be untouched -------------------------
check("D1 control: plain message renders its content",
      dr._render({"content": "hello"}) == "hello")
check("D2 control: plain message IS still clipped at CLIP",
      len(dr._render({"content": "y" * 500})) == dr.CLIP)
check("D3 control: empty non-forward stays empty (no phantom label)",
      dr._render({"content": ""}) == "")
check("D4 control: missing content key does not raise",
      dr._render({}) == "")


# --- end-to-end through main(): the forward must reach STDOUT ---------------
# The unit checks above prove `_render` is correct; this proves the RENDER LOOP
# actually uses it. That is the line that was wrong in production — the helper
# never existed, so a suite that only exercised a helper would have passed
# against a reader that still printed blanks.
import io
import contextlib

_SAMPLE = [
    {"id": "2", "timestamp": "2026-07-30T17:17:20.000000+00:00",
     "author": {"username": "susanliu_"}, "content": "",
     "message_snapshots": [{"message": {"content": "the 1602-char transcript lives here",
                                        "attachments": [], "embeds": []}}]},
    {"id": "1", "timestamp": "2026-07-30T16:58:00.000000+00:00",
     "author": {"username": "susanliu_"}, "content": "an ordinary message"},
]

dr._load_token = lambda env: "test-token"
dr._fetch = lambda extra, channel_id, page, headers: list(_SAMPLE)

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = dr.main(["1532071853219385394", "--operator"])
printed = buf.getvalue()

check("E1 main() exits 0 with a stubbed fetch", rc == 0, f"rc={rc}")
check("E2 the ordinary message still prints its text",
      "an ordinary message" in printed, repr(printed))
check("E3 THE FORWARD REACHES STDOUT (the production failure)",
      "1602-char transcript" in printed, repr(printed))
check("E4 the forward is labelled in the output",
      "[forwarded]" in printed, repr(printed))
check("E5 output stays oldest-first",
      printed.index("an ordinary message") < printed.index("1602-char transcript"),
      repr(printed))

print()
if failures:
    print(f"FAIL — {len(failures)} check(s) failed")
    sys.exit(1)
print("PASS — discord-read forwarded-message tests")
