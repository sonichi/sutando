/**
 * SQLite mirror of conversation.log — searchable, time-indexed, queryable.
 *
 * Issue: #603 (SQLite-ify conversation.log).
 *
 * Slice 1 (this file): parallel-write only. The text conversation.log
 * stays as primary truth for now; this sqlite is a derived mirror that's
 * cheap to rebuild from the text file (planned in slice 2). Best-effort
 * writes — sqlite errors never propagate, never block the caller.
 *
 * Usage from src/task-bridge.ts (and any other writer):
 *   import { recordConversation, recordSessionBoundary } from './conversation-store.js';
 *   recordConversation('user', 'hello');
 *   recordSessionBoundary('user_goodbye');
 *
 * Query from CLI: scripts/query-conversation.sh "<term>"
 */
import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';

const REPO_DIR = new URL('..', import.meta.url).pathname.replace(/\/$/, '');
const DB_PATH = process.env.SUTANDO_CONVERSATION_DB
	|| join(REPO_DIR, 'data', 'conversation.sqlite');

let db: DatabaseSync | null = null;
let insertStmt: ReturnType<DatabaseSync['prepare']> | null = null;
let initFailed = false;

function init(): void {
	if (db || initFailed) return;
	try {
		mkdirSync(dirname(DB_PATH), { recursive: true });
		db = new DatabaseSync(DB_PATH);
		// WAL: concurrent readers don't block the writer. busy_timeout lets
		// the second concurrent writer wait ~1s before erroring instead of
		// failing immediately on SQLITE_BUSY — adequate for the low write
		// volume (one row per conversation turn).
		db.exec('PRAGMA journal_mode = WAL');
		db.exec('PRAGMA busy_timeout = 1000');
		db.exec(`
			CREATE TABLE IF NOT EXISTS conversation (
				ts_unix    REAL NOT NULL,
				role       TEXT NOT NULL,
				text       TEXT NOT NULL,
				session_id TEXT
			);
			CREATE INDEX IF NOT EXISTS idx_ts ON conversation(ts_unix);
			CREATE INDEX IF NOT EXISTS idx_role_ts ON conversation(role, ts_unix);
			CREATE INDEX IF NOT EXISTS idx_session ON conversation(session_id, ts_unix);
		`);
		insertStmt = db.prepare(
			'INSERT INTO conversation (ts_unix, role, text, session_id) VALUES (?, ?, ?, ?)'
		);
	} catch (e) {
		console.error('[conversation-store] init failed:', e);
		initFailed = true;
		db = null;
		insertStmt = null;
	}
}

export function recordConversation(role: string, text: string, sessionId?: string): void {
	init();
	if (!insertStmt) return;
	try {
		insertStmt.run(Date.now() / 1000, role, text, sessionId ?? null);
	} catch (e) {
		console.error('[conversation-store] insert failed:', e);
	}
}

export function recordSessionBoundary(reason: string = 'user_goodbye', sessionId?: string): void {
	recordConversation('SESSION_END', reason, sessionId);
}
