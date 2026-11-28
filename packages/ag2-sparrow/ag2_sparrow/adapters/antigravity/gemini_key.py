"""Antigravity on the Gemini API key the owner saved in the AG2 Space app, instead of the Google sign-in quota.

agy ignores `GEMINI_API_KEY` in its environment while a Google sign-in exists. What it does honour is ACP
`authenticate` with `{"methodId": "gemini-api-key", "_meta": {"apiKey": <key>}}` right after `initialize`:
it answers `{}`, and that one process runs on the key (the Google sign-in is left as it is). So the owner's
turns, and the setup check, send it when `initialize` advertises `gemini-api-key` in `authMethods` and the
owner saved a key for this agent in the app ("Use a Gemini API key" on the agent's Antigravity connection),
which keeps it in the macOS Keychain: service `space.ag2.app.agy-gemini-key.<agent id>`, account
`GEMINI_API_KEY`. No other item (the app's voice key included) is ever read.

The app may store the item as base64 JSON `{"tag": ..., "value": "<key>"}`; a bare key is taken as is.
Restricted (guest) turns run with `--gemini-dir <tmp>` and never get the owner's key: they stay on the sign-in.
`AG2_AGY_GEMINI_KEY=0` (or off/false/no) in the connector's environment turns this off. macOS only. The key
is read with `/usr/bin/security` (bounded), sent only inside the `authenticate` request on agy's stdin, never
put in agy's argv or environment, and never logged: agy's own stderr is stored through the client's
redaction, which is given the key before agy is.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
import sys

from ...acp_client import AcpError, AcpGone, AcpTimeout

METHOD_ID = "gemini-api-key"
API_KEY_AUTH = METHOD_ID   # the saved Session's `auth` when it was opened on the key
ACCOUNT = "GEMINI_API_KEY"
AGENT_SERVICE = "space.ag2.app.agy-gemini-key"
OPT_OUT_ENV = "AG2_AGY_GEMINI_KEY"
SECURITY = "/usr/bin/security"
READ_TIMEOUT_S = 10.0
GUEST_FLAG = "--gemini-dir"
NOT_FOUND = 44   # `security`'s exit when the item does not exist
VERSION_PREFIX = "ag2v1-"   # the app's item version: this and 32 random hex digits, set anew on every write


class AuthUnconfirmed(AcpError):
    """`authenticate` was sent and not answered: whether this agy runs on the key is not known."""


def agent_service(agent_id: str) -> str:
    """This agent's own Keychain service name."""
    return "%s.%s" % (AGENT_SERVICE, agent_id)


