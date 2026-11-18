# Verbatim src/task_lifecycle.py at commit 6b699dd2 (before the claude-local adapter): a fixture for the rolling pins.
"""Task execution lifecycle for an adapter that serves one work dir and calls a provider per task.

One owner for what such an adapter must get right across crashes, so no adapter re-derives it:

- The generation classifier. `classify` answers, strictly, which generation of a task's result
  exists: LIVE_READY, LIVE_UNREADY, CLAIMED (a disposal claim), ARCHIVED, QUARANTINED, ABSENT, or
  UNKNOWN when any lookup failed. Precedence: UNKNOWN > LIVE_READY > ARCHIVED > QUARANTINED >
  CLAIMED > LIVE_UNREADY > ABSENT. Terminal states settle; CLAIMED, LIVE_UNREADY and UNKNOWN park.
- Per-task claims under `state/claims/<task_id>/`, one attempt at a time:
  PREPARED -> CALLING -> {NOT_DELIVERED | CONFIRMED | OUTCOME_UNKNOWN} -> RESULT_STAGED ->
  PUBLISHED -> SETTLED. CALLING is durable before the provider is called.
- Publication: the staged body goes to a unique temp in `results/`, is read back, renamed to a
  private sibling, then renamed no-replace to the final name, with directory fsyncs, all under
  `results/.disposal.lock`. A successful rename is the win; a final file missing afterwards (a drain
  took it) still reads as won, never as a retry.
- The rate journal `state/rate.jsonl`: one line per attempt event with a stable id; a reducer keeps
  one charge per attempt. And `state/settled-ids`: append-only, one validated id per record, written
  after the claim says SETTLED and before the id counts as settled, so a settled id is never called again.
- The fresh-dir rule: a first start needs an empty work dir and writes `state/format`; later starts
  need that marker and only this owner's names in `state/`.

Delivery and claims of published results belong to the bridge's outbox; nothing here re-implements
them. Stdlib plus the package's result lookup modules only.
"""
from __future__ import annotations

import errno
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
PUBLISHED, SETTLED = "PUBLISHED", "SETTLED"
CALLABLE = frozenset((PREPARED, NOT_DELIVERED))

FORMAT = "cloud-session/1"
STATE_ALLOWED = frozenset(("format", "claims", "settled-ids", "rate.jsonl", "done", "collaborator-turns.json"))
# Optional records the adapters rewrite in place: when present, each must be a plain file.
STATE_OPTIONAL_FILES = frozenset(("collaborator-turns.json",))
FORMAT_TEMP = re.compile(r"\.format\.\d+\.[0-9a-f]{6}\.tmp")


