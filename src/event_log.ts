/**
 * Structured event log for Sutando — TS sibling of src/event_log.py.
 *
 * Writes one JSON object per line to logs/events-YYYY-MM-DD.jsonl.
 * Each event: { "ts": <unix float>, "node": <hostname>, "kind": <string>, ...fields }
 *
 * Keep symmetric with src/event_log.py — same file path, same schema.
 * Concurrent writers are safe: appendFileSync uses O_APPEND semantics;
 * one write() per line is atomic on POSIX local filesystems (verified in
 * notes/event-log-atomicity-test.md up to 4500-byte payloads).
 *
 * Usage:
 *   import { logEvent } from './event_log.js';
 *   logEvent('task.received', { taskId: 'task-123', source: 'voice' });
 *
 * Event kinds wired from TS surfaces:
 *   task.received     — task file dispatched to agent (task-bridge.ts)
 *   task.completed    — result archived, task done (task-bridge.ts)
 *   task.timedout     — task hit timeout before result arrived
 *   task.deduped      — task consolidated into another task's result
 */

import { appendFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { hostname } from 'node:os';
import { resolveWorkspace } from './workspace_default.js';

const _node = hostname();

export function logEvent(kind: string, fields: Record<string, unknown> = {}): void {
	try {
		const ws = resolveWorkspace();
		const logsDir = join(ws, 'logs');
		mkdirSync(logsDir, { recursive: true });
		const date = new Date().toISOString().slice(0, 10); // YYYY-MM-DD
		const path = join(logsDir, `events-${date}.jsonl`);
		const event = { ts: Date.now() / 1000, node: _node, kind, ...fields };
		appendFileSync(path, JSON.stringify(event) + '\n');
	} catch {
		// Never crash the caller — event log is best-effort.
	}
}
