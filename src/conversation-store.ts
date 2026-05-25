/**
 * SQLite mirror of conversation.log — per-surface tables.
 *
 * Schema split: each voice surface (voice-agent / phone / discord-voice)
 * gets its own table. Each surface table holds BOTH utterances AND tool
 * calls in one chronological stream — no separate mixed `conversation`
 * table, no separate `tool_calls` table.
 *
 *   voice / phone / discord_voice:
 *     id          INTEGER PRIMARY KEY  -- insertion order (canonical)
 *     ts_unix     REAL NOT NULL        -- emit time
 *     kind        TEXT NOT NULL        -- user | agent | peer | tool_call |
 *                                          tool_result | SESSION_END | ...
 *     text        TEXT                 -- utterance text OR tool name
 *     duration_ms INTEGER              -- tool_call / tool_result only
 *     session_id  TEXT
 *
 * Public API is unchanged: `recordConversation(role, text, sessionId)` and
 * `recordSession(metrics)` keep the same signatures. Internally,
 * recordConversation routes by role-prefix (`phone-*` → phone, `discord-*`
 * → discord_voice, otherwise voice) and recordSession's tool-call fan-out
 * routes by `source` instead of writing to a standalone `tool_calls` table.
 *
 * Migration: on first init, if a surface table is empty and the old
 * `conversation` / `tool_calls` tables have rows, the matching rows are
 * backfilled into the surface tables (idempotent — runs once per machine).
 * The old `conversation` and `tool_calls` tables are then dropped.
 * `sessions` (per-session rollup) and `session_events` (unified event log)
 * are kept — they serve different concerns.
 *
 * Best-effort throughout: sqlite errors never propagate, never block the
 * caller.
 *
 * Usage (signatures unchanged):
 *   import { recordConversation, recordSessionBoundary, recordSession }
 *     from './conversation-store.js';
 *   recordConversation('user', 'hello');            // → voice table
 *   recordConversation('phone-caller', 'hi');       // → phone table
 *   recordConversation('discord-user', 'hey');      // → discord_voice table
 */
import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { resolveWorkspace } from './workspace_default.js';

const DB_PATH = process.env.SUTANDO_CONVERSATION_DB
	|| join(resolveWorkspace(), 'data', 'conversation.sqlite');

type Source = 'voice' | 'phone' | 'discord-voice';

let db: DatabaseSync | null = null;
const turnStmt: Record<Source, ReturnType<DatabaseSync['prepare']> | null> = {
	'voice': null, 'phone': null, 'discord-voice': null,
};
let sessionInsertStmt: ReturnType<DatabaseSync['prepare']> | null = null;
let eventInsertStmt: ReturnType<DatabaseSync['prepare']> | null = null;
let initFailed = false;

/** Map a `Source` to its canonical SQLite table name. */
function tableForSource(source: Source): string {
	if (source === 'phone') return 'phone';
	if (source === 'discord-voice') return 'discord_voice';
	return 'voice';
}

/** Derive `Source` from the legacy free-form role string. */
export function sourceFromRole(role: string): Source {
	if (role.startsWith('phone-')) return 'phone';
	if (role.startsWith('discord-')) return 'discord-voice';
	return 'voice';
}

/** Normalize the legacy role string to the per-surface `kind` taxonomy.
 *  user-side roles → 'user'; agent-side → 'agent'; discord-peer → 'peer';
 *  anything else (SESSION_END, core-agent, system event names) passes
 *  through verbatim so callers can record arbitrary kinds. */
export function kindFromRole(role: string): string {
	if (role === 'user' || role.endsWith('-user') || role.endsWith('-caller')) return 'user';
	if (role === 'assistant' || role === 'sutando'
		|| role.endsWith('-agent') || role.endsWith('-assistant')) return 'agent';
	if (role === 'discord-peer') return 'peer';
	return role;
}