class StartupRefused(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class ClaimUnreadable(Exception):
    pass


class AttemptInProgress(Exception):
    """prepare() for a task whose attempt has not reached an outcome."""


class StateError(StartupRefused):
    """A claim or the ledger could not be read or written: the claim stays as it was, no work is
    admitted, nothing is compacted, and the adapter exits 2 naming the path."""

    def __init__(self, message):
        super().__init__(EXIT_REFUSED, "refusing to serve: " + message)


class LedgerError(StateError):
    pass


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
    fsync_dir(path.parent)


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


def _state_entry_refusal(work):
    """No marker yet: the work dir may hold the bridge's tasks/ and results/ and an interrupted
    init's own state/ (an empty ledger, an empty claims/, one marker temp)."""
    format_temps = 0
    for name in sorted(os.listdir(work)):
        if name in ("tasks", "results") and _is_type(os.path.join(work, name), "dir"):
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


def prepare_work_dir(work):
    """None when `work` may be served (after completing a first init); else the refusal line."""
    state = os.path.join(work, "state")
    marker = os.path.join(state, "format")
    try:
        if not os.path.lexists(marker):
            entry = _state_entry_refusal(work)
            if entry:
                return ("refusing to start: %s is not a fresh work dir for this adapter (it holds %s); "
                        "this adapter serves only a new, dedicated work dir" % (work, entry))
            _first_init(work)
            return None
        with open(marker, encoding="utf-8") as f:
            found = f.read().strip()
        if found != FORMAT:
            return "refusing to start: %s says %r, this binary serves %r" % (marker, found, FORMAT)
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
                os.unlink(os.path.join(state, name))
                continue
            return "refusing to start: state/%s is not this adapter's state (in %s)" % (name, work)
    except OSError as e:
        return "refusing to start: cannot check %s: %s" % (work, e)
    return None


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
    """Append-only, one validated id per newline-terminated record; never rewritten. On open an
    unterminated suffix is truncated away; anything else wrong fails closed (LedgerError)."""

    def __init__(self, path, required=False):
        self.path = str(path)
        try:
            with open(self.path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            if required:
                raise LedgerError("%s is missing" % self.path)
            data = b""
        except OSError as e:
            raise LedgerError("%s cannot be read (%s)" % (self.path, e))
        self.ids, offset = set(), 0
        records = data.split(b"\n")
        for raw in records[:-1]:
            try:
                tid = raw.decode("utf-8")
            except UnicodeDecodeError:
                tid = ""
            if not local_task_protocol.valid_task_id(tid):
                raise LedgerError("%s has a malformed record at byte %d" % (self.path, offset))
            self.ids.add(tid)
            offset += len(raw) + 1
        if records[-1]:
            try:
                os.truncate(self.path, offset)
                _fsync_file(self.path)
            except OSError as e:
                raise LedgerError("%s cannot be repaired (%s)" % (self.path, e))

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
            raise LedgerError("cannot fsync %s (%s)" % (self.path, e))

    def __contains__(self, task_id):
        return task_id in self.ids

    def add(self, task_id):
        """Append, fsync, and only then remember. Any failure raises LedgerError."""
        if task_id in self.ids:
            return
        if not local_task_protocol.valid_task_id(task_id):
            raise LedgerError("%r is not a task id; not appended to %s" % (task_id, self.path))
        data = (task_id + "\n").encode("utf-8")
        try:
            created = not os.path.exists(self.path)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o644)
            try:
                while data:
                    data = data[os.write(fd, data):]
                os.fsync(fd)
            finally:
                os.close(fd)
            if created:
                fsync_dir(os.path.dirname(self.path))
        except OSError as e:
            raise LedgerError("cannot append to %s (%s)" % (self.path, e))
        self.ids.add(task_id)


# ── claims ───────────────────────────────────────────────────────────────────

def _state_io(what, path, e):
    return StateError("cannot %s %s (%s)" % (what, path, e))


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
        except OSError as e:
            raise _state_io("write", d / "claim.json", e)

    def stage(self, task_id, body):
        try:
            write_durable(self.dir(task_id) / "body", body)
        except OSError as e:
            raise _state_io("write", self.dir(task_id) / "body", e)

    def staged(self, task_id):
        try:
            with open(self.dir(task_id) / "body", "rb") as f:
                return f.read()
        except OSError as e:
            raise _state_io("read", self.dir(task_id) / "body", e)

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

    def __init__(self, work, clock=time.time, log=_log):
        self.work, self.clock, self.log = Path(work), clock, log
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

    def prepare(self, task_id, interrupted_body):
        """A new attempt, PREPARED. `interrupted_body` is the result a crash during the call gets."""
        rec = self.claims.load(task_id)
        if task_id in self.open_attempts or (rec and rec["phase"] not in CALLABLE):
            raise AttemptInProgress("%s: attempt %s is %s" % (task_id, rec and rec["attempt"], rec and rec["phase"]))
        attempt = rec["attempt"] + 1 if rec and rec["phase"] == NOT_DELIVERED else (rec or {}).get("attempt") or 1
        rec = {"schema": 1, "task_id": task_id, "attempt": attempt, "created_at": self.clock()}
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
        self._save(self.claims.load(task_id), phase=CALLING, attempt=attempt)

    def not_delivered(self, task_id, attempt, detail):
        self._save(self.claims.load(task_id), phase=NOT_DELIVERED, outcome={"kind": "unsent", "detail": detail})
        self.journal.append(task_id, attempt, "unsent")
        self.open_attempts.discard(task_id)

    def record_outcome(self, task_id, kind, body, detail="", url=None):
        """kind: queued (a receipt), unknown (may have been queued) or final (no call counted)."""
        rec = self.claims.load(task_id)
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
        self.settled.add(task_id)

    def _settle(self, rec):
        self._save(rec, phase=SETTLED, settled_at=self.clock())
        self.settled.add(rec["task_id"])
        self.claims.unstage(rec["task_id"])

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

    def publish_and_settle(self, task_id):
        """settled | parked. Publication never replaces a result and never repeats a won rename."""
        rec = self.claims.load(task_id)
        if rec["phase"] == RESULT_STAGED:
            try:
                with result_disposal.locked(self.results):
                    published = self._publish(rec)
            except OSError as e:
                self.log("publication parked ->", task_id, e)
                return "parked"
            if published is None:
                return "parked"
            rec = self._save(rec, phase=PUBLISHED, published=published)
        if rec["phase"] == PUBLISHED:
            self._settle(rec)
        return "settled"

    def _publish(self, rec):
        task_id, attempt = rec["task_id"], rec["attempt"]
        body = self.claims.staged(task_id)
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
        fsync_dir(self.results)
        seen = self.read_final(final)
        if seen is not None and seen != body:
            self.log("the published result differs from the stage ->", task_id)
        return {"won": True, "state": LIVE_READY if seen is not None else self.classify(task_id), "at": self.clock()}

    def recover(self, now=None):
        """Settle what a dead predecessor left: run once, after the lock, before serving."""
        now = self.clock() if now is None else now
        ids = self.claims.ids()
        recs = {}
        for task_id in ids:
            try:
                recs[task_id] = self.claims.load(task_id)
            except ClaimUnreadable as e:
                self.log("unreadable claim, parked ->", e)
        for task_id, rec in recs.items():
            if rec and rec["phase"] == SETTLED and task_id not in self.settled:
                self.log("re-recording a settled id ->", task_id)
                self.settled.add(task_id)
        self.settled.sync()                             # compaction below never runs ahead of durability
        for name in os.listdir(self.results):
            if name.startswith(".task-") and (".publish." in name):
                os.unlink(self.results / name)
        for task_id, rec in recs.items():
            if rec is None:
                continue
            phase = rec["phase"]
            if phase == SETTLED:
                if now - rec.get("settled_at", now) > COMPACT_AFTER:
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
            self.publish_and_settle(task_id)
