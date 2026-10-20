"""HMAC strict mode, decided at read time, never at install: the one owner of the rule.

The config records only the owner's choice (HMAC_KEY_FILE, the key path; HMAC_STRICT, "1" for strict,
"0" when --no-hmac-strict was given). Whether a task is submitted is decided here, per task, from that
choice plus the key file as it is at that moment: install, verify and the Mod's `inbox envelope` verb
all read this module, and none of them keeps its own copy. Stdlib only.

The rule, closed by default:
  HMAC_STRICT=1                     -> strict, whatever else the config says: only verified is submitted;
                                       while the key file is missing, unreadable, malformed or no path is
                                       recorded, every task is refused with that reason and held for retry
  key path, HMAC_STRICT=0           -> lenient (the explicit --no-hmac-strict record): every verdict but
                                       invalid is submitted, whatever the key file holds
  key path, HMAC_STRICT= (present, empty) -> lenient too: the record an older install wrote for
                                       --no-hmac-strict; verify warns, nothing need be rerun
  key path, any other HMAC_STRICT   -> strict (a missing line or an unknown value is not an opt-out)
  no key path, not HMAC_STRICT=1    -> off: every verdict but invalid is submitted (unsigned reads)
An incomplete or unreadable config is refused upstream (the verb parks it); it never reads as off.
The verdict is bound to the key the verifier read (its digest, `key_seen`): when the key file reads
otherwise here, the verdict is stale and the task is held to be verified again, never retired.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

STRICT, LENIENT, OFF = "strict", "lenient", "off"
VERDICTS = frozenset(("verified", "unsigned", "invalid", "unverifiable"))
KEY_RE = re.compile(r"[0-9a-fA-F]{64}")
ABSENT = "absent"
KEY_FILE, STRICT_FLAG, OPT_OUT, LEGACY_OPT_OUT = "HMAC_KEY_FILE", "HMAC_STRICT", "0", ""
MODE_LINES = {STRICT: "strict (only verified envelopes are submitted), decided per task from the key file as it is then",
              LENIENT: "not strict (unsigned and unverifiable envelopes are submitted too)",
              OFF: "off (no key file: every task reads unsigned and is submitted)"}


def key_absent(path: str) -> bool:
    return not path or not Path(path).exists()


def _read(path: str) -> "tuple[bytes | None, OSError | None]":
    if not path:
        return None, None
    try:
        return Path(path).read_bytes(), None
    except OSError as e:
        return None, e


def _digest(raw: "bytes | None") -> str:
    return ABSENT if raw is None else hashlib.sha256(raw).hexdigest()


def _problem(path: str, raw: "bytes | None", err: "OSError | None") -> str:
    if not path:
        return "no key file recorded (HMAC_KEY_FILE is empty): strict mode submits nothing"
    if raw is None:
        what = "missing" if key_absent(path) else "unreadable (%s)" % ((err and err.strerror) or err)
        return "%s %s: strict mode submits nothing until it is back" % (path, what)
    if not KEY_RE.fullmatch(raw.decode("utf-8", errors="replace").strip()):
        return "%s is not a 64-hex-character key, so every envelope reads unverifiable" % path
    return ""


def key_problem(path: str) -> str:
    """'' when `path` holds the key the plugin's verifier accepts (64 hex characters), else why not."""
    return _problem(path, *_read(path))


def key_digest(path: str) -> str:
    """sha256 hex of the key file's bytes, or "absent" when there is none to read: the verifier's digest."""
    return _digest(_read(path)[0])


def mode(cfg: dict) -> str:
    """From the recorded choice alone; the key file's state enters only in decide(). The strict flag is
    read first: with it and no key path the mode is strict and fails closed, never off. A key path is
    strict unless the opt-out is recorded: "0", or the empty value an older install wrote (a present
    line; a missing one is not a record)."""
    flag = cfg.get(STRICT_FLAG)
    if flag == "1":
        return STRICT
    if not cfg.get(KEY_FILE):
        return OFF
    return LENIENT if flag in (OPT_OUT, LEGACY_OPT_OUT) else STRICT


def legacy_opt_out(cfg: dict) -> bool:
    """The opt-out as an older install recorded it: a key path with HMAC_STRICT present and empty."""
    return bool(cfg.get(KEY_FILE)) and cfg.get(STRICT_FLAG) == LEGACY_OPT_OUT


def decide(cfg: dict, verdict: str, key_seen: "str | None" = None) -> "tuple[bool, str, bool]":
    """(submit, the one reason line when not, retry), from one read of the key file for this task. retry
    is True for a refusal the key file's return would lift (strict mode with the key missing, unreadable
    or malformed), and for a verdict whose `key_seen` is not the key read here: held, never retired."""
    if verdict not in VERDICTS:
        return False, "no verdict (%r)" % (verdict,), False
    path = cfg.get(KEY_FILE, "")
    raw, err = _read(path)
    if key_seen is not None and key_seen != _digest(raw):
        return False, "the key file changed after this envelope was verified; verified again", True
    if verdict == "invalid":
        return False, "envelope invalid", False
    if mode(cfg) != STRICT:
        return True, "", False
    problem = _problem(path, raw, err)
    if problem:
        return False, "strict mode: " + problem, True
    if verdict != "verified":
        return False, "strict mode: envelope %s" % verdict, False
    return True, "", False


def describe(cfg: dict) -> str:
    return MODE_LINES[mode(cfg)]
