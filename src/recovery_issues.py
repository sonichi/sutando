"""Durable issue identities for recovery telemetry, independent of retry counts."""

import json
import os
from pathlib import Path
import tempfile
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None


def _update(path, change, emit):
    """Publish observations best effort; skip a tick if another writer holds the lock."""
    if fcntl is None:
        return
    try:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path.with_name(path.name + '.lock'), 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = json.loads(path.read_text()) if path.exists() else {}
            events = []
            change(state, events)
            if not events:
                return
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(mode='w', dir=path.parent,
                                                 prefix='.recovery-issues-', delete=False) as out:
                    tmp = Path(out.name)
                    json.dump(state, out)
                os.replace(tmp, path)
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
            for event, properties in events:
                try:
                    emit(event, **properties)
                except Exception:
                    pass
    except Exception:
        # Corrupt/unwritable state must not invent identities or prevent repairs.
        return


def _event(events, action, issue):
    events.append(('recovery_issue_' + action, {
        'issue_id': issue['issue_id'], 'issue_type': issue['issue_type'],
    }))


def _begin(state, key, issue_type, events, **local):
    if key not in state:
        state[key] = dict(issue_id=str(uuid.uuid4()), issue_type=issue_type, **local)
        _event(events, 'detected', state[key])
    _event(events, 'attempted', state[key])


def track_health_issues(path, checks, *, start, emit):
    """One issue per failing check; only an explicit OK closes it."""
    def change(state, events):
        for check in checks:
            key = check['name']
            if check['status'] == 'ok' and key in state:
                _event(events, 'recovered', state.pop(key))
            elif start and check['status'] != 'ok':
                _begin(state, key, 'health_check', events)
    _update(path, change, emit)


def track_core_issue(path, *, alive, task, status_ts, start=False, emit):
    """Keep one core issue across retries until observed queue/status progress."""
    def change(state, events):
        issue = state.get('core')
        if issue and alive is True and (
            task is None or task != issue['task'] or (
                isinstance(status_ts, (int, float))
                and isinstance(issue.get('status_ts'), (int, float))
                and status_ts > issue['status_ts']
            )
        ):
            _event(events, 'recovered', state.pop('core'))
        if start:
            _begin(state, 'core', 'core', events, task=task, status_ts=status_ts)
    _update(path, change, emit)
