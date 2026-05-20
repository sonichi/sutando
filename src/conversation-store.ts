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
import { resolveWorkspace } from './workspace_default.js';

// DB lives under the resolved workspace (same tree as conversation.log,
// which task-bridge.ts writes to `<workspace>/conversation.log`). Using the
// repo root here would strand the sqlite mirror in a different tree from the
// text log once runtime state moved to ~/.sutando/workspace (#831).
const DB_PATH = process.env.SUTANDO_CONVERSATION_DB
	|| join(resolveWorkspace(), 'data', 'conversation.sqlite');

let db: DatabaseSync | null = null;
let insertStmt: ReturnType<DatabaseSync['prepare']> | null = null;
let sessionInsertStmt: ReturnType<DatabaseSync['prepare']> | null = null;
let toolCallInsertStmt: ReturnType<DatabaseSync['prepare']> | null = null;
let eventInsertStmt: ReturnType<DatabaseSync['prepare']> | null = null;
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
			CREATE INDEX IF NOT EXISTS idx_conversation_ts ON conversation(ts_unix);
			CREATE INDEX IF NOT EXISTS idx_conversation_role_ts ON conversation(role, ts_unix);
			CREATE INDEX IF NOT EXISTS idx_conversation_session ON conversation(session_id, ts_unix);

			-- Per-session rollups (replaces data/voice-metrics.jsonl + data/call-metrics.jsonl).
			-- Unified table covers voice + phone + future discord-voice sources.
			-- tool_calls + events are kept as JSON for at-a-glance / cross-version compat,
			-- but per-row data ALSO lands in the dedicated tool_calls + session_events tables below
			-- so SQL queries don't need json_extract.
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
				tool_calls       TEXT,                -- JSON array (also in tool_calls table)
				events           TEXT                 -- JSON array (also in session_events table)
			);
			CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(ts_unix);
			CREATE INDEX IF NOT EXISTS idx_sessions_source_ts ON sessions(source, ts_unix);
			CREATE INDEX IF NOT EXISTS idx_sessions_call_sid ON sessions(call_sid);

			-- Per-tool-call rows extracted from sessions.tool_calls JSON.
			-- Query directly: SELECT name, AVG(duration_ms) FROM tool_calls
			--                  WHERE ts_unix > strftime('%s','now')-86400 GROUP BY name
			CREATE TABLE IF NOT EXISTS tool_calls (
				ts_unix     REAL NOT NULL,           -- tool call timestamp (not session start)
				source      TEXT NOT NULL,           -- 'voice' | 'phone' | etc
				session_id  TEXT,                    -- voice session_id
				call_sid    TEXT,                    -- phone callSid
				name        TEXT NOT NULL,           -- tool name (describe_screen, work, etc)
				duration_ms INTEGER                  -- tool execution duration
			);
			CREATE INDEX IF NOT EXISTS idx_tool_calls_ts ON tool_calls(ts_unix);
			CREATE INDEX IF NOT EXISTS idx_tool_calls_name_ts ON tool_calls(name, ts_unix);
			CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id, ts_unix);

			-- Per-event rows extracted from sessions.events JSON.
			-- Covers lifecycle events (session_started, call_ended, transport_close, etc).
			CREATE TABLE IF NOT EXISTS session_events (
				ts_unix    REAL NOT NULL,
				source     TEXT NOT NULL,
				session_id TEXT,
				call_sid   TEXT,
				event_name TEXT NOT NULL              -- session_started, tool_call, call_ended, etc.
			);
			CREATE INDEX IF NOT EXISTS idx_session_events_ts ON session_events(ts_unix);
			CREATE INDEX IF NOT EXISTS idx_session_events_name_ts ON session_events(event_name, ts_unix);
			CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events(session_id, ts_unix);
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
		toolCallInsertStmt = db.prepare(`
			INSERT INTO tool_calls (ts_unix, source, session_id, call_sid, name, duration_ms)
			VALUES (?, ?, ?, ?, ?, ?)
		`);
		eventInsertStmt = db.prepare(`
			INSERT INTO session_events (ts_unix, source, session_id, call_sid, event_name)
			VALUES (?, ?, ?, ?, ?)
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

/** Parse a value that should be a timestamp into unix seconds, or null. */
function tsToUnix(t: unknown): number | null {
	if (typeof t === 'string') {
		const n = Date.parse(t);
		return Number.isFinite(n) ? n / 1000 : null;
	}
	if (typeof t === 'number') return t > 1e12 ? t / 1000 : t;
	return null;
}

/**
 * Record per-session rollup. Replaces appendFileSync to
 * data/voice-metrics.jsonl (voice-agent) and data/call-metrics.jsonl
 * (phone-conversation). Also fans out toolCalls + events into the
 * dedicated `tool_calls` and `session_events` tables for queryability.
 * Best-effort — sqlite errors swallowed.
 */
export function recordSession(m: SessionMetrics): void {
	init();
	if (!sessionInsertStmt) return;
	const nowUnix = Date.now() / 1000;
	try {
		sessionInsertStmt.run(
			nowUnix,
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
	// Fan out tool_calls into the dedicated table.
	if (toolCallInsertStmt && Array.isArray(m.toolCalls)) {
		for (const tc of m.toolCalls as Array<Record<string, unknown>>) {
			try {
				toolCallInsertStmt.run(
					tsToUnix(tc.timestamp) ?? nowUnix,
					m.source,
					m.sessionId ?? null,
					m.callSid ?? null,
					String(tc.name ?? 'unknown'),
					typeof tc.durationMs === 'number' ? tc.durationMs : null,
				);
			} catch (e) {
				console.error('[conversation-store] tool_calls insert failed:', e);
			}
		}
	}
	// Fan out events into the dedicated table.
	if (eventInsertStmt && Array.isArray(m.events)) {
		for (const ev of m.events as Array<Record<string, unknown>>) {
			try {
				eventInsertStmt.run(
					tsToUnix(ev.timestamp) ?? nowUnix,
					m.source,
					m.sessionId ?? null,
					m.callSid ?? null,
					String(ev.event ?? 'unknown'),
				);
			} catch (e) {
				console.error('[conversation-store] session_events insert failed:', e);
			}
		}
	}
}
