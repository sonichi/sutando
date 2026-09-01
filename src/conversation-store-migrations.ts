/**
 * Startup-only SQLite migration policy for the conversation store.
 *
 * Extracted from `conversation-store.ts` so the live writer owns current schema
 * + write APIs only, and these destructive, one-time legacy transformations can
 * be tested against disposable databases without driving a voice or phone
 * session.
 *
 * Two migrations live here, in this order:
 *   1. Backfill the legacy mixed `conversation` / `tool_calls` tables into the
 *      per-surface tables, then drop the legacy tables (one transaction).
 *   2. Drop the redundant `sessions.tool_calls` / `sessions.events` JSON
 *      columns left over from pre-#1052 databases.
 *
 * Preconditions (the caller's responsibility, unchanged from before the split):
 *   - the database is already open;
 *   - the current `voice`, `phone`, `sessions` and `session_events` tables
 *     already exist — this module never creates current-schema DDL;
 *   - call this AFTER those tables exist and BEFORE views are rebuilt and
 *     statements are prepared.
 *
 * Contract:
 *   - idempotent: a second run over an already-migrated database is a no-op;
 *   - best-effort: every failure is logged and swallowed. The caller MUST be
 *     able to keep initializing after a handled migration failure — the store
 *     must not become `initFailed` just because a migration was rolled back.
 *
 * Deliberately NOT a migration framework: no version table, no registry, no
 * abstraction for hypothetical future migrations. This is the policy that
 * already existed, moved.
 */
import { DatabaseSync } from 'node:sqlite';

/** Run every startup migration, in order. Idempotent and best-effort — see the
 *  module docstring for the full contract. */
export function runConversationStoreMigrations(db: DatabaseSync): void {
	migrateLegacyIfNeeded(db);
	dropObsoleteSessionColumns(db);
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

		// Live routing/DDL for plugin surfaces comes only from registerSurfaceTable;
		// this LEGACY migration covers the host surfaces (voice, phone) — any
		// plugin surface owns its own table DDL and any historical backfill.
		const voiceEmpty = (d.prepare('SELECT count(*) AS c FROM voice').get() as { c: number }).c === 0;
		const phoneEmpty = (d.prepare('SELECT count(*) AS c FROM phone').get() as { c: number }).c === 0;
		if (!voiceEmpty && !phoneEmpty) {
			// All surface tables already populated — nothing to backfill.
			// Drop legacy tables if they're still around.
			if (hasConversation) d.exec('DROP TABLE IF EXISTS conversation');
			if (hasToolCalls) d.exec('DROP TABLE IF EXISTS tool_calls');
			return;
		}

		console.log('[conversation-store] migrating legacy conversation + tool_calls into per-surface tables');
		d.exec('BEGIN');
		try {
			// All three migration INSERTs filter out NULL ts_unix rows. The
			// surface tables declare ts_unix REAL NOT NULL, so a legacy row
			// missing it would trip a NOT NULL constraint mid-transaction and
			// roll the whole migration back — leaving the legacy tables in
			// place and the bot logging the same error every restart. Rows
			// with no timestamp can't be meaningfully recovered (no other
			// column carries time), so dropping them on migrate is the
			// right call.
			if (hasConversation && voiceEmpty) {
				// Utterances → voice. Roles that map to voice: 'user', 'assistant',
				// 'sutando', 'core-agent', 'SESSION_END', and anything not prefixed
				// 'phone-'. kind normalization: user/assistant/sutando → user/agent,
				// others passthrough.
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
					WHERE ts_unix IS NOT NULL
					  AND role NOT LIKE 'phone-%'
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
					FROM conversation WHERE ts_unix IS NOT NULL AND role LIKE 'phone-%'
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

/** Drop the redundant sessions.tool_calls / sessions.events JSON columns if a
 *  pre-#1052 db still has them. The atom rows now live in surface tables
 *  (kind='tool_call') and session_events respectively; the JSON cols were
 *  triple-encoding the same data. SQLite 3.35+ supports ALTER TABLE DROP
 *  COLUMN — guard via pragma_table_info so re-running this is a no-op. Each
 *  drop keeps its own independent error handling: one failing must not prevent
 *  the other from being attempted. */
function dropObsoleteSessionColumns(db: DatabaseSync): void {
	// The schema probe itself must not escape. This runner documents a
	// best-effort contract ("every failure is logged and swallowed"), and the
	// caller marks the whole store `initFailed` on any exception out of here —
	// so an unguarded PRAGMA would turn a metadata read error into "all
	// conversation and session recording is silently off for the process
	// lifetime" (CR #2541, qingyun-wu). If we cannot read the schema we cannot
	// know which columns exist, so log and skip the drops; they are re-attempted
	// on the next start.
	let sessionCols: Set<string>;
	try {
		sessionCols = new Set(
			(db.prepare("PRAGMA table_info(sessions)").all() as Array<{ name: string }>)
				.map(c => c.name),
		);
	} catch (e) {
		console.error('[conversation-store] could not read sessions schema; skipping obsolete-column drops:', e);
		return;
	}
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
}
