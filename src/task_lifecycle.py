"""Task execution lifecycle for an adapter that serves one work dir and calls a provider per task.

One owner for what such an adapter must get right across crashes, so no adapter re-derives it:

- The generation classifier. `classify` answers, strictly, which generation of a task's result
  exists: LIVE_READY, LIVE_UNREADY, CLAIMED (a disposal claim), ARCHIVED, QUARANTINED, ABSENT, or
  UNKNOWN when any lookup failed. Precedence: UNKNOWN > LIVE_READY > ARCHIVED > QUARANTINED >
  CLAIMED > LIVE_UNREADY > ABSENT. Terminal states settle; CLAIMED, LIVE_UNREADY and UNKNOWN park.
- Per-task claims under `state/claims/<task_id>/`, one attempt at a time:
  PREPARED -> CALLING -> {NOT_DELIVERED | CONFIRMED | OUTCOME_UNKNOWN} -> RESULT_STAGED ->
  PUBLISHED -> SETTLED. CALLING is durable before the provider is called. A claim carries its
  mode: `cloud` (the writer's {pid, start}; absent = legacy cloud) or `local` (bound to the consumer
  generation {pid, start} of a claude-local session), whose turn runs PREPARED -> CALLING
  (authorize_call, one token) -> TURN_CONFIRMED (confirm_turn) -> RESULT_STAGED (stage_result: a
  descriptor anchored on the claim, then the body installed by link, never partially). Mode is
  enforced on every entry: no-generation `recover` and the cloud adapter refuse local claims, and
  only `recover(generation=..., proof=False)` settles a dead generation's local claims.
- Publication: the staged body goes to a unique temp in `results/`, is read back, renamed to a
  private sibling, then renamed no-replace to the final name, with directory fsyncs, all under
  `results/.disposal.lock`. A successful rename is the win; a final file missing afterwards (a drain
  took it) still reads as won, never as a retry.
- The rate journal `state/rate.jsonl`: one line per attempt event with a stable id; a reducer keeps
  one charge per attempt. And `state/settled-ids`: append-only, one validated id per record, written
  after the claim says SETTLED and before the id counts as settled, so a settled id is never called again.
- The fresh-dir rule: a first start needs an empty work dir and writes `state/format`; later starts
  need that marker and only this owner's names in `state/`. `enable_local_format` bumps the marker
  to `claude-local/1` (after the stable claims.lock inode and state/ are fsynced): the fence an
  older reader refuses at startup, and what a tagged local claim requires.

Delivery and claims of published results belong to the bridge's outbox; nothing here re-implements
them. Stdlib plus the package's result lookup modules only.
"""
from __future__ import annotations

import errno
import hashlib
try:
    import fcntl
except ImportError:                                     # no flock: one writer per ledger, as before
    fcntl = None
import importlib
import json
import os
import re
import shutil
import stat as _stat
import sys
import time
import uuid
from pathlib import Path

try:
    from . import local_task_protocol, result_disposal, result_ready, undelivered_quarantine
except ImportError:  # a flat load (importlib in tests, tools, bridges) has no parent package
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    local_task_protocol = importlib.import_module("local_task_protocol")
    undelivered_quarantine = importlib.import_module("undelivered_quarantine")
    result_disposal = importlib.import_module("delivery.disposal")
    result_ready = importlib.import_module("delivery.readiness")

EXIT_REFUSED = 2
HOUR = 3600.0
COMPACT_AFTER = 24 * HOUR
JOURNAL_KEEP = 2 * HOUR

LIVE_READY, LIVE_UNREADY, CLAIMED = "LIVE_READY", "LIVE_UNREADY", "CLAIMED"
ARCHIVED, QUARANTINED, ABSENT, UNKNOWN = "ARCHIVED", "QUARANTINED", "ABSENT", "UNKNOWN"
PRECEDENCE = (UNKNOWN, LIVE_READY, ARCHIVED, QUARANTINED, CLAIMED, LIVE_UNREADY, ABSENT)
TERMINAL = frozenset((LIVE_READY, ARCHIVED, QUARANTINED))
PARKED = frozenset((CLAIMED, LIVE_UNREADY, UNKNOWN))

PREPARED, CALLING, NOT_DELIVERED = "PREPARED", "CALLING", "NOT_DELIVERED"
CONFIRMED, OUTCOME_UNKNOWN, RESULT_STAGED = "CONFIRMED", "OUTCOME_UNKNOWN", "RESULT_STAGED"
TURN_CONFIRMED = "TURN_CONFIRMED"
PUBLISHED, SETTLED = "PUBLISHED", "SETTLED"
CALLABLE = frozenset((PREPARED, NOT_DELIVERED))

CLOUD, LOCAL = "cloud", "local"
CLAIM_VERSION = 2
FORMAT = "cloud-session/1"
FORMAT_LOCAL = "claude-local/1"
FORMATS = frozenset((FORMAT, FORMAT_LOCAL))
# The two claude-local records and the two per-root locks live in state/ by name.
LOCAL_STATE_FILES = ("claude-local-plugin.json", "claude-local-supervisor.json", "claims.lock", "progress.lock")
STATE_ALLOWED = frozenset(("format", "claims", "settled-ids", "rate.jsonl", "done", "collaborator-turns.json")
                          + LOCAL_STATE_FILES)
# Optional records the adapters rewrite in place: when present, each must be a plain file.
STATE_OPTIONAL_FILES = frozenset(("collaborator-turns.json",) + LOCAL_STATE_FILES)
FORMAT_TEMP = re.compile(r"\.format\.\d+\.[0-9a-f]{6}\.tmp")
# A staged body's private temp: .<task>.<attempt>.<identity>.<pid>.<random>.tmp, beside the blob.
BLOB_TEMP = re.compile(r"^\.(.+)\.(\d+)\.([0-9a-f]{24})\.(\d+)\.[0-9a-f]{8}\.tmp$")
# A private temp older than the launcher's inner stop bound belongs to nobody.
TEMP_SWEEP_AFTER = 120.0
# The gateway bridge's own entries when its state dir sits directly in the work root: its poller lock
# files, its default HITL stores and its log (the bridge's names, pinned to it by test).
BRIDGE_LOCKS = re.compile(r"gateway-bridge(\.[A-Za-z0-9_-]+)?\.lock(\.guard|\.tmp\.\d+)?")
BRIDGE_HITL = re.compile(r"requirements(-[a-z0-9-]+)?")
BRIDGE_LOGS = re.compile(r"gateway-bridge\.log(\.1)?")
# A refusal that names an unreadable work dir, not its layout: retryable.
UNCHECKED = "refusing to start: cannot check"


class StartupRefused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class ClaimUnreadable(Exception):
    pass


class AttemptInProgress(Exception):
    """prepare() for a task whose attempt has not reached an outcome."""


class TurnRefused(Exception):
    """confirm_turn() for a claim that is not CALLING or whose nonce or text hash does not match."""


class StateError(StartupRefused):
    """A claim or the ledger could not be read or written: the claim stays as it was, no work is
    admitted, nothing is compacted, and the adapter exits 2 naming the path."""

    def __init__(self, message):
        super().__init__(EXIT_REFUSED, "refusing to serve: " + message)


class LedgerError(StateError):
    pass


