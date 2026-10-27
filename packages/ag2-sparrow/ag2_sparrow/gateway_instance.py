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
from pathlib import Path
from typing import Any, Callable

from . import diagnostics, file_lock, gateway_credentials, task_io

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


# URL redaction and exception scrubbing are the credential owner's; these names are its, re-exported.
REDACTED_URL = gateway_credentials.REDACTED_URL
REDACTED_TEXT = gateway_credentials.REDACTED_TEXT
redact_url = gateway_credentials.redact_url
redact_text = gateway_credentials.redact_text
scrub_exception = gateway_credentials.scrub_exception
register_gateway_secrets = diagnostics.register_gateway_url


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

    def unraisable_hook(unraisable):
        report(unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback, "Exception ignored: ")

    sys.excepthook = hook
    threading.excepthook = thread_hook
    sys.unraisablehook = unraisable_hook  # also what an exception in an atexit callback reaches


ROOM_PIECE_MIN = 6  # a shorter localpart is a "piece" of nearly any secret and reveals none
MATRIX_ROOM_RE = re.compile(r"!([A-Za-z0-9._~=-]{1,255}):([A-Za-z0-9.-]{1,255}(?::[0-9]{1,5})?)")
MATRIX_USER_RE = re.compile(r"@([A-Za-z0-9._=/+-]{1,255}):([A-Za-z0-9.-]{1,255}(?::[0-9]{1,5})?)")


def _own_server_id(value: Any, pattern: "re.Pattern[str]", identity: Any) -> str:
    """`value` when `pattern` matches it whole, its server part is the agent's own (the server of
    its `@local:server` identity), it holds no registered secret and its localpart, if
    ROOM_PIECE_MIN characters or longer, is no piece of one, in any case; '' otherwise."""
    m = pattern.fullmatch(value) if isinstance(value, str) else None
    server = identity.partition(":")[2] if isinstance(identity, str) and identity.startswith("@") else ""
    if m is None or not server or m.group(2).casefold() != server.casefold():
        return ""
    local = m.group(1)
    piece = len(local) >= ROOM_PIECE_MIN and diagnostics.holds_or_is_piece(local)
    return "" if diagnostics.contains_secret(value) or piece else value


def owner_room(room: Any, identity: str) -> str:
    """`room` when it is a Matrix room id on the agent's own homeserver (see _own_server_id); room
    ids without a server part (room version 12) are refused. The one check every writer and reader
    of the owner DM applies, so an echoed credential never becomes a room."""
    return _own_server_id(room, MATRIX_ROOM_RE, identity)


def owner_mxid(owner: Any, identity: str) -> str:
    """`owner` when it is a Matrix user id on the agent's own homeserver, by owner_room's rule."""
    return _own_server_id(owner, MATRIX_USER_RE, identity)


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
