"""Named gateway instances: how one bridge's task ids and outbox stay apart from another's.

One host may run several bridges, each against its own gateway (GATEWAY_INSTANCE).
Broker ids are unique only within a gateway, so a named instance stores tasks as
`task-<instance>~<broker id>` and delivers results from `<results>/.outbox.<instance>`.
`~` is outside both the broker id and the instance charsets, so the mapping is
injective and the first `~` splits it. The empty instance is the primary: identity.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import re
import stat
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable

from . import diagnostics, file_lock, task_io

INSTANCE_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def suffix(instance: str) -> str:
    return f".{instance}" if instance else ""


def local_tid(broker_tid: str, instance: str) -> str:
    return f"task-{instance}~{broker_tid}" if instance else broker_tid


def broker_tid(local_tid: str, instance: str) -> str:
    prefix = f"task-{instance}~"
    if instance and local_tid.startswith(prefix):
        return local_tid[len(prefix):]
    return local_tid


def owns(local_tid: str, instance: str) -> bool:
    """Whether `instance` wrote this local id: its own prefix, or no `~` for the primary."""
    return local_tid.startswith(f"task-{instance}~") if instance else "~" not in local_tid


def outbox_root(results_dir: Path, instance: str) -> Path:
    """The outbox the bridge for `instance` delivers results from."""
    return Path(results_dir) / f".outbox{suffix(instance)}"


REDACTED_URL = "<unparseable gateway URL>"


def redact_url(value: str) -> str:
    """Scheme, host, port and path of a gateway URL, as Sparrow persists and logs it: userinfo,
    query and fragment dropped. Fails closed: REDACTED_URL when it does not parse as one."""
    try:
        p = urllib.parse.urlsplit(str(value))
        if not p.scheme or not p.hostname:
            return REDACTED_URL
        host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
        if p.port:
            host = f"{host}:{p.port}"
        return urllib.parse.urlunsplit((p.scheme, host, p.path, "", ""))
    except Exception:  # noqa: BLE001 - redaction must never break status I/O
        return REDACTED_URL


_SCRUBBED_TYPES: "dict[type, type]" = {}


def scrub_exception(exc: BaseException, gateway_url: str) -> BaseException:
    """`exc`, raised by a call that received the gateway URL, as an exception of a subclass of
    its own type (so every `except` and attribute read still works) whose text is only
    `<type> contacting <redacted gateway>`, plus the HTTP status when it has one. Nothing the
    original text held, in any encoding, can reach a log or a status file through it."""
    base = type(exc)
    safe = _SCRUBBED_TYPES.get(base)
    if safe is None:
        # The original is never reachable from it: no __context__ (set when raised in a handler).
        safe = _SCRUBBED_TYPES[base] = type(base.__name__, (base,), {
            "__str__": lambda self: self.__dict__.get("_scrubbed", base.__name__),
            "__repr__": lambda self: f"{base.__name__}({self.__dict__.get('_scrubbed', '')!r})",
            "__context__": property(lambda self: None, lambda self, value: None),
            "__module__": base.__module__, "__qualname__": base.__qualname__})
        diagnostics.SCRUBBED_TYPES.add(safe)
    code = exc.__dict__.get("code")
    text = f"{base.__name__} contacting {redact_url(gateway_url)}"
    if isinstance(code, int):
        text += f" (HTTP {code})"
    out = safe.__new__(safe)
    # Objects (an HTTPError's body and headers) and numbers stay; text and bytes never do.
    out.__dict__.update({k: (text if isinstance(v, str) else b"" if isinstance(v, (bytes, bytearray))
                             else scrub_exception(v, gateway_url) if isinstance(v, BaseException) else v)
                         for k, v in exc.__dict__.items()})
    if isinstance(exc, OSError):
        out.errno = exc.errno
    out.__dict__["_scrubbed"] = text
    out.__dict__["_gateway"] = redact_url(gateway_url)
    out.args = (text,)
    out.__traceback__ = exc.__traceback__
    return out


@contextlib.contextmanager
def gateway_call(gateway_url: str):
    """Wrap every call that receives the gateway URL, or a URL derived from it: what it raises
    is re-raised by scrub_exception, so no caller can log or persist text from it."""
    try:
        yield
    except Exception as e:  # noqa: BLE001 - re-raised as the same type, scrubbed
        raise scrub_exception(e, gateway_url) from None


def install_scrubbing_excepthooks(get_url: Callable[[], str]) -> None:
    """Backstop for a process that holds the gateway URL: an uncaught exception, in any thread,
    is reported as the file:line of its innermost frame, its type and its text through
    redact_text, never a raw traceback (a path and a line number cannot carry the URL)."""
    def clean(value) -> str:
        try:
            return diagnostics.scrub(redact_text(str(value), get_url() or ""))
        except Exception:  # noqa: BLE001 - a hook must report something, never the raw text
            return REDACTED_TEXT

    def report(exc_type, exc, tb, where: str) -> None:
        while tb is not None and tb.tb_next is not None:
            tb = tb.tb_next
        at = f" at {clean(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}" if tb is not None else ""
        record = f"{where}{clean(getattr(exc_type, '__name__', 'Exception'))}{at}: {clean(exc)}"
        if clean(record) != record:  # a secret spanning the pieces
            record = f"{where.split(':')[0]}: {REDACTED_TEXT}"
        try:
            sys.stderr.write(record + "\n")
        except Exception:  # noqa: BLE001 - nowhere left to report to
            pass

    def hook(exc_type, exc, tb):
        report(exc_type, exc, tb, "Uncaught ")

    def thread_hook(args):
        if args.exc_type is SystemExit:
            return
        name = clean(getattr(args.thread, "name", "?"))
        report(args.exc_type, args.exc_value, args.exc_traceback, f"Exception in thread {name}: ")

    sys.excepthook = hook
    threading.excepthook = thread_hook


REDACTED_TEXT = "<text withheld: it may contain the gateway URL>"


def _split_by_hand(url: str) -> "tuple[str, str, str]":
    before, _, fragment = url.partition("#")
    _, _, query = before.partition("?")
    userinfo = before.partition("://")[2].partition("/")[0].rpartition("@")[0]
    return userinfo, query, fragment


def _secret_parts(url: str) -> "list[str]":
    """Each part of the URL that may carry a credential, with the delimiter the URL itself puts
    before it, so a part of any length is matched where URL text renders it. Split by hand as
    well as by urlsplit, which drops tab, CR and LF from the parts it returns."""
    splits = [_split_by_hand(url)]
    try:
        p = urllib.parse.urlsplit(url)
        splits.append((p.netloc.rpartition("@")[0], p.query, p.fragment))
    except Exception:  # noqa: BLE001 - the hand split stands alone
        pass
    parts = []
    for userinfo, query, fragment in splits:
        if userinfo:
            parts.append(userinfo + "@")
            if ":" in userinfo:
                parts.append(":" + userinfo.partition(":")[2] + "@")
        if query:
            parts.append("?" + query)
            for pair in re.split(r"[&;]", query):
                key, eq, value = pair.partition("=")
                parts += [pair, "=" + value] if eq else ["?" + pair, "&" + pair, ";" + pair]
        if fragment:
            parts.append("#" + fragment)
    return [p for p in dict.fromkeys(parts) if p not in ("=", "?", "&", ";", "#", "@", ":@")]


def _url_form_re(form: str) -> "re.Pattern[str]":
    """`form` with its scheme and host matched in any case, which a URL ignores there, and the
    rest (userinfo, path, query: the bytes a credential is made of) exactly."""
    scheme, sep, rest = form.partition("://")
    if not sep:
        return re.compile(re.escape(form))
    authority, slash, tail = rest.partition("/")
    userinfo, at, host = authority.rpartition("@")
    return re.compile(f"(?i:{re.escape(scheme + sep)})" + re.escape(userinfo + at)
                      + f"(?i:{re.escape(host)})" + re.escape(slash + tail))


def register_gateway_secrets(url: str) -> None:
    """Add every credential part of the gateway URL to the process's secret registry: each
    delimited part, and the bare userinfo, password and query values long enough to match alone."""
    for part in _secret_parts(str(url or "")):
        diagnostics.register_secret(part)
    userinfo, query, fragment = _split_by_hand(str(url or ""))
    username, _, password = userinfo.partition(":")
    for value in (userinfo, username, password, fragment,
                  *(pair.partition("=")[2] for pair in re.split(r"[&;]", query))):
        diagnostics.register_secret(value, min_len=diagnostics.BARE_SECRET_MIN)


def redact_text(text: str, gateway_url: str) -> str:
    """Diagnostic text safe to log or persist: the full gateway URL replaced by its redacted
    form. Fails closed: REDACTED_TEXT when any credential-bearing part of the URL (userinfo,
    query or one of its pairs or values, fragment) survives in any rendering of it."""
    text, url = str(text), str(gateway_url or "")
    if not url:
        return text
    for form in sorted(diagnostics.forms(url), key=len, reverse=True):
        text = _url_form_re(form).sub(lambda _m: redact_url(url), text)
    views = {diagnostics.hex_upper(v) for v in (text, urllib.parse.unquote(text), urllib.parse.unquote_plus(text))}
    for part in _secret_parts(url):
        if any(diagnostics.hex_upper(form) in view for form in diagnostics.forms(part) for view in views):
            return REDACTED_TEXT
    return text


KEY_GRACE_S = 5.0


def binding_key_path() -> Path:
    """The per-install key the binding is computed with: in the config dir under the effective
    uid's passwd home (task_io.user_home), so every launch context resolves one key; never the
    state dir, which may be synced. OSError when the uid has no passwd entry."""
    home = task_io.user_home()
    if not home:
        raise OSError("no passwd home for this uid")
    return Path(home) / ".config" / "ag2-sparrow" / "binding.key"


def _read_key(path: Path) -> "bytes | None":
    """The key, only from a regular file this uid owns with mode 0600 holding exactly 64 hex
    digits (no newline)."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="ascii") as fh:
            st = os.fstat(fh.fileno())
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                    or stat.S_IMODE(st.st_mode) != 0o600):
                return None
            key = fh.read(65)
    except (OSError, UnicodeDecodeError):
        return None
    return key.encode("ascii") if re.fullmatch(r"[0-9a-f]{64}", key) else None


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _binding_key(create: bool) -> "bytes | None":
    try:
        path = binding_key_path()
    except OSError:
        return None
    key = _read_key(path)
    if key is not None or not create:
        return key
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with file_lock.locked_file(path.parent / f"{path.name}.lock", create_mode=0o600):
            return _publish_key(path)
    except OSError:
        return None


