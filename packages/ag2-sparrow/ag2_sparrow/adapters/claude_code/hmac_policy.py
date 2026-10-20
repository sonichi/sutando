"""HMAC strict mode, decided at read time, never at install: the one owner of the rule.

The config records only the owner's choice (HMAC_KEY_FILE, the key path; HMAC_STRICT, empty when
--no-hmac-strict was given). Whether a task is submitted is decided here, per task, from that choice
plus the key file as it is at that moment: install, verify and the Mod's `inbox envelope` verb all
read this module, and none of them keeps its own copy. Stdlib only.

The rule:
  HMAC_STRICT=1 (a key path without --no-hmac-strict, or --hmac-strict)
                                    -> strict: only verified is submitted; while the key file is missing,
                                       unreadable, malformed or not recorded at all, every task is
                                       refused with that reason and held for retry, never read as off
  key path, --no-hmac-strict        -> lenient: every verdict but invalid is submitted, whatever the key
  no key path, no strict flag       -> off: every verdict but invalid is submitted (unsigned reads)
"""
from __future__ import annotations

import re
from pathlib import Path

STRICT, LENIENT, OFF = "strict", "lenient", "off"
VERDICTS = frozenset(("verified", "unsigned", "invalid", "unverifiable"))
KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
KEY_FILE, STRICT_FLAG = "HMAC_KEY_FILE", "HMAC_STRICT"
MODE_LINES = {STRICT: "strict (only verified envelopes are submitted), decided per task from the key file as it is then",
              LENIENT: "not strict (unsigned and unverifiable envelopes are submitted too)",
              OFF: "off (no key file: every task reads unsigned and is submitted)"}


def key_absent(path: str) -> bool:
    return not path or not Path(path).exists()


def key_problem(path: str) -> str:
    """'' when `path` holds the key the plugin's verifier accepts (64 hex characters), else why not."""
    if not path:
        return "no key file recorded (HMAC_KEY_FILE is empty): strict mode submits nothing"
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError as e:
        what = "missing" if key_absent(path) else "unreadable (%s)" % (e.strerror or e)
        return "%s %s: strict mode submits nothing until it is back" % (path, what)
    if not KEY_RE.fullmatch(text):
        return "%s is not a 64-hex-character key, so every envelope reads unverifiable" % path
    return ""


def mode(cfg: dict) -> str:
    """From the recorded choice alone; the key file's state enters only in decide(). The strict flag is
    read first: with it and no key path the mode is strict and fails closed, never off."""
    if cfg.get(STRICT_FLAG) == "1":
        return STRICT
    return LENIENT if cfg.get(KEY_FILE) else OFF


def decide(cfg: dict, verdict: str) -> "tuple[bool, str, bool]":
    """(submit, the one reason line when not, retry), read fresh for this task. retry is True only for a
    refusal the key file's return would lift (strict mode with the key missing, unreadable or malformed):
    the task is held and tried again, not retired."""
    if verdict not in VERDICTS:
        return False, "no verdict (%r)" % (verdict,), False
    if verdict == "invalid":
        return False, "envelope invalid", False
    if mode(cfg) != STRICT:
        return True, "", False
    problem = key_problem(cfg.get(KEY_FILE, ""))
    if problem:
        return False, "strict mode: " + problem, True
    if verdict != "verified":
        return False, "strict mode: envelope %s" % verdict, False
    return True, "", False


def describe(cfg: dict) -> str:
    return MODE_LINES[mode(cfg)]