class StateIOError(StateError):
    """The I/O kind of StateError (a read or write that failed), as distinct from a refusal on what the
    state says: a caller may retry the one, never the other."""


class StateWriteError(StateIOError):
    """A durable write to a claim or a publication that failed: a serving loop stops on it (exit 2) instead
    of parking the task, which it does for a read that failed (a plain StateIOError)."""


class LedgerIOError(LedgerError, StateIOError):
    pass


class RenamedNotDurable(OSError):
    """write_durable after its rename: the new contents are in place, their durability unknown."""


class PublishedNotDurable(StateIOError):
    """A publication whose rename landed (the result is visible) but whose directory fsync or read-back
    failed: a retry that finds the result settles on it; never "did not commit"."""


class StateCommitted(StateError):
    """A store write whose rename landed: the state changed, so the step counts as committed."""


def _log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


def fsync_dir(path):
    if os.name == "nt":  # pragma: no cover - directories cannot be opened for fsync there
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_durable(path, data):
    """Replace `path` with `data` atomically and durably: unique temp, fsync, rename, fsync dir."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(".%s.%d.%s.tmp" % (path.name, os.getpid(), uuid.uuid4().hex[:8]))
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)
    try:
        fsync_dir(path.parent)
    except OSError as e:
        raise RenamedNotDurable(e.errno, "%s was replaced, then its directory fsync failed (%s)" % (path, e.strerror or e))


def append_durable(path, line):
    """Append all of `line` (a short write continues where it stopped) and fsync. A file this call
    creates is made durable first (created, fsynced, its directory fsynced), then written."""
    created = not os.path.exists(path)
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o644)
    try:
        if created:
            os.fsync(fd)
            fsync_dir(os.path.dirname(str(path)) or ".")
        data = line.encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _lstat(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _dead_or_unobserved(pid, start=None):
    """The default liveness, fail-closed: a pid that cannot be signalled is dead (False); any other
    is unobserved (None), never live. An adapter injects the real tri-state through `lineage`."""
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError, TypeError, ValueError):
        return None
    return None


# ── startup refusals ─────────────────────────────────────────────────────────

def _first_entry(root, rel=""):
    """The first path under `root` (relative), descending into non-empty directories."""
    for name in sorted(os.listdir(os.path.join(root, rel) if rel else root)):
        path = os.path.join(rel, name) if rel else name
        if os.path.isdir(os.path.join(root, path)) and not os.path.islink(os.path.join(root, path)):
            return _first_entry(root, path) or path
        return path
    return None


def _init_step(name):
    """A test seam between the first-init steps; production does nothing here."""


def _is_type(path, want):
    """lstat, never following a link: `want` is "file" (regular) or "dir"."""
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    return _stat.S_ISREG(mode) if want == "file" else _stat.S_ISDIR(mode)


def bridge_entry(work, rel):
    """Whether `rel` under the work root is the gateway bridge's by its layout: state/locks holding
    only its lock files, state/hitl holding only its stores, logs holding only its log."""
    path = os.path.join(work, rel)
    kinds = {os.path.join("state", "locks"): (BRIDGE_LOCKS, "file"),
             os.path.join("state", "hitl"): (BRIDGE_HITL, "dir"), "logs": (BRIDGE_LOGS, "file")}
    if rel not in kinds or not _is_type(path, "dir"):
        return False
    pattern, kind = kinds[rel]
    return all(pattern.fullmatch(n) and _is_type(os.path.join(path, n), kind) for n in os.listdir(path))


def _state_entry_refusal(work):
    """No marker yet: the work dir may hold the bridge's tasks/ and results/ (and its own entries,
    bridge_entry) and an interrupted init's own state/ (an empty ledger, an empty claims/, one marker temp)."""
    format_temps = 0
    for name in sorted(os.listdir(work)):
        if name in ("tasks", "results") and _is_type(os.path.join(work, name), "dir") or bridge_entry(work, name):
            continue
        if name != "state" or not _is_type(os.path.join(work, name), "dir"):
            path = os.path.join(work, name)
            return (_first_entry(work, name) if _is_type(path, "dir") else None) or name
        for inner in sorted(os.listdir(os.path.join(work, "state"))):
            path = os.path.join(work, "state", inner)
            if inner == "settled-ids" and _is_type(path, "file") and not os.lstat(path).st_size:
                continue
            if inner == "claims" and _is_type(path, "dir") and not os.listdir(path):
                continue
            if bridge_entry(work, os.path.join("state", inner)):
                continue
            if FORMAT_TEMP.fullmatch(inner) and _is_type(path, "file") and format_temps == 0:
                format_temps += 1
                continue
            return (_first_entry(work, os.path.join("state", inner)) if os.path.isdir(path) else None) \
                or os.path.join("state", inner)
    return None


