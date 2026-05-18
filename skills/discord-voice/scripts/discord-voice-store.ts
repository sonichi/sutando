/**
 * Inline mini conversation.sqlite writer for discord-voice (issue #792
 * sibling for voice). Self-contained — does NOT depend on
 * src/conversation-store.ts (which lives on a different branch / future
 * #791 merge). Once #791 lands and conversation-store.ts is on main, this
 * helper can be folded into that module.
 *
 * Mirrors the same schema #791 introduces (CREATE TABLE IF NOT EXISTS so
 * order with #791 doesn't matter). Writes:
 *   - per-turn row → `conversation` table (role='discord-user'|'discord-agent')
 *   - per-session rollup row → `sessions` table (source='discord-voice')
 *
 * Fail-soft — writes never throw. discord-voice availability is more
 * important than capturing every turn.
 */
import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';

const REPO_DIR = new URL('../../..', import.meta.url).pathname.replace(/\/$/, '');
const DB_PATH = process.env.SUTANDO_CONVERSATION_DB
	|| join(REPO_DIR, 'data', 'conversation.sqlite');

let db: DatabaseSync | null = null;
let turnStmt: ReturnType<DatabaseSync['prepare']> | null = null;
let sessionStmt: ReturnType<DatabaseSync['prepare']> | null = null;
let initFailed = false;

function init(): void {
	if (db || initFailed) return;
	try {
		mkdirSync(dirname(DB_PATH), { recursive: true });
		db = new DatabaseSync(DB_PATH);
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

			CREATE TABLE IF NOT EXISTS sessions (
				ts_unix          REAL    NOT NULL,
				source           TEXT    NOT NULL,
				session_id       TEXT,
				call_sid         TEXT,
				caller           TEXT,
				is_owner         INTEGER,
				is_meeting       INTEGER,
				duration_ms      INTEGER NOT NULL,
				transcript_lines INTEGER,
				tool_count       INTEGER,
				pending_tasks    INTEGER,
				tool_calls       TEXT,
				events           TEXT
			);
			CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(ts_unix);
			CREATE INDEX IF NOT EXISTS idx_sessions_source_ts ON sessions(source, ts_unix);
		`);
		turnStmt = db.prepare(
			'INSERT INTO conversation (ts_unix, role, text, session_id) VALUES (?, ?, ?, ?)',
		);
		sessionStmt = db.prepare(`
			INSERT INTO sessions (
				ts_unix, source, session_id, call_sid, caller, is_owner, is_meeting,
				duration_ms, transcript_lines, tool_count, pending_tasks, tool_calls, events
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		`);
	} catch (e) {
		console.error('[discord-voice-store] init failed:', e);
		initFailed = true;
		db = null;
	}
}

export function recordDiscordVoiceTurn(
	role: 'discord-user' | 'discord-agent' | 'discord-peer',
	text: string,
	sessionId: string,
	tsOverride?: number,
): void {
	init();
	if (!turnStmt || !text) return;
	try {
		// For agent rows, callers pass the first-audio-out time so
		// `agent.ts_unix - user.ts_unix` measures perceived latency
		// (what self-repair watches for). Falls back to wall clock when
		// audio was gated/dropped and no real emit happened.
		const ts = tsOverride ?? Date.now() / 1000;
		turnStmt.run(ts, role, text, sessionId);
	} catch (e) {
		console.error(`[discord-voice-store] turn write failed (${role}):`, e);
	}
}

export interface DiscordVoiceSessionMetrics {
	sessionId: string;
	durationMs: number;
	transcriptLines: number;
	toolCount: number;
	pendingTasks: number;
	toolCalls: unknown;
	events: unknown;
}

export function recordDiscordVoiceSession(m: DiscordVoiceSessionMetrics): void {
	init();
	if (!sessionStmt) return;
	try {
		sessionStmt.run(
			Date.now() / 1000,
			'discord-voice',
			m.sessionId,
			null, // call_sid
			null, // caller
			null, // is_owner
			null, // is_meeting
			m.durationMs,
			m.transcriptLines,
			m.toolCount,
			m.pendingTasks,
			JSON.stringify(m.toolCalls ?? []),
			JSON.stringify(m.events ?? []),
		);
	} catch (e) {
		console.error('[discord-voice-store] session write failed:', e);
	}
}
