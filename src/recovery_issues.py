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


HEALTH_CAUSES = frozenset("""
voice-agent voice-watchers voice-transport bodhi-dist cli-wedge secret-scanner
node-runtime cron-runner session-crons memory-dir-override workspace-wiring
context-read-budget workspace-root-tidy memory-dir-siblings carrier-set memory-index
memory-sync onboarding-status host-subtrees per-host-config-backup sync-conflicts-unmerged
skills-driver-code-drift live-checkout-branch engine-revision-drift migrate-reader-contract
tcc-documents-access quota-telemetry core-request-rejections core-quota quota-account-identity
battery memory cron-schedule core-proactive-loop core-supervisor gateway-bridge runtime-identity
daily-cron-punctuality live-tree-drift disk-space skill-symlinks task-queue pool-advertisement held-no-consumer
orphaned-results stranded-destined-proactive proactive-quarantine stale-proactive-backlog
task-watcher a-fallback-hits task-claims codex-task-notifier claude-task-notifier codex-presence sandbox-delegation notes-split-brain
vendored-resolver-env legacy-notes-divergence vault-manifest claude-hooks comm-sweep
core-model-pin web-client memory-dir tailscale-funnel sutando-app telegram-bridge
discord-bridge slack-bridge whatsapp-bridge
agent-api dashboard screen-capture credential-proxy notes-dir voice-config
CLAUDE.md build_log.md .env conversation-server ngrok
""".split())


def _health_cause(check):
    name = check['name']
    if name not in HEALTH_CAUSES:
        name = 'custom-check' if name.startswith('extra:') else (
            'dynamic-loop' if name.startswith('dynamic-loop:') else 'other-check')
    status = check['status'] if check['status'] in ('warn', 'down') else 'non-ok'
    return 'health:' + name + ':' + status


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
        'issue_cause': issue.get('issue_cause', 'unknown'),
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
                _begin(state, key, 'health_check', events, issue_cause=_health_cause(check))
    _update(path, change, emit)


def track_core_issue(path, *, alive, task, status_ts, start=False, cause=None, emit):
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
            _begin(state, 'core', 'core', events, task=task, status_ts=status_ts,
                   issue_cause='core:' + cause if cause in ('dead', 'wedged') else 'unknown')
    _update(path, change, emit)