def _first_init(work):
    """One durable transaction: ledger, then claims/, then the marker last; then the directories."""
    state = os.path.join(work, "state")
    for d in ("tasks", "results", "state"):
        os.makedirs(os.path.join(work, d), exist_ok=True)
    _init_step("dirs")
    ledger, claims = os.path.join(state, "settled-ids"), os.path.join(state, "claims")
    if not os.path.exists(ledger):
        SettledIds.create(ledger)
    _init_step("ledger")
    if not os.path.isdir(claims):
        os.mkdir(claims)
    fsync_dir(claims)
    fsync_dir(state)
    _init_step("claims")
    for name in os.listdir(state):
        if FORMAT_TEMP.fullmatch(name):
            os.unlink(os.path.join(state, name))
    tmp = os.path.join(state, ".format.%d.%s.tmp" % (os.getpid(), uuid.uuid4().hex[:6]))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o644)
    try:
        os.write(fd, (FORMAT + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    _init_step("format-temp")
    os.replace(tmp, os.path.join(state, "format"))
    fsync_dir(state)
    _init_step("format")
    for d in (state, work, os.path.dirname(os.path.abspath(work))):
        fsync_dir(d)
    _init_step("done")


def prepare_work_dir(work, check_only=False):
    """None when `work` may be served (after completing a first init); else the refusal line.
    `check_only` decides the same without writing anything (no first init, no temp sweep)."""
    state = os.path.join(work, "state")
    marker = os.path.join(state, "format")
    try:
        if not os.path.lexists(marker):
            if check_only and not os.path.lexists(work):
                return None
            entry = _state_entry_refusal(work)
            if entry:
                return ("refusing to start: %s is not a fresh work dir for this adapter (it holds %s); "
                        "this adapter serves only a new, dedicated work dir" % (work, entry))
            if not check_only:
                _first_init(work)
            return None
        with open(marker, encoding="utf-8") as f:
            found = f.read().strip()
        if found not in FORMATS:
            return "refusing to start: %s says %r, this binary serves %r" % (marker, found, sorted(FORMATS))
        if not _is_type(state, "dir"):
            return "refusing to start: %s is not a directory" % state
        for name, kind in (("settled-ids", "file"), ("claims", "dir")):
            path = os.path.join(state, name)
            if not os.path.lexists(path):
                return "refusing to start: %s is marked but %s is missing" % (state, name)
            if not _is_type(path, kind):
                return "refusing to start: state/%s in %s is not a plain %s" % (name, state, kind)
        for name in sorted(os.listdir(state)):
            if name in STATE_ALLOWED:
                if name in STATE_OPTIONAL_FILES and not _is_type(os.path.join(state, name), "file"):
                    return "refusing to start: state/%s in %s is not a plain file" % (name, state)
                continue
            if name.startswith(".") and name.endswith(".tmp") and any(name[1:].startswith(a + ".")
                                                                      for a in STATE_ALLOWED - {"format"}):
                # A fresh temp is a live writer's, mid-replace (the launcher's record as hold starts).
                if not check_only and time.time() - os.lstat(os.path.join(state, name)).st_mtime > TEMP_SWEEP_AFTER:
                    os.unlink(os.path.join(state, name))
                continue
            if bridge_entry(work, os.path.join("state", name)):
                continue
            return "refusing to start: state/%s is not this adapter's state (in %s)" % (name, work)
    except OSError as e:
        return "%s %s: %s" % (UNCHECKED, work, e)
    return None


def enable_local_format(work):
    """Bump a prepared work dir to FORMAT_LOCAL, the fence under every tagged local claim: create and
    fsync the stable state/claims.lock inode and state/, then durably replace state/format. An older
    reader refuses the dir at startup from here on. Idempotent; StateError when the dir is not prepared."""
    state = os.path.join(work, "state")
    marker = os.path.join(state, "format")
    try:
        with open(marker, encoding="utf-8") as f:
            found = f.read().strip()
    except OSError as e:
        raise StateError("%s is not a prepared work dir (%s)" % (work, e))
    if found not in FORMATS:
        raise StateError("%s says %r" % (marker, found))
    try:
        fd = os.open(os.path.join(state, "claims.lock"), os.O_WRONLY | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o644)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        fsync_dir(state)
        if found != FORMAT_LOCAL:
            write_durable(marker, (FORMAT_LOCAL + "\n").encode("utf-8"))
    except OSError as e:
        raise _state_io("write", marker, e)


def probe_moves(directory):
    """Refusal line unless rename, hard link and the no-replace move publication uses all work in `directory`."""
    base = os.path.join(directory, ".probe-%d-%s" % (os.getpid(), uuid.uuid4().hex[:8]))
    try:
        with open(base, "wb") as f:
            f.write(b"probe")
        os.link(base, base + ".link")
        os.rename(base + ".link", base + ".moved")
        undelivered_quarantine.move_private(Path(base + ".moved"), Path(base + ".final"))
        return None
    except OSError as e:
        return "refusing to start: rename, hard link and no-replace rename must work in %s (%s)" % (directory, e)
    finally:
        for p in (base, base + ".link", base + ".moved", base + ".final"):
            if os.path.lexists(p):
                os.unlink(p)


# ── the generation classifier ────────────────────────────────────────────────

def _list_strict(path):
    """Names in a directory; [] when it does not exist; OSError for anything else."""
    try:
        return os.listdir(path)
    except FileNotFoundError:
        return []


def _read_strict(path):
    """Bytes of a file; None when it does not exist; OSError for anything else."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


def classify(results_dir, task_id):
    """Exactly one state for the task's result. Any lookup error is UNKNOWN, never ABSENT."""
    results = Path(results_dir)
    found = set()
    try:
        if not os.path.isdir(results):
            raise OSError(errno.ENOENT, "no results directory", str(results))
        names = os.listdir(results)
        live = _read_strict(results / ("%s.txt" % task_id))
        if live is not None:
            found.add(LIVE_READY if result_ready.ready_body_of(live) else LIVE_UNREADY)
        for name in names:
            claim = result_disposal.parse_claim(results / name)
            if claim is not None and claim.stem == task_id:
                found.add(CLAIMED)
        for d in [results / "archive"] + [results / n for n in names if n.startswith("archive-")]:
            if not os.path.isdir(d):
                continue
            with os.scandir(d) as entries:
                subdirs = [e.path for e in entries if e.is_dir()]
            for sub in subdirs:
                os.listdir(sub)
        for candidate in local_task_protocol.iter_result_candidates(results, task_id):
            if candidate == results / ("%s.txt" % task_id):
                continue
            data = _read_strict(candidate)
            found.add(ARCHIVED if data is not None and result_ready.ready_body_of(data) else UNKNOWN)
        _list_strict(undelivered_quarantine.quarantine_dir(results))
        if undelivered_quarantine.find_quarantined(results, task_id):
            found.add(QUARANTINED)
    except OSError:
        return UNKNOWN
    for state in PRECEDENCE:
        if state in found:
            return state
    return ABSENT


# ── the rate journal ─────────────────────────────────────────────────────────

CHARGES = frozenset(("queued", "unknown", "fired"))


class RateJournal:
    """Append-only attempt events; one charge per (task, attempt) whatever is replayed."""

    def __init__(self, path, clock=time.time):
        self.path, self.clock = str(path), clock
        self.events, self.ids = [], set()
        data = b""
        try:
            with open(self.path, "rb") as f:
                data = f.read()
            fsync_dir(os.path.dirname(self.path) or ".")    # a crash may have left the entry undurable
        except FileNotFoundError:
            pass
        lines = data.split(b"\n")
        torn = bool(lines[-1])                          # no final newline: the last append was cut
        for raw in lines[:-1]:
            try:
                ev = json.loads(raw.decode("utf-8"))
                self._take(ev["task"], int(ev["attempt"]), ev["kind"], float(ev["at"]))
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                continue
        stale = any(self.clock() - ev["at"] > JOURNAL_KEEP for ev in self.events)
        if torn or stale:
            self._rewrite()

    def _take(self, task, attempt, kind, at):
        eid = "%s:%d:%s" % (task, attempt, kind)
        if eid in self.ids:
            return False
        self.ids.add(eid)
        self.events.append({"id": eid, "task": task, "attempt": attempt, "kind": kind, "at": at})
        return True

    def _rewrite(self):
        keep = [ev for ev in self.events if self.clock() - ev["at"] <= JOURNAL_KEEP]
        self.events, self.ids = keep, {ev["id"] for ev in keep}
        write_durable(self.path, "".join(json.dumps(ev, sort_keys=True) + "\n" for ev in keep).encode("utf-8"))

    def append(self, task, attempt, kind, at=None):
        at = self.clock() if at is None else at
        if self._take(task, attempt, kind, at):
            append_durable(self.path, json.dumps(self.events[-1], sort_keys=True) + "\n")

    def charges(self):
        """{(task, attempt): time} for every counted attempt, at its latest charge event."""
        kinds, times = {}, {}
        for ev in self.events:
            key = (ev["task"], ev["attempt"])
            kinds.setdefault(key, set()).add(ev["kind"])
            if ev["kind"] in CHARGES:
                times[key] = max(times.get(key, ev["at"]), ev["at"])
        return {k: t for k, t in times.items() if "unsent" not in kinds[k]}

    def charged(self, task, attempt):
        return (task, attempt) in self.charges()

    def times(self):
        now = self.clock()
        return sorted(t for t in self.charges().values() if 0 <= now - t < HOUR)

    def wait_seconds(self, per_hour):
        times = self.times()
        if len(times) < per_hour:
            return 0.0
        return max(times[0] + HOUR - self.clock(), 0.0)


# ── settled ids ──────────────────────────────────────────────────────────────

def _fsync_file(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SettledIds:
    """Append-only, one validated id per newline-terminated record; never rewritten. A torn tail (an
    unterminated suffix: what a short append leaves) is dropped under the ledger's flock, on open and
    before every append; any malformed newline-terminated record fails closed (LedgerError). `ids` is
    what the file shows (enough never to call an id again); `durable` only what a successful fsync of
    the file and its directory, by this instance, covered: what a settlement may count."""

    def __init__(self, path, required=False):
        self.path = str(path)
        synced = False
        try:
            with open(self.path, "rb") as f:
                data = f.read()
                try:
                    os.fsync(f.fileno())                # what this instance read is durable only once this succeeds
                    synced = True
                except OSError:
                    pass
        except FileNotFoundError:
            if required:
                raise LedgerError("%s is missing" % self.path)
            data = b""
        except OSError as e:
            raise LedgerIOError("%s cannot be read (%s)" % (self.path, e))
        self.ids, keep = self._parse(data)
        read = set(self.ids)                            # only lines wholly within the bytes the fsync above covered
        if keep < len(data):
            try:
                fd = os.open(self.path, os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
                try:
                    self.ids = self._repair(fd)
                finally:
                    os.close(fd)
            except OSError as e:
                raise LedgerIOError("%s cannot be repaired (%s)" % (self.path, e))
        self.durable = set()
        if read and synced:
            try:
                fsync_dir(os.path.dirname(self.path))
                self.durable = read                     # never what a repair re-read later: no fsync of ours covered it
            except OSError:
                pass                                    # visible, not yet durable: add() syncs before it counts

    def _sync_file_and_dir(self):
        _fsync_file(self.path)
        fsync_dir(os.path.dirname(self.path))

    def _parse(self, data):
        """(ids, the byte length of the whole records kept): an unterminated tail is not kept."""
        ids, offset = set(), 0
        for raw in data.split(b"\n")[:-1]:
            try:
                tid = raw.decode("utf-8")
            except UnicodeDecodeError:
                tid = ""
            if not local_task_protocol.valid_task_id(tid):
                raise LedgerError("%s has a malformed record at byte %d" % (self.path, offset))
            ids.add(tid)
            offset += len(raw) + 1
        return ids, offset

    def _repair(self, fd):
        """Under the ledger's flock: the torn tail truncated away and the cut made durable; the ids held."""
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        data = os.pread(fd, os.fstat(fd).st_size, 0)
        ids, keep = self._parse(data)
        if keep < len(data):
            os.ftruncate(fd, keep)
            _fsync_file(self.path)
        return ids

    @staticmethod
    def create(path):
        """An empty ledger, durable: the file and its directory are fsynced."""
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0), 0o644)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        fsync_dir(os.path.dirname(str(path)))

    def sync(self):
        """fsync the ledger as it stands; LedgerError when that fails."""
        try:
            _fsync_file(self.path)
        except FileNotFoundError:
            return
        except OSError as e:
            raise LedgerIOError("cannot fsync %s (%s)" % (self.path, e))

    def __contains__(self, task_id):
        return task_id in self.ids

    def add(self, task_id):
        """Append, fsync, and only then count it durable. Any failure raises LedgerError. Repair-read-check-append
        is one critical section under the ledger's own flock, so instances loaded earlier never append an id twice
        and never append after a torn tail. An id already visible but not yet durable is synced, never trusted."""
        if task_id in self.durable:
            return
        if not local_task_protocol.valid_task_id(task_id):
            raise LedgerError("%r is not a task id; not appended to %s" % (task_id, self.path))
        data = (task_id + "\n").encode("utf-8")
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o644)
            try:
                if task_id not in self._repair(fd):
                    # One write of the whole record; a short one is torn, cut by the next repair and re-appended.
                    if os.write(fd, data) != len(data):
                        raise OSError(errno.EIO, "a short write of a settled id")
                    self.ids.add(task_id)
            finally:
                os.close(fd)
            self._sync_file_and_dir()
        except OSError as e:
            raise LedgerIOError("cannot append to %s (%s)" % (self.path, e))
        self.ids.add(task_id)
        self.durable.add(task_id)


# ── claims ───────────────────────────────────────────────────────────────────

def _state_io(what, path, e):
    kind = StateIOError if what in ("read", "list") else StateWriteError
    return kind("cannot %s %s (%s)" % (what, path, e))


class ClaimStore:
    """state/claims/<task_id>/{claim.json,body}. An I/O error is a StateError; a record that reads
    but does not parse is ClaimUnreadable (that task parks)."""

    def __init__(self, root):
        self.root = Path(root)

    def dir(self, task_id):
        return self.root / task_id

    def load(self, task_id):
        path = self.dir(task_id) / "claim.json"
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            return None
        except OSError as e:
            raise _state_io("read", path, e)
        try:
            rec = json.loads(text)
        except ValueError as e:
            raise ClaimUnreadable("%s: %s" % (path, e))
        if not isinstance(rec, dict) or rec.get("task_id") != task_id:
            raise ClaimUnreadable("%s: not a claim record" % path)
        return rec

    def save(self, rec):
        d = self.dir(rec["task_id"])
        try:
            new = not d.is_dir()
            d.mkdir(parents=True, exist_ok=True)
            if new:
                fsync_dir(self.root)
            write_durable(d / "claim.json", json.dumps(rec, sort_keys=True).encode("utf-8"))
        except RenamedNotDurable as e:
            raise StateCommitted(str(e))
        except OSError as e:
            raise _state_io("write", d / "claim.json", e)

    def stage(self, task_id, body):
        try:
            write_durable(self.dir(task_id) / "body", body)
        except RenamedNotDurable as e:
            raise StateCommitted(str(e))
        except OSError as e:
            raise _state_io("write", self.dir(task_id) / "body", e)

    def staged(self, task_id):
        try:
            with open(self.dir(task_id) / "body", "rb") as f:
                return f.read()
        except OSError as e:
            raise _state_io("read", self.dir(task_id) / "body", e)

    def blob(self, task_id, name):
        """A committed turn result, or None when absent; StateError on any other read error."""
        try:
            return _read_strict(self.dir(task_id) / name)
        except OSError as e:
            raise _state_io("read", self.dir(task_id) / name, e)

    def unstage(self, task_id):
        try:
            os.unlink(self.dir(task_id) / "body")
        except FileNotFoundError:
            pass
        except OSError as e:
            raise _state_io("remove", self.dir(task_id) / "body", e)

    def remove(self, task_id):
        try:
            shutil.rmtree(self.dir(task_id))
            fsync_dir(self.root)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise _state_io("remove", self.dir(task_id), e)

    def ids(self):
        try:
            return sorted(n for n in os.listdir(self.root) if not n.startswith("."))
        except FileNotFoundError:
            return []
        except OSError as e:
            raise _state_io("list", self.root, e)


# ── the lifecycle ────────────────────────────────────────────────────────────

class Lifecycle:
    """Every durable step of one task's execution; the adapter calls the provider in between."""

    def __init__(self, work, clock=time.time, log=_log, *, identity=None, lineage=None):
        """`identity` is this writer's {pid, start} recorded on cloud claims; `lineage(pid, start)` the
        tri-state liveness (True live, False dead, None unobserved) that decides whether another
        process's PREPARED claim may be re-prepared. The default admits only a verified-dead pid."""
        self.work, self.clock, self.log = Path(work), clock, log
        self.identity = dict(identity) if identity else {"pid": os.getpid(), "start": None}
        self.lineage = lineage or _dead_or_unobserved
        self.inbox, self.results, self.state = self.work / "tasks", self.work / "results", self.work / "state"
        for d in (self.inbox, self.results, self.state):
            d.mkdir(parents=True, exist_ok=True)
        self.claims = ClaimStore(self.state / "claims")
        self.journal = RateJournal(self.state / "rate.jsonl", clock)
        self.settled = SettledIds(self.state / "settled-ids", required=(self.state / "format").exists())
        self.open_attempts = set()

    def classify(self, task_id):
        return classify(self.results, task_id)

    def decide(self, task_id):
        """(call | settle | park, state), taken under the disposal lock. An unreadable claim parks."""
        try:
            self.claims.load(task_id)
        except ClaimUnreadable as e:
            self.log("unreadable claim, parked ->", e)
            return "park", UNKNOWN
        try:
            with result_disposal.locked(self.results):
                state = self.classify(task_id)
        except OSError:
            return "park", UNKNOWN
        if state == ABSENT:
            return "call", state
        return ("settle", state) if state in TERMINAL else ("park", state)

    def _save(self, rec, **changes):
        rec.update(changes)
        rec["updated_at"] = self.clock()
        self.claims.save(rec)
        return rec

    def _format(self):
        try:
            with open(self.state / "format", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return None

    def _prepared_elsewhere(self, rec, mode, generation):
        """A PREPARED claim another writer still owns: its recorded cloud writer or local generation is
        live or unobserved. A legacy claim with neither re-prepares as today; our own does too."""
        if rec.get("claim_mode") == LOCAL:
            who = rec.get("generation")
            if not who or (mode == LOCAL and who == generation):
                return False
        else:
            who = rec.get("writer")
            if not who or who == self.identity:
                return False
        return self.lineage(who.get("pid"), who.get("start")) is not False

    def prepare(self, task_id, interrupted_body, *, mode=CLOUD, generation=None):
        """A new attempt, PREPARED. `interrupted_body` is the result a crash during the call gets. A
        local claim binds to its consumer `generation` {pid, start} and needs the FORMAT_LOCAL fence;
        the same generation re-claiming its own PREPARED attempt gets it back, nothing rewritten."""
        if mode not in (CLOUD, LOCAL):
            raise ValueError("claim_mode must be cloud or local")
        if mode == LOCAL:
            if not (isinstance(generation, dict) and generation.get("pid")):
                raise ValueError("a local claim needs its consumer generation {pid, start}")
            if self._format() != FORMAT_LOCAL:
                raise StateError("%s is not %s; local claims need the fence first" % (self.state / "format", FORMAT_LOCAL))
        rec = self.claims.load(task_id)
        if task_id in self.open_attempts or (rec and rec["phase"] not in CALLABLE):
            raise AttemptInProgress("%s: attempt %s is %s" % (task_id, rec and rec["attempt"], rec and rec["phase"]))
        if rec and rec["phase"] == PREPARED:
            if self._prepared_elsewhere(rec, mode, generation):
                raise AttemptInProgress("%s: attempt %s is PREPARED by a live or unobserved writer"
                                        % (task_id, rec["attempt"]))
            if mode == LOCAL and rec.get("claim_mode") == LOCAL and rec.get("generation") == generation:
                self.open_attempts.add(task_id)
                return rec["attempt"]
        attempt = rec["attempt"] + 1 if rec and rec["phase"] == NOT_DELIVERED else (rec or {}).get("attempt") or 1
        rec = {"schema": 1, "task_id": task_id, "attempt": attempt, "created_at": self.clock(),
               "claim_mode": mode, "claim_version": CLAIM_VERSION}
        if mode == LOCAL:
            rec["generation"] = {"pid": generation["pid"], "start": generation.get("start")}
        else:
            rec["writer"] = dict(self.identity)
        self._save(rec, phase=PREPARED, interrupted_body=interrupted_body)
        self.journal.append(task_id, attempt, "reserved")
        self.open_attempts.add(task_id)
        return attempt

    def task_present(self, task_path):
        return os.path.exists(task_path)

    def begin_call(self, task_id, attempt, task_path):
        """The final pre-call check. A task observed absent is never called (the claim is dropped);
        one observed present may be called, even if it is removed before CALLING is written."""
        if not self.task_present(task_path):
            self.log("cancelled before the call ->", task_id)
            self.journal.append(task_id, attempt, "unsent")
            self.claims.remove(task_id)
            self.open_attempts.discard(task_id)
            return False
        self.mark_calling(task_id, attempt)
        return True

    def mark_calling(self, task_id, attempt):
        try:
            rec = self.claims.load(task_id)
        except StateIOError:
            self.open_attempts.discard(task_id)         # CALLING was never written: the attempt may be prepared again
            raise
        self._save(rec, phase=CALLING, attempt=attempt)

    def _outcome_claim(self, task_id):
        """The claim an outcome is recorded on, after the provider was called: a read that fails here is part of
        that durable write (StateWriteError), so a serving loop stops and recovery answers the call at restart."""
        try:
            return self.claims.load(task_id)
        except StateWriteError:
            raise
        except StateIOError as e:
            raise StateWriteError(e.message[len("refusing to serve: "):]) from e

    def authorize_call(self, task_id, attempt, *, text_hash="", expected_phase=PREPARED):
        """One PREPARED -> CALLING transition per attempt: the one-use submit token (a random nonce
        stored on the claim with `text_hash`), or None when the claim is not `expected_phase` at this
        attempt, so a claim seen CALLING never authorizes a second submit."""
        rec = self.claims.load(task_id)
        if rec is None or rec["phase"] != expected_phase or rec["attempt"] != attempt:
            return None
        nonce = os.urandom(16).hex()
        self._save(rec, phase=CALLING, call={"nonce": nonce, "text_hash": text_hash, "at": self.clock()})
        return nonce

    def confirm_turn(self, task_id, turn_id, nonce, text_hash):
        """CALLING -> TURN_CONFIRMED, recording the turn id, only for the nonce and text hash that
        authorize_call recorded; anything else is TurnRefused with the claim untouched."""
        rec = self.claims.load(task_id)
        if rec is None or rec["phase"] != CALLING:
            raise TurnRefused("%s: not CALLING (%s)" % (task_id, rec and rec["phase"]))
        call = rec.get("call") or {}
        if not call.get("nonce") or nonce != call["nonce"] or text_hash != call.get("text_hash"):
            raise TurnRefused("%s: the nonce or text hash does not match the authorized call" % task_id)
        if not isinstance(turn_id, str) or not turn_id:
            raise TurnRefused("%s: a turn id is required" % task_id)
        now = self.clock()
        self.journal.append(task_id, rec["attempt"], "fired", now)
        self._save(rec, phase=TURN_CONFIRMED, turn_id=turn_id, outcome={"kind": "fired", "detail": "turn confirmed", "at": now})

    @staticmethod
    def _identity_of(rec, turn_id):
        return {"task_id": rec["task_id"], "attempt": rec["attempt"], "generation": rec.get("generation"),
                "turn_id": turn_id}

    @staticmethod
    def _blob_name(rec, identity):
        digest = _sha256(json.dumps(identity, sort_keys=True).encode("utf-8"))[:24]
        return "blob.%d.%s" % (rec["attempt"], digest), digest

    def _verified(self, task_id, descriptor):
        """The committed blob's bytes when its length and digest match the descriptor, else None."""
        data = self.claims.blob(task_id, descriptor["blob"])
        if data is None or len(data) != descriptor["length"] or _sha256(data) != descriptor["sha256"]:
            return None
        return data

    def stage_result(self, task_id, body, turn_id):
        """TURN_CONFIRMED -> RESULT_STAGED for the recorded turn, in the commit order: (1) the immutable
        descriptor {identity, length, sha256} saved on the claim, (2) the body in a private temp,
        fsynced, (3) os.link to the final name (atomic, no-replace), (4) the blob directory fsynced,
        (5) the final re-read and verified, (6) RESULT_STAGED. The caller holds claims.lock. staged |
        parked: an identical retry finishes; different bytes, a corrupt blob or a second candidate
        for the same identity park with the claim and the first bytes untouched."""
        rec = self.claims.load(task_id)
        if rec is None or rec.get("claim_mode") != LOCAL or rec.get("turn_id") != turn_id:
            self.log("stage refused: not this turn's local claim ->", task_id, rec and rec["phase"])
            return "parked"
        identity = self._identity_of(rec, turn_id)
        name, digest = self._blob_name(rec, identity)
        descriptor = {"identity": identity, "length": len(body), "sha256": _sha256(body), "blob": name}
        if rec["phase"] == RESULT_STAGED:
            return "staged" if rec.get("staged") == descriptor else "parked"
        if rec["phase"] != TURN_CONFIRMED:
            self.log("stage refused: claim is", rec["phase"], "->", task_id)
            return "parked"
        if rec.get("staged") not in (None, descriptor):
            self.log("a different result for the same turn; parked with the first ->", task_id)
            return "parked"
        if rec.get("staged") is None:
            rec = self._save(rec, staged=descriptor)
        d = self.claims.dir(task_id)
        tmp = d / (".%s.%d.%s.%d.%s.tmp" % (task_id, rec["attempt"], digest, os.getpid(), uuid.uuid4().hex[:8]))
        try:
            linked = self._commit_blob(tmp, d / name, body)
            if not linked and self._verified(task_id, descriptor) is None:
                self.log("the committed result does not match the descriptor; parked ->", task_id)
                return "parked"
            if not linked:
                fsync_dir(d)                            # the existing final is durable before the phase says so
            elif self._verified(task_id, descriptor) is None:
                self.log("the result read back does not verify; parked ->", task_id)
                return "parked"
        except OSError as e:
            self.log("staging parked ->", task_id, e)
            return "parked"
        self._save(rec, phase=RESULT_STAGED)
        self.open_attempts.discard(task_id)
        return "staged"

    def finish_staged(self, task_id):
        """A TURN_CONFIRMED claim that already recorded its staged descriptor completes the move with
        exactly the recorded body (its blob verifying), so what publishes is the answer that was
        accepted, never a second body. staged | parked (no descriptor, or no verifying blob)."""
        rec = self.claims.load(task_id)
        if rec is None or rec["phase"] != TURN_CONFIRMED or not rec.get("staged"):
            return "parked"
        body = self._verified(task_id, rec["staged"])
        if body is None:
            return "parked"
        return self.stage_result(task_id, body, rec.get("turn_id"))

    @staticmethod
    def _commit_blob(tmp, final, body):
        """True when this call linked the final; False when it already existed. The temp never
        outlives the call, and a final can never be partial."""
        try:
            with open(tmp, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, final)
            except FileExistsError:
                return False
            fsync_dir(final.parent)
            return True
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)

    def _staged_body(self, rec):
        """What publication writes: the verified blob of a local turn result, else the legacy body;
        None when the blob is missing or does not verify (parked)."""
        descriptor = rec.get("staged")
        if descriptor is None:
            return self.claims.staged(rec["task_id"])
        return self._verified(rec["task_id"], descriptor)

    def not_delivered(self, task_id, attempt, detail):
        self._save(self._outcome_claim(task_id), phase=NOT_DELIVERED, outcome={"kind": "unsent", "detail": detail})
        self.journal.append(task_id, attempt, "unsent")
        self.open_attempts.discard(task_id)

    def record_outcome(self, task_id, kind, body, detail="", url=None):
        """kind: queued (a receipt), unknown (may have been queued) or final (no call counted)."""
        rec = self._outcome_claim(task_id)
        phase = {"queued": CONFIRMED, "unknown": OUTCOME_UNKNOWN}.get(kind, rec["phase"] if rec else PREPARED)
        self.claims.stage(task_id, body.encode("utf-8"))
        rec = self._save(rec, phase=phase, outcome={"kind": kind, "detail": detail, "url": url, "at": self.clock()})
        if kind in ("queued", "unknown"):
            self.journal.append(task_id, rec["attempt"], kind, rec["outcome"]["at"])
        self._save(rec, phase=RESULT_STAGED)
        self.open_attempts.discard(task_id)

    def complete_without_call(self, task_id, body, reason):
        rec = {"schema": 1, "task_id": task_id, "attempt": 0, "created_at": self.clock()}
        self.claims.stage(task_id, body.encode("utf-8"))
        self._save(rec, phase=RESULT_STAGED, outcome={"kind": "final", "detail": reason, "at": self.clock()})
        return self.publish_and_settle(task_id)

    def settle_without_result(self, task_id, reason):
        rec = self.claims.load(task_id) or {"schema": 1, "task_id": task_id, "attempt": 0, "created_at": self.clock()}
        self.log("settled without a result ->", task_id, reason)
        self._save(rec, phase=SETTLED, settled_at=self.clock(), reason=reason)
        self.reconcile_settled(task_id)

    def _settle(self, rec):
        self._save(rec, phase=SETTLED, settled_at=self.clock())
        self.reconcile_settled(rec["task_id"])

    def reconcile_settled(self, task_id):
        """The one repair-then-check every reader that can report a claim settled or final runs first: a
        SETTLED claim's id appended to the settled ledger (idempotent) and its staged body removed, the steps
        _settle takes after the save. settlement_complete afterwards (False for a claim not SETTLED); any
        failure raises, as those steps do, so nothing reports final over an incomplete settlement."""
        rec = self.claims.load(task_id)
        if rec is None or rec["phase"] != SETTLED:
            return False
        if task_id not in self.settled:
            self.log("re-recording a settled id ->", task_id)
        # Always a durable ledger sync first (an id already visible included); only then is the body removed.
        self.settled.add(task_id)
        self.claims.unstage(task_id)
        return self.settlement_complete(task_id)

    def _reconcile_or_skip(self, task_id):
        """Recovery's repair of one SETTLED claim: an I/O failure skips this claim (logged, left for the next
        pass, never compacted meanwhile) instead of stopping the start. True when the settlement is complete."""
        try:
            return self.reconcile_settled(task_id)
        except StateIOError as e:
            self.log("settlement repair skipped, retried next pass ->", task_id, e)
            return False

    def settlement_complete(self, task_id):
        """Whether a SETTLED claim's settlement is whole: its id in the ledger and no staged body left. What
        every report of a settled or final claim requires; reconcile_settled is what makes it so."""
        rec = self.claims.load(task_id)
        return bool(rec and rec["phase"] == SETTLED and task_id in self.settled.durable
                    and not os.path.lexists(self.claims.dir(task_id) / "body"))

    def is_settled(self, task_id):
        """Whether a reader may treat the id as final: in the ledger and, while its claim is still SETTLED on
        disk, that settlement repaired and complete (reconcile_settled); a compacted claim is complete."""
        if task_id not in self.settled:
            return False
        rec = self.claims.load(task_id)
        if rec and rec["phase"] == SETTLED:
            return self.reconcile_settled(task_id)
        self.settled.add(task_id)                       # a compacted claim: its id synced before it counts
        return True

    def reconcile_unbound(self, now=None):
        """The SETTLED claims no generation owns (answered without a call): each settlement completed, and
        compacted once due, as generation recovery does for its own. Under claims.lock, by the caller."""
        now = self.clock() if now is None else now
        for task_id, rec in self._claims_readable().items():
            if not rec or rec.get("generation") or rec.get("claim_mode") not in (None, LOCAL) or rec["phase"] != SETTLED:
                continue
            if self._reconcile_or_skip(task_id) and now - rec.get("settled_at", now) > COMPACT_AFTER:
                self.claims.remove(task_id)

    def _holds_body(self, task_id, body):
        """Labels a settled publication won when some generation holds exactly our bytes."""
        try:
            paths = list(local_task_protocol.iter_result_candidates(self.results, task_id))
            paths += undelivered_quarantine.find_quarantined(self.results, task_id)
            return any(_read_strict(p) == body for p in paths)
        except OSError:
            return False

    def read_final(self, final):
        return _read_strict(final)

    def interrupt_turn(self, task_id, detail, now=None):
        """A TURN_CONFIRMED local claim answered with its interrupted body in place of any result it
        recorded: what recovery gives a dead generation's, and a live one whose Mod lost the accepted
        text. staged (RESULT_STAGED, for publish_and_settle) | parked (not such a claim)."""
        rec = self.claims.load(task_id)
        if rec is None or rec.get("claim_mode") != LOCAL or rec["phase"] != TURN_CONFIRMED:
            return "parked"
        now = self.clock() if now is None else now
        self.log("turn confirmed, no result; answered as interrupted ->", task_id)
        self.claims.stage(task_id, rec["interrupted_body"].encode("utf-8"))
        # One durable transition: a rename that lands leaves a publishable claim, never an intermediate phase.
        self._save(rec, phase=RESULT_STAGED, staged=None, outcome={"kind": "unknown", "detail": detail, "at": now})
        self.open_attempts.discard(task_id)
        return "staged"

    def resume_outcome(self, task_id):
        """A local claim committed at OUTCOME_UNKNOWN (its body durable, the move to RESULT_STAGED not yet
        saved) moved there now: what recovery does for it, and the live reclaim too, so a committed
        intermediate state is always resumed and never retired. staged | parked (not such a claim)."""
        rec = self.claims.load(task_id)
        if rec is None or rec.get("claim_mode") != LOCAL or rec["phase"] != OUTCOME_UNKNOWN:
            return "parked"
        self._save(rec, phase=RESULT_STAGED)
        return "staged"

    def finish_verified(self, task_id):
        """A TURN_CONFIRMED local claim whose recorded descriptor's blob verifies moved to RESULT_STAGED by
        the claim save alone: the blob is final and is never written again. finished | parked (no such
        claim, or no verifying blob). A save that fails raises, as every claim save does."""
        rec = self.claims.load(task_id)
        if rec is None or rec.get("claim_mode") != LOCAL or rec["phase"] != TURN_CONFIRMED:
            return "parked"
        descriptor = rec.get("staged")
        if descriptor is None or self._verified(task_id, descriptor) is None:
            return "parked"
        # The blob shares claim.json's directory, so this save's directory fsync makes its link durable too.
        self._save(rec, phase=RESULT_STAGED)
        self.open_attempts.discard(task_id)
        return "finished"

    def close_turn(self, task_id, detail, now=None):
        """The one decision for a TURN_CONFIRMED local claim nobody will stage again (a dead generation's,
        or a live one whose Mod lost it): a descriptor whose blob verifies is the answer, moved on by
        finish_verified with no blob write (finished); no descriptor, or a blob missing or not matching
        it, is answered as interrupted (interrupted), never a substituted body; a claim that is not such
        a turn is parked. Every outcome but parked leaves the claim RESULT_STAGED for publish_and_settle."""
        rec = self.claims.load(task_id)
        if rec is None or rec.get("claim_mode") != LOCAL or rec["phase"] != TURN_CONFIRMED:
            return "parked"
        descriptor = rec.get("staged")
        if descriptor is not None and self._verified(task_id, descriptor) is not None:
            return self.finish_verified(task_id)
        if descriptor is not None and self.claims.blob(task_id, descriptor["blob"]) is not None:
            self.log("a committed result does not match its descriptor; answered as interrupted ->", task_id)
            detail = "%s (the committed result does not match its descriptor)" % detail
        return "interrupted" if self.interrupt_turn(task_id, detail, now) == "staged" else "parked"

    def publish_and_settle(self, task_id, *, io_raises=False):
        """settled | parked: settled only for a claim this call published and settled, or one already
        SETTLED (the publisher is idempotent there); any phase it cannot advance is parked.
        Publication never replaces a result and never repeats a won rename. An I/O failure of the
        publication parks, or with `io_raises` is the StateIOError a retrying caller distinguishes."""
        rec = self.claims.load(task_id)
        if rec is None:
            return "parked"
        if rec["phase"] == RESULT_STAGED:
            try:
                with result_disposal.locked(self.results):
                    published = self._publish(rec)
            except OSError as e:
                self.log("publication parked ->", task_id, e)
                if io_raises and isinstance(e, RenamedNotDurable):
                    raise PublishedNotDurable(str(e))
                if io_raises:
                    raise _state_io("publish", self.results / ("%s.txt" % task_id), e)
                return "parked"
            if published is None:
                return "parked"
            rec = self._save(rec, phase=PUBLISHED, published=published)
        if rec["phase"] == PUBLISHED:
            self._settle(rec)
        if rec["phase"] != SETTLED:
            return "parked"
        # Settled is reported only once the settlement is whole, also for a claim found already SETTLED.
        self.reconcile_settled(task_id)
        return "settled"

    def _publish(self, rec):
        task_id, attempt = rec["task_id"], rec["attempt"]
        body = self._staged_body(rec)
        if body is None:
            self.log("no verified staged body; parked ->", task_id)
            return None
        state = self.classify(task_id)
        if state in PARKED:
            return None
        if state in TERMINAL:
            self.log("a result already exists, none added ->", task_id, state)
            return {"won": self._holds_body(task_id, body), "state": state, "at": self.clock()}
        final = self.results / ("%s.txt" % task_id)
        sibling = self.results / (".%s.txt.publish.%d" % (task_id, attempt))
        tmp = self.results / (".%s.publish.%d.%d.%s.tmp" % (task_id, attempt, os.getpid(), uuid.uuid4().hex[:8]))
        try:
            with open(tmp, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            if _read_strict(tmp) != body:
                raise OSError(errno.EIO, "the publication copy does not match the stage", str(tmp))
            os.replace(tmp, sibling)
        finally:
            if os.path.lexists(tmp):
                os.unlink(tmp)
        fsync_dir(self.results)
        try:
            undelivered_quarantine.move_private(sibling, final)
        except FileExistsError as e:
            if e.errno not in (None, errno.EEXIST):
                raise
            state = self.classify(task_id)
            if state not in TERMINAL:
                self.log("the final name is taken by an unfinished result; parked ->", task_id, state)
                return None
            os.unlink(sibling)
            fsync_dir(self.results)
            return {"won": self._holds_body(task_id, body), "state": state, "at": self.clock()}
        try:
            fsync_dir(self.results)
            seen = self.read_final(final)
            state = LIVE_READY if seen is not None else self.classify(task_id)
        except OSError as e:
            raise RenamedNotDurable("%s landed, then %s" % (final, e))
        if seen is not None and seen != body:
            self.log("the published result differs from the stage ->", task_id)
        return {"won": True, "state": state, "at": self.clock()}

    def recover(self, now=None, *, generation=None, proof=None):
        """Settle what a dead predecessor left: run once, after the lock, before serving. Without a
        generation this is the cloud path, which refuses every local claim (logged as foreign) before
        touching the publisher, the ledger or any body. With `generation` {pid, start} and
        `proof=False` (that generation verified dead by the caller) it handles the local claims of
        exactly that generation: TURN_CONFIRMED with a verified blob publishes that body once, without
        one answers "turn confirmed, no result", with a mismatched blob answers as interrupted (close_turn)."""
        now = self.clock() if now is None else now
        if generation is not None:
            if proof is not False:
                raise ValueError("generation recovery needs proof=False: the generation verified dead")
            return self._recover_generation(now, generation)
        recs = self._claims_readable()
        for task_id, rec in recs.items():
            if rec and rec.get("claim_mode") == LOCAL:
                self.log("foreign claim (local), left alone ->", task_id)
                continue
            if rec and rec["phase"] == SETTLED:
                self._reconcile_or_skip(task_id)
        self.settled.sync()                             # compaction below never runs ahead of durability
        for name in os.listdir(self.results):
            if name.startswith(".task-") and (".publish." in name):
                os.unlink(self.results / name)
        for task_id, rec in recs.items():
            if rec is None or rec.get("claim_mode") == LOCAL:
                continue
            phase = rec["phase"]
            if phase == SETTLED:
                # Compaction only after the settlement's own durable ledger sync: the id outlives the claim.
                if self.settlement_complete(task_id) and now - rec.get("settled_at", now) > COMPACT_AFTER:
                    self.claims.remove(task_id)
                continue
            task_path = self.inbox / ("%s.txt" % task_id)
            if phase in CALLABLE:
                if not task_path.exists():
                    self.journal.append(task_id, rec["attempt"], "unsent")
                    self.settle_without_result(task_id, "the task was removed before the call")
                elif self.decide(task_id)[0] == "settle":
                    self.settle_without_result(task_id, "a result already exists")
                continue
            if phase == CALLING:
                self.log("the call may have queued; counted at restart, never resent ->", task_id)
                self.journal.append(task_id, rec["attempt"], "unknown", now)
                self.claims.stage(task_id, rec["interrupted_body"].encode("utf-8"))
                rec = self._save(rec, phase=OUTCOME_UNKNOWN, outcome={"kind": "unknown", "detail": "interrupted",
                                                                      "at": now})
                rec = self._save(rec, phase=RESULT_STAGED)
            elif phase in (CONFIRMED, OUTCOME_UNKNOWN, RESULT_STAGED):
                kind = (rec.get("outcome") or {}).get("kind")
                if kind in ("queued", "unknown"):
                    self.journal.append(task_id, rec["attempt"], kind, rec["outcome"]["at"])
                if phase != RESULT_STAGED:
                    rec = self._save(rec, phase=RESULT_STAGED)
            elif phase != PUBLISHED:
                self.log("a phase this reader does not know, left alone ->", task_id, phase)
                continue
            self.publish_and_settle(task_id)

    def _claims_readable(self):
        recs = {}
        for task_id in self.claims.ids():
            try:
                recs[task_id] = self.claims.load(task_id)
            except ClaimUnreadable as e:
                self.log("unreadable claim, parked ->", e)
        return recs

    def _sweep_temps(self, task_id, now):
        """Private temps of a dead writer, or older than the stop bound, are removed (never a final),
        each removal made durable by a directory fsync. Under claims.lock, by the caller."""
        d = self.claims.dir(task_id)
        try:
            names = os.listdir(d)
        except OSError:
            return
        for name in names:
            m = BLOB_TEMP.match(name)
            if m is None:
                continue
            st = _lstat(d / name)
            if st is None:
                continue
            if self.lineage(int(m.group(4)), None) is False or now - st.st_mtime > TEMP_SWEEP_AFTER:
                try:
                    os.unlink(d / name)
                    fsync_dir(d)
                except OSError as e:
                    self.log("a leftover temp could not be swept ->", task_id, e)

    def _recover_generation(self, now, generation):
        recs = {t: r for t, r in self._claims_readable().items()
                if r and r.get("claim_mode") == LOCAL and r.get("generation") == generation}
        for task_id, rec in recs.items():
            if rec["phase"] == SETTLED:
                self._reconcile_or_skip(task_id)
        self.settled.sync()
        for task_id, rec in recs.items():
            phase = rec["phase"]
            if phase == SETTLED:
                # Compaction only after the settlement's own durable ledger sync: the id outlives the claim.
                if self.settlement_complete(task_id) and now - rec.get("settled_at", now) > COMPACT_AFTER:
                    self.claims.remove(task_id)
                continue
            self._sweep_temps(task_id, now)
            task_path = self.inbox / ("%s.txt" % task_id)
            if phase in CALLABLE:
                if not task_path.exists():
                    self.journal.append(task_id, rec["attempt"], "unsent")
                    self.settle_without_result(task_id, "the task was removed before the call")
                elif self.decide(task_id)[0] == "settle":
                    self.settle_without_result(task_id, "a result already exists")
                continue
            if phase == CALLING:
                self.log("the submit may have happened; counted, never resent ->", task_id)
                self.journal.append(task_id, rec["attempt"], "unknown", now)
                self.claims.stage(task_id, rec["interrupted_body"].encode("utf-8"))
                rec = self._save(rec, phase=OUTCOME_UNKNOWN, outcome={"kind": "unknown", "detail": "interrupted",
                                                                      "at": now})
                rec = self._save(rec, phase=RESULT_STAGED)
            elif phase == TURN_CONFIRMED:
                if self.close_turn(task_id, "turn confirmed, no result", now) == "parked":
                    continue
            elif phase in (OUTCOME_UNKNOWN, RESULT_STAGED):
                if phase != RESULT_STAGED:
                    self.resume_outcome(task_id)
            elif phase != PUBLISHED:
                self.log("a phase this reader does not know, left alone ->", task_id, phase)
                continue
            self.publish_and_settle(task_id)
