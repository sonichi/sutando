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
let sessionInsertStmt: ReturnType<DatabaseSync['prepare']> | null = null;
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

			-- Per-session rollups (replaces data/voice-metrics.jsonl + data/call-metrics.jsonl).
			-- Unified table covers voice + phone + future discord-voice sources.
			CREATE TABLE IF NOT EXISTS sessions (
				ts_unix          REAL    NOT NULL,
				source           TEXT    NOT NULL,    -- 'voice' | 'phone' | 'discord-voice' | ...
				session_id       TEXT,                -- voice/discord-voice key
				call_sid         TEXT,                -- phone (Twilio) key
				caller           TEXT,                -- phone caller number
				is_owner         INTEGER,             -- phone access tier (0/1)
				is_meeting       INTEGER,             -- phone is_meeting (0/1)
				duration_ms      INTEGER NOT NULL,
				transcript_lines INTEGER,
				tool_count       INTEGER,
				pending_tasks    INTEGER,
				tool_calls       TEXT,                -- JSON array
				events           TEXT                 -- JSON array
			);
			CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(ts_unix);
			CREATE INDEX IF NOT EXISTS idx_sessions_source_ts ON sessions(source, ts_unix);
			CREATE INDEX IF NOT EXISTS idx_sessions_call_sid ON sessions(call_sid);
		`);
		insertStmt = db.prepare(
			'INSERT INTO conversation (ts_unix, role, text, session_id) VALUES (?, ?, ?, ?)'
		);
		sessionInsertStmt = db.prepare(`
			INSERT INTO sessions (
				ts_unix, source, session_id, call_sid, caller, is_owner, is_meeting,
				duration_ms, transcript_lines, tool_count, pending_tasks, tool_calls, events
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		`);
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

export interface SessionMetrics {
	source: 'voice' | 'phone' | 'discord-voice' | string;
	sessionId?: string | null;
	callSid?: string | null;
	caller?: string | null;
	isOwner?: boolean | null;
	isMeeting?: boolean | null;
	durationMs: number;
	transcriptLines?: number | null;
	toolCount?: number | null;
	pendingTasks?: number | null;
	toolCalls?: unknown;     // JSON-serializable array
	events?: unknown;        // JSON-serializable array
}

/**
 * Record per-session rollup. Replaces appendFileSync to
 * data/voice-metrics.jsonl (voice-agent) and data/call-metrics.jsonl
 * (phone-conversation). Best-effort — sqlite errors swallowed.
 */
export function recordSession(m: SessionMetrics): void {
	init();
	if (!sessionInsertStmt) return;
	try {
		sessionInsertStmt.run(
			Date.now() / 1000,
			m.source,
			m.sessionId ?? null,
			m.callSid ?? null,
			m.caller ?? null,
			m.isOwner === null || m.isOwner === undefined ? null : (m.isOwner ? 1 : 0),
			m.isMeeting === null || m.isMeeting === undefined ? null : (m.isMeeting ? 1 : 0),
			m.durationMs,
			m.transcriptLines ?? null,
			m.toolCount ?? null,
			m.pendingTasks ?? null,
			m.toolCalls === undefined ? null : JSON.stringify(m.toolCalls),
			m.events === undefined ? null : JSON.stringify(m.events),
		);
	} catch (e) {
		console.error('[conversation-store] session insert failed:', e);
	}
}
