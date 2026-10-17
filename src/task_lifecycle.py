"""Task execution lifecycle for an adapter that serves one work dir and calls a provider per task.

API stub: every function raises NotImplementedError until the implementation lands."""
from __future__ import annotations
import errno
import importlib
import json
import os
import shutil
import socket
import stat as _stat
import sys
import time
import uuid
from pathlib import Path
try:
    from . import local_task_protocol, result_disposal, result_ready, undelivered_quarantine
    from .file_lock import lock_fd
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    local_task_protocol = importlib.import_module('local_task_protocol')
    undelivered_quarantine = importlib.import_module('undelivered_quarantine')
    result_disposal = importlib.import_module('delivery.disposal')
    result_ready = importlib.import_module('delivery.readiness')
    lock_fd = importlib.import_module('file_lock').lock_fd
LOCK_NAME = '.adapter.lock'
HOLDER_NAME = '.adapter.lock.holder'
EXIT_REFUSED = 2
EXIT_HELD = 3
HOUR = 3600.0
COMPACT_AFTER = 24 * HOUR
JOURNAL_KEEP = 2 * HOUR
LIVE_READY, LIVE_UNREADY, CLAIMED = ('LIVE_READY', 'LIVE_UNREADY', 'CLAIMED')
ARCHIVED, QUARANTINED, ABSENT, UNKNOWN = ('ARCHIVED', 'QUARANTINED', 'ABSENT', 'UNKNOWN')
PRECEDENCE = (UNKNOWN, LIVE_READY, ARCHIVED, QUARANTINED, CLAIMED, LIVE_UNREADY, ABSENT)
TERMINAL = frozenset((LIVE_READY, ARCHIVED, QUARANTINED))
PARKED = frozenset((CLAIMED, LIVE_UNREADY, UNKNOWN))
PREPARED, CALLING, NOT_DELIVERED = ('PREPARED', 'CALLING', 'NOT_DELIVERED')
CONFIRMED, OUTCOME_UNKNOWN, RESULT_STAGED = ('CONFIRMED', 'OUTCOME_UNKNOWN', 'RESULT_STAGED')
PUBLISHED, SETTLED = ('PUBLISHED', 'SETTLED')
CALLABLE = frozenset((PREPARED, NOT_DELIVERED))
FORMAT = 'cloud-session/1'
STATE_ALLOWED = frozenset(('format', 'claims', 'settled-ids', 'rate.jsonl', 'done'))

class StartupRefused(Exception):

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = (code, message)

class ClaimUnreadable(Exception):
    pass

class AttemptInProgress(Exception):
    """prepare() for a task whose attempt has not reached an outcome."""


class StateError(StartupRefused):
    """A claim or the ledger could not be read or written: the claim stays as it was, no work is
    admitted, nothing is compacted, and the adapter exits 2 naming the path."""

    def __init__(self, message):
        super().__init__(EXIT_REFUSED, 'refusing to serve: ' + message)

class LedgerError(StateError):
    pass

def _log(*a):
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def fsync_dir(path):
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def write_durable(path, data):
    """Replace `path` with `data` atomically and durably: unique temp, fsync, rename, fsync dir."""
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def append_durable(path, line):
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

class WorkLock:

    def __init__(self, fd, path):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def release(self):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def _pid_alive(pid):
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def holder_line(work):
    """Who holds the work dir, from the holder's sidecar; generic when the record cannot be trusted."""
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def acquire_work_lock(work, adapter):
    """The adapter's first act on its work dir. Nothing else under it is read or written before."""
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def _lstat(path):
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def _first_entry(root, rel=''):
    """The first path under `root` (relative), descending into non-empty directories."""
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def _init_step(name):
    """A test seam between the first-init steps; production does nothing here."""
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def _state_entry_refusal(work):
    """No marker yet: the work dir may hold the bridge's tasks/ and results/, the lock and its
    sidecar, and an interrupted init's own state/ (an empty ledger, an empty claims/)."""
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def _first_init(work):
    """One durable transaction: ledger, then claims/, then the marker last; then the directories."""
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def prepare_work_dir(work):
    """None when `work` may be served (after completing a first init); else the refusal line."""
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def probe_moves(directory):
    """Refusal line unless rename and hard link both work in `directory`."""
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def _list_strict(path):
    """Names in a directory; [] when it does not exist; OSError for anything else."""
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def _read_strict(path):
    """Bytes of a file; None when it does not exist; OSError for anything else."""
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def classify(results_dir, task_id):
    """Exactly one state for the task's result. Any lookup error is UNKNOWN, never ABSENT."""
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')
CHARGES = frozenset(('queued', 'unknown', 'fired'))

class RateJournal:
    """Append-only attempt events; one charge per (task, attempt) whatever is replayed."""

    def __init__(self, path, clock=time.time):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def _take(self, task, attempt, kind, at):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def _rewrite(self):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def append(self, task, attempt, kind, at=None):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def charges(self):
        """{(task, attempt): time} for every counted attempt, at its latest charge event."""
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def charged(self, task, attempt):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def times(self):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def wait_seconds(self, per_hour):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def _fsync_file(path):
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

class SettledIds:
    """Append-only, one validated id per newline-terminated record; never rewritten. On open an
    unterminated suffix is truncated away; anything else wrong fails closed (LedgerError)."""

    def __init__(self, path, required=False):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    @staticmethod
    def create(path):
        """An empty ledger, durable: the file and its directory are fsynced."""
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def sync(self):
        """fsync the ledger as it stands; LedgerError when that fails."""
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def __contains__(self, task_id):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def add(self, task_id):
        """Append, fsync, and only then remember. Any failure raises LedgerError."""
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

def _state_io(what, path, e):
    raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

class ClaimStore:
    """state/claims/<task_id>/{claim.json,body}. An I/O error is a StateError; a record that reads
    but does not parse is ClaimUnreadable (that task parks)."""

    def __init__(self, root):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def dir(self, task_id):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def load(self, task_id):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def save(self, rec):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def stage(self, task_id, body):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def staged(self, task_id):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def unstage(self, task_id):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def remove(self, task_id):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def ids(self):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

class Lifecycle:
    """Every durable step of one task's execution; the adapter calls the provider in between."""

    def __init__(self, work, clock=time.time, log=_log):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def classify(self, task_id):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def decide(self, task_id):
        """(call | settle | park, state), taken under the disposal lock."""
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def _save(self, rec, **changes):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def prepare(self, task_id, interrupted_body):
        """A new attempt, PREPARED. `interrupted_body` is the result a crash during the call gets."""
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def task_present(self, task_path):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def begin_call(self, task_id, attempt, task_path):
        """The final pre-call check. A task observed absent is never called (the claim is dropped);
        one observed present may be called, even if it is removed before CALLING is written."""
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def mark_calling(self, task_id, attempt):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def not_delivered(self, task_id, attempt, detail):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def record_outcome(self, task_id, kind, body, detail='', url=None):
        """kind: queued (a receipt), unknown (may have been queued) or final (no call counted)."""
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def complete_without_call(self, task_id, body, reason):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def settle_without_result(self, task_id, reason):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def _settle(self, rec):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def _holds_body(self, task_id, body):
        """Labels a settled publication won when some generation holds exactly our bytes."""
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def read_final(self, final):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def publish_and_settle(self, task_id):
        """settled | parked. Publication never replaces a result and never repeats a won rename."""
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def _publish(self, rec):
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')

    def recover(self, now=None):
        """Settle what a dead predecessor left: run once, after the lock, before serving."""
        raise NotImplementedError('the task-lifecycle contract is specified by its tests; the implementation lands in the next commit')
