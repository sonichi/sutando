/**
 * SQLite mirror of conversation.log — per-surface tables.
 *
 * Schema split: each voice surface (voice-agent's `voice`, the phone skill's
 * `phone`, and any plugin-registered surface) gets its own table. Each surface
 * table holds BOTH utterances AND tool calls in one chronological stream — no
 * separate mixed `conversation` table, no separate `tool_calls` table.
 *
 *   voice / phone / <plugin-surface>:
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
 * recordConversation routes by role-prefix (`phone-*` → phone, a plugin's
 * registered prefix → its surface table, otherwise voice) and recordSession's
 * tool-call fan-out routes by `source` instead of writing to a standalone
 * `tool_calls` table.
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
 */
import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { resolveWorkspace } from './workspace_default.js';
import { runConversationStoreMigrations } from './conversation-store-migrations.js';

const DB_PATH = process.env.SUTANDO_CONVERSATION_DB
	|| join(resolveWorkspace(), 'data', 'conversation.sqlite');

type Source = string;

// ---------------------------------------------------------------------------
// Surface registry (#1427 two-repo refactor, round ④). The engine knows only
// the HOST surfaces it ships (voice-agent's `voice`, the phone-conversation
// skill's `phone`). Plugin surfaces register themselves at startup via
// registerSurfaceTable — the engine never names a plugin. Each entry: which table a source writes to,
// which role prefix routes to it, and the insert column list (derived from
// the table's actual schema at registration, so surfaces with extra columns
// — speaker attribution etc. — get them persisted without the engine
// hardcoding per-surface shapes).
// ---------------------------------------------------------------------------
interface SurfaceEntry {
	table: string;
	rolePrefix: string | null;     // null = fallback surface (host `voice`)
	insertCols: string[];          // ordered columns of the prepared INSERT
	stmt: ReturnType<DatabaseSync['prepare']> | null;
}
const BASE_COLS = ['ts_unix', 'kind', 'text', 'duration_ms', 'session_id'];
// Optional per-surface extras the engine understands how to fill from
// SpeakerMeta. A surface gets them iff its registered table declares them.
const META_COLS = ['speaker_id', 'speaker_name', 'speaker_type', 'spoken'];

const surfaces = new Map<Source, SurfaceEntry>([
	['voice', { table: 'voice', rolePrefix: null, insertCols: [...BASE_COLS], stmt: null }],
	['phone', { table: 'phone', rolePrefix: 'phone-', insertCols: [...BASE_COLS], stmt: null }],
]);

let db: DatabaseSync | null = null;
let sessionInsertStmt: ReturnType<DatabaseSync['prepare']> | null = null;
let eventInsertStmt: ReturnType<DatabaseSync['prepare']> | null = null;
let initFailed = false;

/** Derive `Source` from the legacy free-form role string by registered
 *  role prefix; unmatched roles fall through to the host `voice` surface. */
export function sourceFromRole(role: string): Source {
	for (const [source, s] of surfaces) {
		if (s.rolePrefix && role.startsWith(s.rolePrefix)) return source;
	}
	return 'voice';
}

/** Normalize the legacy role string to the per-surface `kind` taxonomy.
 *  user-side roles → 'user'; agent-side → 'agent'; '*-peer' → 'peer';
 *  anything else (SESSION_END, core-agent, system event names) passes
 *  through verbatim so callers can record arbitrary kinds. */
export function kindFromRole(role: string): string {
	if (role === 'user' || role.endsWith('-user') || role.endsWith('-caller')) return 'user';
	if (role === 'assistant' || role === 'sutando'
		|| role.endsWith('-agent') || role.endsWith('-assistant')) return 'agent';
	if (role.endsWith('-peer')) return 'peer';
	return role;
}

// =============================================================================
// Plugin surface-table registration (voice-core recording API, issue #1427
// two-repo refactor). External plugins own their table DDL AND their routing
// registration — the engine never hardcodes a plugin name (manifest principle). DDL must be
// idempotent (CREATE ... IF NOT EXISTS). Fail-open like the rest of the
// store — a broken plugin table must never take down host recording.
//
// With `opts`, the call also registers the surface for live routing: roles
// matching `rolePrefix` route to `table`, the prepared INSERT is built from
// the table's ACTUAL columns (so surface-specific extras like speaker
// attribution persist without the engine knowing the shape), and the
// cross-surface `conversation` + v_<table> views are rebuilt to include it.
// =============================================================================
export function registerSurfaceTable(
	ddl: string,
	opts?: { source: string; table: string; rolePrefix: string },
): boolean {
	init();
	if (!db) return false;
	try {
		db.exec(ddl);
		if (opts) {
			surfaces.set(opts.source, {
				table: opts.table,
				rolePrefix: opts.rolePrefix,
				insertCols: insertColsFor(db, opts.table),
				stmt: null, // prepared lazily on first write
			});
			rebuildViews(db);
		}
		return true;
	} catch (e) {
		console.error('[conversation-store] registerSurfaceTable failed:', e);
		return false;
	}
}