function init(): void {
	if (db || initFailed) return;
	try {
		mkdirSync(dirname(DB_PATH), { recursive: true });
		db = new DatabaseSync(DB_PATH);
		db.exec('PRAGMA journal_mode = WAL');
		db.exec('PRAGMA busy_timeout = 1000');

		// Defensive: an older `discord_voice` table (e.g. from a multi-instance
		// branch using `discord-voice-store.ts`) used a `role` column instead
		// of the new `kind` column. CREATE TABLE IF NOT EXISTS would skip the
		// new schema and we'd write to mismatched columns. Rename any pre-
		// existing legacy-schema table out of the way so the new CREATE runs.
		try {
			const old = db.prepare(
				"SELECT name FROM sqlite_master WHERE type='table' AND name='discord_voice'",
			).get();
			if (old) {
				const cols = db.prepare("PRAGMA table_info(discord_voice)").all() as Array<{ name: string }>;
				const hasKind = cols.some(c => c.name === 'kind');
				if (!hasKind) {
					db.exec('ALTER TABLE discord_voice RENAME TO discord_voice_legacy');
					console.log('[conversation-store] renamed legacy discord_voice → discord_voice_legacy (different schema)');
				}
			}
		} catch (e) {
			console.error('[conversation-store] legacy-schema detect failed:', e);
		}

		db.exec(`
			-- Per-surface event tables. Identical schema; one per voice surface.
			-- Holds utterances + tool calls in one chronological stream — id is
			-- insertion order (canonical sort key), ts_unix is emit time.
			CREATE TABLE IF NOT EXISTS voice (
				id          INTEGER PRIMARY KEY,
				ts_unix     REAL    NOT NULL,
				kind        TEXT    NOT NULL,
				text        TEXT,
				duration_ms INTEGER,
				session_id  TEXT
			);
			CREATE INDEX IF NOT EXISTS idx_voice_ts ON voice(ts_unix);
			CREATE INDEX IF NOT EXISTS idx_voice_kind_ts ON voice(kind, ts_unix);
			CREATE INDEX IF NOT EXISTS idx_voice_session ON voice(session_id, ts_unix);

			CREATE TABLE IF NOT EXISTS phone (
				id          INTEGER PRIMARY KEY,
				ts_unix     REAL    NOT NULL,
				kind        TEXT    NOT NULL,
				text        TEXT,
				duration_ms INTEGER,
				session_id  TEXT
			);
			CREATE INDEX IF NOT EXISTS idx_phone_ts ON phone(ts_unix);
			CREATE INDEX IF NOT EXISTS idx_phone_kind_ts ON phone(kind, ts_unix);
			CREATE INDEX IF NOT EXISTS idx_phone_session ON phone(session_id, ts_unix);

			CREATE TABLE IF NOT EXISTS discord_voice (
				id          INTEGER PRIMARY KEY,
				ts_unix     REAL    NOT NULL,
				kind        TEXT    NOT NULL,
				text        TEXT,
				duration_ms INTEGER,
				session_id  TEXT
			);
			CREATE INDEX IF NOT EXISTS idx_discord_voice_ts ON discord_voice(ts_unix);
			CREATE INDEX IF NOT EXISTS idx_discord_voice_kind_ts ON discord_voice(kind, ts_unix);
			CREATE INDEX IF NOT EXISTS idx_discord_voice_session ON discord_voice(session_id, ts_unix);

			-- Per-session rollup. Kept — different concern from the per-event log.
			-- Per-tool-call rows live in surface tables (kind='tool_call'),
			-- per-event rows live in session_events. The old tool_calls/events
			-- JSON columns are dropped post-init via the migration below.
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
				pending_tasks    INTEGER
			);
			CREATE INDEX IF NOT EXISTS idx_sessions_ts ON sessions(ts_unix);
			CREATE INDEX IF NOT EXISTS idx_sessions_source_ts ON sessions(source, ts_unix);
			CREATE INDEX IF NOT EXISTS idx_sessions_call_sid ON sessions(call_sid);

			-- Unified event log — lifecycle events (session_started, call_ended,
			-- transport_close, etc.). Kept — different concern from per-event log.
			CREATE TABLE IF NOT EXISTS session_events (
				ts_unix    REAL NOT NULL,
				source     TEXT NOT NULL,
				session_id TEXT,
				call_sid   TEXT,
				event_name TEXT NOT NULL
			);
			CREATE INDEX IF NOT EXISTS idx_session_events_ts ON session_events(ts_unix);
			CREATE INDEX IF NOT EXISTS idx_session_events_name_ts ON session_events(event_name, ts_unix);
			CREATE INDEX IF NOT EXISTS idx_session_events_session ON session_events(session_id, ts_unix);
		`);

		// One-time migration: backfill from the legacy `conversation` and
		// `tool_calls` tables into the new per-surface tables, then drop the
		// legacy tables. Idempotent — gated on each surface table being empty.
		migrateLegacyIfNeeded(db);

		// Drop the redundant sessions.tool_calls / sessions.events JSON
		// columns if a pre-#1052 db still has them. The atom rows now live
		// in surface tables (kind='tool_call') and session_events
		// respectively; the JSON cols were triple-encoding the same data.
		// SQLite 3.35+ supports ALTER TABLE DROP COLUMN — guard via
		// pragma_table_info so re-running this is a no-op.
		const sessionCols = new Set(
			(db.prepare("PRAGMA table_info(sessions)").all() as Array<{ name: string }>)
				.map(c => c.name),
		);
		if (sessionCols.has('tool_calls')) {
			try {
				db.exec('ALTER TABLE sessions DROP COLUMN tool_calls');
				console.log('[conversation-store] dropped sessions.tool_calls (redundant w/ surface tables)');
			} catch (e) {
				console.error('[conversation-store] could not drop sessions.tool_calls:', e);
			}
		}
		if (sessionCols.has('events')) {
			try {
				db.exec('ALTER TABLE sessions DROP COLUMN events');
				console.log('[conversation-store] dropped sessions.events (redundant w/ session_events)');
			} catch (e) {
				console.error('[conversation-store] could not drop sessions.events:', e);
			}
		}

		// Convenience views — thin wrappers that add a human-readable `time`
		// column (local-time) and default-sort by ts_unix DESC. DROP+CREATE so
		// definitions stay in lock-step with the surface tables and any
		// pre-migration v_* (which referenced now-dropped legacy tables) gets
		// replaced cleanly.
		db.exec(`
			DROP VIEW IF EXISTS v_voice;
			DROP VIEW IF EXISTS v_phone;
			DROP VIEW IF EXISTS v_discord_voice;
			DROP VIEW IF EXISTS v_sessions;
			DROP VIEW IF EXISTS conversation;
			CREATE VIEW v_voice AS
				SELECT id, datetime(ts_unix,'unixepoch','localtime') AS time,
					ts_unix, kind, text, duration_ms, session_id
				FROM voice ORDER BY ts_unix DESC;
			CREATE VIEW v_phone AS
				SELECT id, datetime(ts_unix,'unixepoch','localtime') AS time,
					ts_unix, kind, text, duration_ms, session_id
				FROM phone ORDER BY ts_unix DESC;
			CREATE VIEW v_discord_voice AS
				SELECT id, datetime(ts_unix,'unixepoch','localtime') AS time,
					ts_unix, kind, text, duration_ms, session_id
				FROM discord_voice ORDER BY ts_unix DESC;
			CREATE VIEW v_sessions AS
				SELECT datetime(ts_unix,'unixepoch','localtime') AS time,
					ts_unix, source, session_id, call_sid, caller, is_owner, is_meeting,
					duration_ms, transcript_lines, tool_count, pending_tasks
				FROM sessions ORDER BY ts_unix DESC;
			-- Backward-compat view for pre-refactor readers that still
			-- SELECT FROM conversation with the old role column. Surface
			-- the union of all 3 tables under the legacy schema so external
			-- scripts (query-conversation.sh, regression-search, any other
			-- consumer we missed) keep working without source edits. New
			-- code should read the surface tables directly.
			CREATE VIEW conversation AS
				SELECT ts_unix, kind AS role, text, session_id FROM voice
				UNION ALL
				SELECT ts_unix, kind AS role, text, session_id FROM phone
				UNION ALL
				SELECT ts_unix, kind AS role, text, session_id FROM discord_voice;
		`);

		turnStmt['voice'] = db.prepare(
			'INSERT INTO voice (ts_unix, kind, text, duration_ms, session_id) VALUES (?, ?, ?, ?, ?)',
		);
		turnStmt['phone'] = db.prepare(
			'INSERT INTO phone (ts_unix, kind, text, duration_ms, session_id) VALUES (?, ?, ?, ?, ?)',
		);
		turnStmt['discord-voice'] = db.prepare(
			'INSERT INTO discord_voice (ts_unix, kind, text, duration_ms, session_id) VALUES (?, ?, ?, ?, ?)',
		);
		sessionInsertStmt = db.prepare(`
			INSERT INTO sessions (
				ts_unix, source, session_id, call_sid, caller, is_owner, is_meeting,
				duration_ms, transcript_lines, tool_count, pending_tasks
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		`);
		eventInsertStmt = db.prepare(`
			INSERT INTO session_events (ts_unix, source, session_id, call_sid, event_name)
			VALUES (?, ?, ?, ?, ?)
		`);
	} catch (e) {
		console.error('[conversation-store] init failed:', e);
		initFailed = true;
		db = null;
	}
}