def unwrap(stored: str) -> str:
    """The key in a Keychain value: base64 JSON {"tag", "value"} -> value; a bare key as is; '' when unusable."""
    s = (stored or "").strip()
    if not s:
        return ""
    if s.startswith("eyJ"):   # base64 of '{"'
        try:
            obj = json.loads(base64.b64decode(s + "=" * (-len(s) % 4), validate=False).decode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return ""
        value = obj.get("value") if isinstance(obj, dict) else None
        return value.strip() if isinstance(value, str) else ""
    if s.startswith("{"):
        try:
            obj = json.loads(s)
        except ValueError:
            return ""
        value = obj.get("value") if isinstance(obj, dict) else None
        return value.strip() if isinstance(value, str) else ""
    return s if not any(c.isspace() for c in s) else ""


def _read_item(service: str, run, timeout: float) -> str:
    try:
        done = run([SECURITY, "find-generic-password", "-a", ACCOUNT, "-s", service, "-w"],
                   capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    if getattr(done, "returncode", 1) != 0:
        return ""
    return unwrap(getattr(done, "stdout", "") or "")


def opted_out(environ=None) -> bool:
    v = (os.environ if environ is None else environ).get(OPT_OUT_ENV, "")
    return v.strip().lower() in ("0", "off", "false", "no")


def gemini_api_key_from_app(agent_id: str = "", run=None, platform: str = None, environ=None,
                            timeout: float = READ_TIMEOUT_S) -> str:
    """This agent's Gemini API key saved in the app, or ''. macOS only, needs the agent id; never raises."""
    if not agent_id or (platform or sys.platform) != "darwin" or opted_out(environ):
        return ""
    return _read_item(agent_service(agent_id), run or subprocess.run, timeout)


def advertised(init) -> bool:
    methods = init.get("authMethods") if isinstance(init, dict) else None
    return any(isinstance(m, dict) and m.get("id") == METHOD_ID for m in (methods or []))


def restricted(argv) -> bool:
    """A guest turn's command line: its own agy home, which must keep the plain sign-in."""
    return any(str(a) == GUEST_FLAG or str(a).startswith(GUEST_FLAG + "=") for a in (argv or []))


def authenticate_with_app_key(client, init, timeout, argv, agent_id="", log=lambda *a: None,
                              key_source=None) -> bool:
    """After `initialize`: send `authenticate` gemini-api-key with the app's key when this is not a restricted
    turn, agy advertises the method and a key is saved. True when agy accepted it. A refusal (agy answers with
    an error) is logged, without the key or agy's message, which could echo it, and the turn goes on on the
    Google sign-in. No answer at all (a timeout, agy gone, a failed send) raises AuthUnconfirmed: that process
    may or may not be on the key, so no Session is opened or recorded on it."""
    if restricted(argv) or not advertised(init):
        return False
    key = (key_source or gemini_api_key_from_app)(agent_id)
    if not key:
        return False
    # Before the key leaves this process: whatever agy writes to stderr from here on is stored without it.
    redact = getattr(client, "redact", None)
    if callable(redact):
        redact(key)
    try:
        client.call("authenticate", {"methodId": METHOD_ID, "_meta": {"apiKey": key}}, timeout)
    except (AcpTimeout, AcpGone) as e:
        # No answer is not a refusal: agy may still finish authenticating, so which account this process
        # runs on is unknown. It gets no Session; the caller stops, and the next turn starts a fresh agy.
        log("Gemini API key from the app: no answer to authenticate (%s); this agy is not used" % type(e).__name__)
        raise AuthUnconfirmed("authenticate: no answer, so this process's account is unknown") from None
    except AcpError as e:   # agy answered with an error: an explicit refusal, and the sign-in still works
        log("Gemini API key from the app not accepted (%s); staying on the Google sign-in" % type(e).__name__)
        return False
    except Exception as e:  # noqa: BLE001 - a broken pipe and the like: unknown too
        log("Gemini API key from the app: authenticate failed (%s); this agy is not used" % type(e).__name__)
        raise AuthUnconfirmed("authenticate: it could not be sent or answered") from None
    finally:
        key = None
    log("using the Gemini API key saved in the app for this agent")
    return True


def key_saved(agent_id: str = "", run=None, platform: str = None, environ=None,
              timeout: float = READ_TIMEOUT_S) -> bool:
    """Whether this agent has a Gemini API key saved in the app, without reading the key: `security` is asked
    for the item's attributes only (no `-w`/`-g`), and what it prints is dropped. False off macOS, without the
    agent id, when opted out, and on any failure."""
    if not agent_id or (platform or sys.platform) != "darwin" or opted_out(environ):
        return False
    try:
        done = (run or subprocess.run)([SECURITY, "find-generic-password", "-a", ACCOUNT, "-s", agent_service(agent_id)],
                                       capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return getattr(done, "returncode", 1) == 0


# Only the item's version (its generic attribute, which the app sets fresh on each write) or, from an app that
# writes none, its creation and modification dates (14 digits + Z) are kept: nothing else it prints.
_ITEM_DATE = re.compile(r'"(cdat|mdat)"<timedate>=\S*\s*"(\d{14}Z)')
_ITEM_VERSION = re.compile(r'"gena"<blob>="(%s[0-9a-f]{32})"' % re.escape(VERSION_PREFIX))
UNKNOWN = None   # key_marker: the query failed, so whether the key changed is not known


def key_marker(agent_id: str = "", run=None, platform: str = None, environ=None,
               timeout: float = READ_TIMEOUT_S) -> "str | None":
    """A change marker for this agent's own saved key, with no secret in it, from the item's attributes (no
    `-w`/`-g`): "saved v=<version>" when the app set one, else "saved cdat=<date> mdat=<date>" (an older app);
    '' when none is saved (`security` said not found; also off macOS, without the agent id, opted out); UNKNOWN
    (None) on any other failure (no answer, another exit, output with neither), which is never read as removed."""
    if not agent_id or (platform or sys.platform) != "darwin" or opted_out(environ):
        return ""
    try:
        done = (run or subprocess.run)([SECURITY, "find-generic-password", "-a", ACCOUNT, "-s", agent_service(agent_id)],
                                       capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    code = getattr(done, "returncode", 1)
    if code == NOT_FOUND:
        return ""
    out = getattr(done, "stdout", "") or ""
    version = _ITEM_VERSION.search(out) if code == 0 else None
    if version:
        return "saved v=%s" % version.group(1)
    dates = dict(_ITEM_DATE.findall(out)) if code == 0 else {}
    if not dates:
        return UNKNOWN
    return " ".join(["saved"] + ["%s=%s" % (k, dates[k]) for k in ("cdat", "mdat") if k in dates])


REMOVED, ABSENT, FAILED = "removed", "absent", "failed"


def clear_key(agent_id: str, run=None, platform: str = None, timeout: float = READ_TIMEOUT_S) -> str:
    """Delete this agent's saved key ("Stop using the key"): REMOVED when `security` deleted it and it is gone,
    ABSENT when there was none to delete, FAILED otherwise (off macOS, no agent id, `security` did not run,
    failed, or the item is still there). Only this agent's own item is named, so no other agent's key, and not
    the app's voice key, can be removed by it."""
    if not agent_id or (platform or sys.platform) != "darwin":
        return FAILED
    try:
        done = (run or subprocess.run)([SECURITY, "delete-generic-password", "-a", ACCOUNT, "-s", agent_service(agent_id)],
                                       capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return FAILED
    code = getattr(done, "returncode", 1)
    if code == NOT_FOUND:
        return ABSENT
    if code != 0:
        return FAILED
    try:
        still = (run or subprocess.run)([SECURITY, "find-generic-password", "-a", ACCOUNT, "-s", agent_service(agent_id)],
                                        capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return FAILED
    return REMOVED if getattr(still, "returncode", 0) == NOT_FOUND else FAILED