/** Ordered insert-column list for a surface table: the base columns plus any
 *  META_COLS the table actually declares (PRAGMA-derived, not hardcoded). */
function insertColsFor(d: DatabaseSync, table: string): string[] {
	const declared = new Set(
		(d.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>).map(c => c.name),
	);
	return [...BASE_COLS, ...META_COLS.filter(c => declared.has(c))];
}

/** Lazily-prepared INSERT for a surface (column list fixed at registration). */
function stmtFor(source: Source): { stmt: ReturnType<DatabaseSync['prepare']>; cols: string[] } | null {
	const s = surfaces.get(source);
	if (!s || !db) return null;
	if (!s.stmt) {
		try {
			s.stmt = db.prepare(
				`INSERT INTO ${s.table} (${s.insertCols.join(', ')}) VALUES (${s.insertCols.map(() => '?').join(', ')})`,
			);
		} catch (e) {
			console.error(`[conversation-store] prepare failed for surface '${source}':`, e);
			return null;
		}
	}
	return { stmt: s.stmt, cols: s.insertCols };
}

/** Rebuild the per-table convenience views + the cross-surface backward-compat
 *  `conversation` view over every surface table that exists in the DB. Data-
 *  driven: includes registered surfaces AND any leftover surface table from a
 *  previously-installed plugin (so its history stays queryable plugin-absent). */
function rebuildViews(d: DatabaseSync): void {
	try {
		const known = new Set([...surfaces.values()].map(s => s.table));
		// A leftover plugin table counts as a surface table iff it has the base
		// event columns (data-driven detection — no plugin names in the engine).
		const allTables = (d.prepare(
			"SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
		).all() as Array<{ name: string }>).map(t => t.name);
		for (const t of allTables) {
			if (known.has(t) || ['sessions', 'session_events'].includes(t)) continue;
			const cols = new Set((d.prepare(`PRAGMA table_info(${t})`).all() as Array<{ name: string }>).map(c => c.name));
			if (BASE_COLS.every(c => cols.has(c))) known.add(t);
		}
		const stmts: string[] = [];
		for (const t of known) {
			const extra = META_COLS.filter(c =>
				(d.prepare(`PRAGMA table_info(${t})`).all() as Array<{ name: string }>).some(ci => ci.name === c));
			stmts.push(`DROP VIEW IF EXISTS v_${t};`);
			stmts.push(`CREATE VIEW v_${t} AS
				SELECT id, datetime(ts_unix,'unixepoch','localtime') AS time,
					ts_unix, kind, text, duration_ms, session_id${extra.length ? ', ' + extra.join(', ') : ''}
				FROM ${t} ORDER BY ts_unix DESC;`);
		}
		stmts.push('DROP VIEW IF EXISTS conversation;');
		stmts.push(`CREATE VIEW conversation AS\n${[...known].map(t =>
			`SELECT ts_unix, kind AS role, text, session_id FROM ${t}`).join('\nUNION ALL\n')};`);
		d.exec(stmts.join('\n'));
	} catch (e) {
		console.error('[conversation-store] view rebuild failed:', e);
	}
}