/** One-time migration: copy rows from legacy `conversation` and `tool_calls`
 *  tables into the per-surface tables, then drop the legacy tables. Gated on
 *  each per-surface table being empty (so a re-run on an already-migrated db
 *  is a no-op). Wrapped in a single transaction; failures are logged but
 *  don't propagate. */
function migrateLegacyIfNeeded(d: DatabaseSync): void {
	try {
		const hasConversation = d.prepare(
			"SELECT name FROM sqlite_master WHERE type='table' AND name='conversation'",
		).get();
		const hasToolCalls = d.prepare(
			"SELECT name FROM sqlite_master WHERE type='table' AND name='tool_calls'",
		).get();
		if (!hasConversation && !hasToolCalls) return; // nothing to migrate

		const voiceEmpty = (d.prepare('SELECT count(*) AS c FROM voice').get() as { c: number }).c === 0;
		const phoneEmpty = (d.prepare('SELECT count(*) AS c FROM phone').get() as { c: number }).c === 0;
		const discordEmpty = (d.prepare('SELECT count(*) AS c FROM discord_voice').get() as { c: number }).c === 0;
		if (!voiceEmpty && !phoneEmpty && !discordEmpty) {
			// All surface tables already populated — nothing to backfill.
			// Drop legacy tables if they're still around.
			if (hasConversation) d.exec('DROP TABLE IF EXISTS conversation');
			if (hasToolCalls) d.exec('DROP TABLE IF EXISTS tool_calls');
			return;
		}

		console.log('[conversation-store] migrating legacy conversation + tool_calls into per-surface tables');
		d.exec('BEGIN');
		try {
			if (hasConversation && voiceEmpty) {
				// Utterances → voice. Roles that map to voice: 'user', 'assistant',
				// 'sutando', 'core-agent', 'SESSION_END', and anything not prefixed
				// 'phone-' / 'discord-'. kind normalization: user/assistant/sutando
				// → user/agent, others passthrough.
				d.exec(`
					INSERT INTO voice (ts_unix, kind, text, duration_ms, session_id)
					SELECT ts_unix,
					       CASE
					         WHEN role='user' THEN 'user'
					         WHEN role IN ('assistant','sutando') THEN 'agent'
					         ELSE role
					       END,
					       text, NULL, session_id
					FROM conversation
					WHERE role NOT LIKE 'phone-%' AND role NOT LIKE 'discord-%'
				`);
			}
			if (hasConversation && phoneEmpty) {
				d.exec(`
					INSERT INTO phone (ts_unix, kind, text, duration_ms, session_id)
					SELECT ts_unix,
					       CASE
					         WHEN role LIKE 'phone-caller%' THEN 'user'
					         WHEN role LIKE 'phone-agent%'  THEN 'agent'
					         ELSE substr(role, 7)
					       END,
					       text, NULL, session_id
					FROM conversation WHERE role LIKE 'phone-%'
				`);
			}
			if (hasConversation && discordEmpty) {
				d.exec(`
					INSERT INTO discord_voice (ts_unix, kind, text, duration_ms, session_id)
					SELECT ts_unix,
					       CASE
					         WHEN role='discord-user'  THEN 'user'
					         WHEN role='discord-agent' THEN 'agent'
					         WHEN role='discord-peer'  THEN 'peer'
					         ELSE substr(role, 9)
					       END,
					       text, NULL, session_id
					FROM conversation WHERE role LIKE 'discord-%'
				`);
			}
			if (hasToolCalls) {
				// Tool calls → surface table by `source`. kind='tool_call', text=name,
				// duration_ms preserved. (The standalone tool_calls table goes away —
				// per-tool-call rows now live alongside utterances in the surface
				// table, ordered by ts_unix.)
				d.exec(`
					INSERT INTO voice (ts_unix, kind, text, duration_ms, session_id)
					SELECT ts_unix, 'tool_call', name, duration_ms, session_id
					FROM tool_calls WHERE source='voice'
				`);
				d.exec(`
					INSERT INTO phone (ts_unix, kind, text, duration_ms, session_id)
					SELECT ts_unix, 'tool_call', name, duration_ms, session_id
					FROM tool_calls WHERE source='phone'
				`);
				d.exec(`
					INSERT INTO discord_voice (ts_unix, kind, text, duration_ms, session_id)
					SELECT ts_unix, 'tool_call', name, duration_ms, session_id
					FROM tool_calls WHERE source='discord-voice'
				`);
				// Also backfill discord-voice tool calls from sessions.tool_calls JSON —
				// discord-voice historically never wrote to the tool_calls table; its
				// per-call data lives only in the sessions JSON column. Use json_each
				// to expand.
				d.exec(`
					INSERT INTO discord_voice (ts_unix, kind, text, duration_ms, session_id)
					SELECT CAST(strftime('%s', json_extract(je.value,'$.timestamp')) AS REAL),
					       'tool_call',
					       json_extract(je.value,'$.name'),
					       json_extract(je.value,'$.durationMs'),
					       s.session_id
					FROM sessions s, json_each(s.tool_calls) je
					WHERE s.source='discord-voice'
					  AND s.tool_calls IS NOT NULL
					  AND s.tool_calls != '[]'
				`);
			}
			// If a legacy `discord_voice` table was renamed aside (different
			// schema from an older multi-instance branch), backfill its rows.
			const legacy = d.prepare(
				"SELECT name FROM sqlite_master WHERE type='table' AND name='discord_voice_legacy'",
			).get();
			if (legacy) {
				d.exec(`
					INSERT INTO discord_voice (ts_unix, kind, text, duration_ms, session_id)
					SELECT ts_unix,
					       CASE
					         WHEN role='discord-user'  THEN 'user'
					         WHEN role='discord-agent' THEN 'agent'
					         WHEN role='discord-peer'  THEN 'peer'
					         ELSE role
					       END,
					       text, NULL, session_id
					FROM discord_voice_legacy
				`);
				d.exec('DROP TABLE IF EXISTS discord_voice_legacy');
			}
			// Drop legacy tables — they're fully migrated.
			if (hasConversation) d.exec('DROP TABLE IF EXISTS conversation');
			if (hasToolCalls) d.exec('DROP TABLE IF EXISTS tool_calls');
			d.exec('COMMIT');
			console.log('[conversation-store] migration done; legacy tables dropped');
		} catch (e) {
			d.exec('ROLLBACK');
			console.error('[conversation-store] migration failed (rolled back):', e);
		}
	} catch (e) {
		console.error('[conversation-store] migration probe failed:', e);
	}
}

