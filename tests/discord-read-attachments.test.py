#!/usr/bin/env python3
"""discord-read.py must render ATTACHMENTS, and carry the url that retrieves them.

Two defects, one branch apart in `_render`:

  * a message whose only payload is a top-level attachment rendered as a BLANK
    LINE — the forward bug of #2458, on the non-forward branch;
  * neither branch emitted the attachment `url`, so the reader could name a file
    and never open it. Measured live: the owner sent two SVGs and asked for an
    edit; the reader printed a filename placeholder, a scan for
    cdn.discordapp.com over the full output found 0 urls, and the request could
    not be served at all.

Fetch-side note for whoever consumes the url: the CDN refuses urllib's default
User-Agent with 403 and serves 200 for a browser one.
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


URL = "https://cdn.discordapp.com/attachments/1/2/logo.svg?ex=a&is=b&hm=c"


def plain(content="", atts=None):
    return {"content": content, "attachments": atts or []}


def fwd(atts=None, inner="", outer=""):
    return {"content": outer,
            "message_snapshots": [{"message": {"content": inner,
                                               "attachments": atts or [],
                                               "embeds": []}}]}


print("A. a top-level attachment is no longer invisible")
out = dr._render(plain("", [{"filename": "logo.svg", "url": URL}]))
check("A1 a file-only message does NOT render blank", out != "", repr(out))
check("A2 the filename is named", "logo.svg" in out, repr(out))
check("A3 the retrieving url is carried", URL in out, repr(out))
out = dr._render(plain("look at this", [{"filename": "logo.svg", "url": URL}]))
check("A4 the author's own text is kept alongside", out.startswith("look at this"), repr(out))
check("A5 ...and the attachment still follows it", "logo.svg" in out, repr(out))

print("B. a FORWARDED attachment carries its url too")
out = dr._render(fwd([{"filename": "trayicon.svg", "url": URL}]))
check("B1 still labelled as a forward", "[forwarded]" in out, repr(out))
check("B2 the filename survives (unchanged behaviour)", "trayicon.svg" in out, repr(out))
check("B3 the retrieving url is carried", URL in out, repr(out))

print("C. multiple attachments are all reachable")
out = dr._render(fwd([{"filename": "a.svg", "url": URL + "1"},
                      {"filename": "b.svg", "url": URL + "2"}]))
check("C1 both files named", "a.svg" in out and "b.svg" in out, repr(out))
check("C2 both urls carried", (URL + "1") in out and (URL + "2") in out, repr(out))

print("D. CONTROLS — nothing else changes")
check("D1 a plain text message is untouched", dr._render({"content": "hello"}) == "hello")
check("D2 the 200-char clip still applies to bodies",
      len(dr._render({"content": "y" * 500})) == dr.CLIP)
check("D3 a message with no content and no attachments stays empty",
      dr._render(plain("")) == "", repr(dr._render(plain(""))))
check("D4 an attachment with no url degrades to the name, not a dangling mark",
      dr._render(plain("", [{"filename": "x.png"}])) == "<attachment: x.png>",
      repr(dr._render(plain("", [{"filename": "x.png"}]))))
check("D5 a missing filename does not crash",
      "?" in dr._render(plain("", [{"url": URL}])))
check("D6 a forward with a text body is unchanged",
      dr._render(fwd(inner="the sentence")) == "[forwarded] the sentence",
      repr(dr._render(fwd(inner="the sentence"))))

print()
if failures:
    print(f"FAIL — {len(failures)} check(s): {failures}")
    sys.exit(1)
print("PASS — discord-read attachment tests")