function init(): void {
	if (db || initFailed) return;
	try {
		mkdirSync(dirname(DB_PATH), { recursive: true });
		db = new DatabaseSync(DB_PATH);
		db.exec('PRAGMA journal_mode = WAL');
		db.exec('PRAGMA busy_timeout = 1000');

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

			-- (plugin surface tables are NOT created
			-- here — the owning plugin registers its own DDL + routing at startup
			-- via registerSurfaceTable. Round ④ of #1427: engine names no plugin.)

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

		// Startup-only migration policy (legacy table backfill + obsolete
		// sessions column removal) lives in conversation-store-migrations.ts.
		// Order matters: current tables exist above; views + prepared statements
		// come after. Idempotent and best-effort — a handled migration failure
		// must NOT make the store initFailed.
		runConversationStoreMigrations(db);

		// Convenience views — rebuilt data-driven over every surface table that
		// exists (registered hosts + any plugin table found in the DB), plus the
		// static sessions view. Re-run again whenever a plugin registers.
		db.exec(`
			DROP VIEW IF EXISTS v_sessions;
			CREATE VIEW v_sessions AS
				SELECT datetime(ts_unix,'unixepoch','localtime') AS time,
					ts_unix, source, session_id, call_sid, caller, is_owner, is_meeting,
					duration_ms, transcript_lines, tool_count, pending_tasks
				FROM sessions ORDER BY ts_unix DESC;
		`);
		rebuildViews(db);

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

/** Speaker attribution for a multi-party plugin-surface turn (#1427 meeting-buddy). */
export interface SpeakerMeta {
	speakerId?: string;        // platform user id of the turn's speaker
	speakerName?: string;      // display name (nickname || username)
	speakerType?: 'human' | 'agent';
	spoken?: boolean;          // false = generated but name-gate suppressed (no audio played)
	// Override the row timestamp (epoch SECONDS). Default is Date.now() at write time.
	// Needed for per-speaker STT (#1427, Susan 2026-06-09): a user utterance is WRITTEN
	// ~3s after it was spoken (STT latency), so stamping it at write time sorts it AFTER
	// the tool_call it actually preceded ("记录反了"). Pass the speech-end time so the
	// recorded order matches reality.
	tsUnix?: number;
}

/** Values for a surface row in registered-column order. Base columns first;
 *  any META_COLS the surface's table declares are filled from SpeakerMeta
 *  (null-padded when no meta given) — no per-surface special cases. */
function rowValues(cols: string[], parts: {
	tsUnix: number; kind: string; text: string; durationMs: number | null;
	sessionId: string | null; meta?: SpeakerMeta; defaultSpeakerType?: string | null;
}): Array<string | number | null> {
	const metaVal: Record<string, string | number | null> = {
		speaker_id: parts.meta?.speakerId ?? null,
		speaker_name: parts.meta?.speakerName ?? null,
		speaker_type: parts.meta?.speakerType ?? parts.defaultSpeakerType ?? null,
		spoken: parts.meta?.spoken === undefined ? null : (parts.meta.spoken ? 1 : 0),
	};
	return cols.map(c => {
		switch (c) {
			case 'ts_unix': return parts.tsUnix;
			case 'kind': return parts.kind;
			case 'text': return parts.text;
			case 'duration_ms': return parts.durationMs;
			case 'session_id': return parts.sessionId;
			default: return metaVal[c] ?? null;
		}
	});
}

/** Record a conversation turn. Source is derived from `role` via the surface
 *  registry's role prefixes (unmatched → host voice surface); `kind` is
 *  normalized (user / agent / peer / SESSION_END / other). Best-effort.
 *  `meta` persists only into surfaces whose registered table declares the
 *  speaker columns; others ignore it, so existing callers are unaffected. */
export function recordConversation(role: string, text: string, sessionId?: string, meta?: SpeakerMeta): void {
	init();
	const source = sourceFromRole(role);
	const s = stmtFor(source);
	if (!s) return;
	try {
		s.stmt.run(...rowValues(s.cols, {
			tsUnix: meta?.tsUnix ?? Date.now() / 1000, kind: kindFromRole(role), text,
			durationMs: null, sessionId: sessionId ?? null, meta,
		}));
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
/**
 * Record an arbitrary audit/event kind into an EXPLICIT surface table.
 * Needed because recordConversation routes by role prefix (sourceFromRole),
 * so a custom kind like 'speak_decision' would silently fall through to the
 * `voice` table — invisible to the plugin-surface audits it exists for
 * (#1427 regime_switch / speak_decision / mode_switch_regime rows). The
 * kind string is stored verbatim; mirrors recordToolCall's explicit-source
 * pattern.
 */
export function recordEvent(
	source: Source,
	kind: string,
	text: string,
	sessionId?: string | null,
): void {
	init();
	const s = stmtFor(source);
	if (!s) return;
	try {
		// Audit events come from the surface's own agent loop — default the
		// speaker_type to 'agent' on surfaces that record speaker attribution.
		s.stmt.run(...rowValues(s.cols, {
			tsUnix: Date.now() / 1000, kind, text, durationMs: null,
			sessionId: sessionId ?? null, defaultSpeakerType: 'agent',
		}));
	} catch (e) {
		console.error('[conversation-store] event insert failed:', e);
	}
}

export function recordToolCall(
	source: Source,
	name: string,
	durationMs: number | null,
	sessionId?: string | null,
): void {
	init();
	const s = stmtFor(source);
	if (!s) return;
	try {
		s.stmt.run(...rowValues(s.cols, {
			tsUnix: Date.now() / 1000, kind: 'tool_call', text: name,
			durationMs, sessionId: sessionId ?? null,
		}));
	} catch (e) {
		console.error('[conversation-store] tool_call insert failed:', e);
	}
}

export interface SessionMetrics {
	source: 'voice' | 'phone' | string;
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
	// atoms are canonical in the per-surface tables now.
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