/** Record a conversation turn. Source is derived from `role` (`phone-*` →
 *  phone, `discord-*` → discord_voice, otherwise voice); `kind` is
 *  normalized (user / agent / peer / SESSION_END / other). Best-effort. */
export function recordConversation(role: string, text: string, sessionId?: string): void {
	init();
	const source = sourceFromRole(role);
	const stmt = turnStmt[source];
	if (!stmt) return;
	try {
		stmt.run(Date.now() / 1000, kindFromRole(role), text, null, sessionId ?? null);
	} catch (e) {
		console.error('[conversation-store] insert failed:', e);
	}
}

export function recordSessionBoundary(reason: string = 'user_goodbye', sessionId?: string): void {
	recordConversation('SESSION_END', reason, sessionId);
}

/**
 * Record a single tool invocation into the matching surface table as
 * `kind='tool_call'`. Call this from each surface's `onToolResult` hook so
 * tool calls land in db immediately (and are visible mid-session in DB
 * Browser) instead of being batched at session end via recordSession's
 * fan-out — that older path lost everything if the session never cleanly
 * ended (crash, kill -9, ngrok drop). durationMs may be null when unknown.
 */
export function recordToolCall(
	source: 'voice' | 'phone' | 'discord-voice',
	name: string,
	durationMs: number | null,
	sessionId?: string | null,
): void {
	init();
	const stmt = turnStmt[source];
	if (!stmt) return;
	try {
		stmt.run(Date.now() / 1000, 'tool_call', name, durationMs, sessionId ?? null);
	} catch (e) {
		console.error('[conversation-store] tool_call insert failed:', e);
	}
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
	/** No longer persisted (per #1052). Surface table rows with
	 *  kind='tool_call' are canonical; this field is accepted only for
	 *  backwards-compat with existing callers and silently ignored. */
	toolCalls?: unknown;
	/** Iterated for the session_events fan-out (lifecycle events). User /
	 *  agent / tool_call / tool_result entries are filtered out — those
	 *  atoms live in surface tables now (per #1052). */
	events?: unknown;
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

// Event names whose substance already lives in a surface table row
// (kind='user'/'agent'/'tool_call'). Filtered out of the session_events
// fan-out so the same atom isn't recorded twice. Defense-in-depth: the
// 3 surface servers also stopped pushing these into their in-memory
// events array as of #1052; this filter catches anything missed +
// protects against external callers passing them in m.events.
const DUPLICATE_EVENT_PREFIXES = ['user:', 'caller:', 'sutando:', 'assistant:', 'tool_call:', 'tool_result:'];

/**
 * Record per-session rollup. Also fans out lifecycle events (session_started,
 * session_ended, error, task_*, etc.) into the unified session_events table.
 * Tool calls are NOT fanned out here — each surface server writes them in
 * real time via recordToolCall() inside its onToolResult hook. Utterance
 * events with user:/sutando: prefixes are filtered out — they duplicate
 * surface-table user/agent rows. Best-effort.
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
		);
	} catch (e) {
		console.error('[conversation-store] session insert failed:', e);
	}
	// Fan out LIFECYCLE events into session_events. Skip duplicates of
	// surface-table rows (user/sutando/tool_call/tool_result) — those
	// atoms are canonical in voice/phone/discord_voice tables now.
	if (eventInsertStmt && Array.isArray(m.events)) {
		for (const ev of m.events as Array<Record<string, unknown>>) {
			const name = String(ev.event ?? 'unknown');
			if (DUPLICATE_EVENT_PREFIXES.some(p => name.startsWith(p))) continue;
			try {
				eventInsertStmt.run(
					tsToUnix(ev.timestamp) ?? nowUnix,
					m.source,
					m.sessionId ?? null,
					m.callSid ?? null,
					name,
				);
			} catch (e) {
				console.error('[conversation-store] session_events insert failed:', e);
			}
		}
	}
}
