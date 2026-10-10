#!/usr/bin/env python3
"""discord-read.py must render TOP-LEVEL embeds and stickers, not a blank line.

The third instance of one defect, each a branch apart in `_render`: #2458 fixed
forwarded content, discord-read-attachments fixed top-level attachments, and the
top level still dropped `embeds` and `sticker_items` — while the forward branch
labelled embeds. So an embed-only post read as empty in the reader that
`context-reconstruct` runs on every proactive pass.

Measured 2026-09-23: one #dev post (thegreymutant73, 2026-09-21T22:10) rendered
as an empty body. Blank was ambiguous across three different inputs — genuinely
empty, embed-only, and sticker-only — so nothing in the output could tell the
reader whether a record existed at all. The fix routes BOTH levels through one
`_payload_marks()`, so the two cannot drift apart a fourth time.

This does NOT claim that particular post was embed-only; its raw payload was
never fetched. The claim under test is about the code path.
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


def fwd(inner):
    return {"content": "", "message_snapshots": [{"message": inner}]}


# --- top level: the branch that was dropping payloads -----------------------
out = dr._render({"content": "", "embeds": [{"title": "Sutando crashed on startup"}]})
check("an embed-only post is not blank", out != "", f"got {out!r}")
check("the embed TITLE is what identifies it",
      "Sutando crashed on startup" in out, f"got {out!r}")

out = dr._render({"content": "", "embeds": [{"type": "gifv"}]})
check("a titleless embed falls back to its type", "gifv" in out, f"got {out!r}")

out = dr._render({"content": "", "embeds": [{}]})
check("an embed with neither title nor type still marks its presence",
      out != "", f"got {out!r}")

out = dr._render({"content": "", "sticker_items": [{"name": "wave"}]})
check("a sticker-only post is not blank", "wave" in out, f"got {out!r}")

out = dr._render({"content": "look", "embeds": [{"title": "T"}],
                  "attachments": [{"filename": "f.png", "url": "u"}],
                  "sticker_items": [{"name": "s"}]})
for part in ("look", "T", "f.png", "s"):
    check(f"a mixed message keeps {part!r}", part in out, f"got {out!r}")

# --- blank must now mean EMPTY, which is what makes the output readable -----
check("a genuinely empty message is still blank",
      dr._render({"content": ""}) == "", f"got {dr._render({'content': ''})!r}")

# --- both levels go through one helper, so they cannot drift apart ----------
check("a forwarded embed is still labelled",
      "<embed: inner>" in dr._render(fwd({"content": "", "embeds": [{"title": "inner"}]})))
check("a forwarded STICKER is labelled too (the forward branch lacked this)",
      "sticker" in dr._render(fwd({"content": "", "sticker_items": [{"name": "s"}]})),
      f"got {dr._render(fwd({'content': '', 'sticker_items': [{'name': 's'}]}))!r}")
top = dr._render({"content": "", "embeds": [{"title": "X"}], "sticker_items": [{"name": "Y"}]})
inner = dr._render(fwd({"content": "", "embeds": [{"title": "X"}],
                        "sticker_items": [{"name": "Y"}]}))
check("the two levels emit the SAME marks for the same payload",
      top and top in inner, f"top={top!r} inner={inner!r}")

# Production-shaped token: `_redact` keys on a full-length key, so a short
# stand-in passes while proving nothing — the control at the end pins that.
SECRET = "sk-ant-api03-" + "A" * 95
leaky = dr._render({"content": "", "embeds": [{"title": f"token {SECRET}"}]})
check("an embed title is redacted before it is printed",
      SECRET not in leaky and "REDACTED" in leaky, f"got {leaky!r}")
sticky = dr._render({"content": "", "sticker_items": [{"name": SECRET}]})
check("a sticker name is redacted too", SECRET not in sticky, f"got {sticky!r}")
control = dr._render({"content": f"token {SECRET}"})
check("control: the same token is redacted in plain content",
      SECRET not in control, f"got {control!r}")

print(("FAILED: " + ", ".join(failures)) if failures
      else "PASS — discord-read embed + sticker tests")
sys.exit(1 if failures else 0)