def _publish_key(path: Path) -> "bytes | None":
    """Under the key dir's lock, so one writer publishes or recovers and every other re-reads
    the key it published before binding with it."""
    key = _read_key(path)
    if key is not None:
        return key
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    # A bad key younger than the grace may be an older, unlocked writer mid-write.
    if st is not None and time.time() - st.st_mtime < KEY_GRACE_S:
        return None
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            os.fchmod(fh.fileno(), 0o600)
            fh.write(os.urandom(32).hex())
            fh.flush()
            os.fsync(fh.fileno())
        if st is None:
            os.link(tmp, str(path))  # the key appears whole or not at all
        else:
            os.replace(tmp, str(path))
        _fsync_dir(path.parent)
    except FileExistsError:
        pass  # an unlocked writer published first: its key, re-read below, is the key
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return _read_key(path)


def binding_digest(identity: str, gateway_url: str, create: bool = False) -> str:
    """HMAC-SHA256, under the per-install key, of an agent identity and the FULL gateway
    URL: binds a saved reading to one enrollment without persisting anything the URL could
    be recovered from. '' when either is missing or there is no key; only the owner-routing
    writer creates one (create=True)."""
    key = _binding_key(create) if identity and gateway_url else None
    if key is None:
        return ""
    return hmac.new(key, f"{identity}\n{gateway_url}".encode("utf-8"), hashlib.sha256).hexdigest()
