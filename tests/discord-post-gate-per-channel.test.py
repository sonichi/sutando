#!/usr/bin/env python3
"""`bridges.discord_post_gate` as a {channel_id: path} map routes each send
to its own policy FILE, and a broken entry refuses only its own channel.

The single-path form is what shipped; a deployment needing #dev gated
differently from #gtm had to put both rulesets in one file and branch inside
`validate`. This proves the map form works AND that it does not weaken the
two properties the single-path form guaranteed:

  - a configured-but-unloadable policy still fails CLOSED, and
  - a load failure does not spread past the channel that named it.

`*` is REQUIRED, and that is the security property a map most easily loses: an
omitted `*` would let an unlisted channel send unvalidated, making config
omission a policy bypass. A mapping without a usable `*` refuses EVERY send,
and dispatch tests membership so a listed channel never falls through to `*`.

Run: python3 tests/discord-post-gate-per-channel.test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
os.environ["SUTANDO_TEST_MODE"] = "1"
os.environ.pop("SUTANDO_DISCORD_POST_GATE", None)

import channels.discord.post_gate as dpg  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


D = Path(tempfile.mkdtemp(prefix="pg-per-channel-"))
(D / "refuse.py").write_text(
    "def validate(channel_id, payload):\n    return 'refused-by-A'\n", encoding="utf-8")
(D / "allow.py").write_text(
    "def validate(channel_id, payload):\n    return None\n", encoding="utf-8")
(D / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
(D / "star.py").write_text(
    "def validate(channel_id, payload):\n    return 'refused-by-STAR'\n", encoding="utf-8")


def resolve(mapping):
    """resolve_validator with `mapping` as the config value."""
    orig = dpg._configured_target
    dpg._configured_target = lambda repo_root=None: mapping
    try:
        return dpg.resolve_validator()
    finally:
        dpg._configured_target = orig


P = {"content": "x"}

# --- the feature: two channels, two FILES -------------------------------
v = resolve({"111": str(D / "refuse.py"), "222": str(D / "allow.py"), "*": str(D / "allow.py")})
check("mapped channel 111 refuses via its own file", v("111", P) == "refused-by-A", repr(v("111", P)))
check("mapped channel 222 allows via its own file", not v("222", P), repr(v("222", P)))
check("int channel id resolves like its str form", v(111, P) == "refused-by-A", repr(v(111, P)))

# --- `*` fallback --------------------------------------------------------
v = resolve({"111": str(D / "allow.py"), "*": str(D / "star.py")})
check("unlisted channel falls back to `*`", v("999", P) == "refused-by-STAR", repr(v("999", P)))
check("listed channel is NOT overridden by `*`", not v("111", P), repr(v("111", P)))

# --- P1: an OMITTED `*` must never create an ungated send. client.py's
# contract: config selects WHICH ruleset applies, never WHETHER one does.
for partial in ({"111": str(D / "refuse.py")},
                {"111": str(D / "refuse.py"), "*": None},
                {"111": str(D / "refuse.py"), "*": "   "}):
    v = resolve(partial)
    r = v("999", P)
    check(f"omitted/unusable `*` refuses UNLISTED channel ({partial.get('*')!r})",
          isinstance(r, str) and "unvalidated" in r, repr(r))
    r2 = v("111", P)
    check(f"...and refuses the LISTED one too, whole map is closed ({partial.get('*')!r})",
          isinstance(r2, str), repr(r2))

# a listed key whose validator is falsy must NOT fall through to `*`
class _Falsy:
    def __bool__(self): return False
    def __call__(self, c, p): return "refused-by-FALSY-CALLABLE"
v = _dispatch_direct = None
import channels.discord.post_gate as _dpg
v = _dpg._dispatching({"111": _Falsy(), "*": lambda c, p: None})
check("listed key with a FALSY validator does not fall through to `*`",
      v("111", P) == "refused-by-FALSY-CALLABLE", repr(v("111", P)))

# --- a broken entry fails closed for ITS channel only --------------------
v = resolve({"111": str(D / "broken.py"), "222": str(D / "allow.py"), "*": str(D / "allow.py")})
r = v("111", P)
check("broken policy refuses its own channel", isinstance(r, str) and "failed to load" in r, repr(r))
check("broken policy does NOT refuse a sibling channel", not v("222", P), repr(v("222", P)))
r = v("111", P)
check("refusal names the failing path", isinstance(r, str) and "broken.py" in r, repr(r))

# --- P1: a LISTED channel with an empty path must not be answered by `*`.
# Both halves of this axis were tested; their INTERSECTION was the hole.
for empty_val in (None, "", "   "):
    v = resolve({"sensitive": empty_val, "*": str(D / "allow.py")})
    r = v("sensitive", P)
    check(f"listed channel with {empty_val!r} path REFUSES, `*` cannot answer for it",
          isinstance(r, str) and "no policy path" in r, repr(r))
    check(f"...and the refusal names the channel ({empty_val!r})",
          isinstance(r, str) and "sensitive" in r, repr(r))
    check(f"...while a genuinely unlisted channel still uses `*` ({empty_val!r})",
          v("other", P) is None, repr(v("other", P)))

v = resolve({"111": str(D / "allow.py"), "*": None})
r = v("999", P)
check("an empty `*` closes the WHOLE map (no ungated send anywhere)",
      isinstance(r, str) and "unvalidated" in r, repr(r))
check("...including the listed channel -- fail-closed is not per-key here",
      isinstance(v("111", P), str), repr(v("111", P)))

# --- a mapping naming nothing is configured-but-empty, not unconfigured --
for empty in ({}, {"111": ""}, {"111": None}):
    r = resolve(empty)
    got = r("111", P) if callable(r) else r
    check(f"empty mapping {empty!r} fails CLOSED, never None",
          callable(r) and isinstance(got, str) and "names no policy path" in got, repr(got))

# --- regression: the single-path form is unchanged -----------------------
v = resolve(str(D / "refuse.py"))
check("string path still gates every channel (regression)",
      v("111", P) == "refused-by-A" and v("999", P) == "refused-by-A")
check("unconfigured empty string still resolves to None (regression)",
      resolve("") is None, repr(resolve("")))
r = resolve(str(D / "broken.py"))
check("string path to a broken policy still fails closed (regression)",
      callable(r) and "failed to load" in r("111", P))

# --- env override stays a single GLOBAL path -----------------------------
os.environ["SUTANDO_DISCORD_POST_GATE"] = str(D / "refuse.py")
try:
    t = dpg._configured_target()
    check("env override returns a str, never a mapping", isinstance(t, str), repr(t))
    check("env override wins over a config mapping", t == str(D / "refuse.py"))
finally:
    os.environ.pop("SUTANDO_DISCORD_POST_GATE", None)

# A key selects WHICH policy runs (#3389): an untrimmed one silently routes its
# channel to `*` — still gated, but by the wrong and usually laxer policy.
for _key in (" 111", "111 ", "\t111"):
    _v = resolve({_key: str(D / "refuse.py"), "*": str(D / "star.py")})
    check(f"whitespace key {_key!r} still reaches its own policy",
          _v("111", P) == "refused-by-A", repr(_v("111", P)))
_v = resolve({"111": str(D / "refuse.py"), "*": str(D / "star.py")})
check("control — a clean key behaves identically", _v("111", P) == "refused-by-A", repr(_v("111", P)))
check("control — an unlisted channel still falls through to `*`",
      _v("999", P) == "refused-by-STAR", repr(_v("999", P)))
_v = resolve({" * ": str(D / "star.py")})
check("`*` itself survives key stripping", _v("999", P) == "refused-by-STAR", repr(_v("999", P)))

print(("\nFAILED: " + ", ".join(FAILS)) if FAILS else "\nAll per-channel post-gate checks passed.")
sys.exit(1 if FAILS else 0)
