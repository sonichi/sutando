// Direct fixture coverage for the extracted startup migration policy
// (src/conversation-store-migrations.ts).
//
// Every case builds a disposable SQLite database, runs the exported entry
// point against it, and then QUERIES tables/columns/rows — no assertions on
// function names or copied SQL text. Nothing here touches the owner's
// workspace database: each fixture gets its own file under a fresh mkdtemp.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { runConversationStoreMigrations } from '../src/conversation-store-migrations.js';

let n = 0;
function freshDb(): DatabaseSync {
	const p = join(mkdtempSync(join(tmpdir(), 'sutando-migr-')), `f${n++}.sqlite`);
	return new DatabaseSync(p);
}

/** The current-schema DDL the migration module requires as a precondition. It
 *  never creates these itself — the store owns current DDL. */
function currentSchema(d: DatabaseSync, sessionsExtraCols = ''): void {
	for (const t of ['voice', 'phone']) {
		d.exec(`CREATE TABLE ${t} (
			id INTEGER PRIMARY KEY, ts_unix REAL NOT NULL, kind TEXT NOT NULL,
			text TEXT, duration_ms INTEGER, session_id TEXT
		)`);
	}
	d.exec(`CREATE TABLE sessions (
		id INTEGER PRIMARY KEY, ts_unix REAL NOT NULL, source TEXT, session_id TEXT,
		call_sid TEXT, caller TEXT, is_owner INTEGER, is_meeting INTEGER,
		duration_ms INTEGER, transcript_lines INTEGER, tool_count INTEGER,
		pending_tasks TEXT${sessionsExtraCols}
	)`);
	d.exec(`CREATE TABLE session_events (
		id INTEGER PRIMARY KEY, ts_unix REAL NOT NULL, source TEXT,
		session_id TEXT, call_sid TEXT, event_name TEXT NOT NULL
	)`);
}

function legacyConversation(d: DatabaseSync): void {
	d.exec(`CREATE TABLE conversation (
		id INTEGER PRIMARY KEY, ts_unix REAL, role TEXT, text TEXT, session_id TEXT
	)`);
}
function legacyToolCalls(d: DatabaseSync): void {
	d.exec(`CREATE TABLE tool_calls (
		id INTEGER PRIMARY KEY, ts_unix REAL, source TEXT, name TEXT,
		duration_ms INTEGER, session_id TEXT
	)`);
}

const tables = (d: DatabaseSync): string[] =>
	(d.prepare("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").all() as Array<{ name: string }>)
		.map(r => r.name);
const cols = (d: DatabaseSync, t: string): string[] =>
	(d.prepare(`PRAGMA table_info(${t})`).all() as Array<{ name: string }>).map(c => c.name);
// SQLite rows are open-shaped; `unknown` values force an explicit read at each
// use-site rather than silently trusting a column's type.
type Row = Record<string, unknown>;
const rows = (d: DatabaseSync, t: string): Row[] =>
	d.prepare(`SELECT * FROM ${t} ORDER BY id`).all() as Row[];
const count = (d: DatabaseSync, t: string): number =>
	(d.prepare(`SELECT count(*) AS c FROM ${t}`).get() as { c: number }).c;

// ── 1. no legacy tables → no-op ──────────────────────────────────────────────
test('no legacy tables: no-op, current tables unchanged', () => {
	const d = freshDb();
	currentSchema(d);
	const before = tables(d);
	runConversationStoreMigrations(d);
	assert.deepEqual(tables(d), before);
	assert.equal(count(d, 'voice'), 0);
	assert.equal(count(d, 'phone'), 0);
});

// ── 2. legacy conversation only ──────────────────────────────────────────────
test('legacy conversation only: voice + phone rows copied with exact kinds, table dropped', () => {
	const d = freshDb();
	currentSchema(d);
	legacyConversation(d);
	const ins = d.prepare('INSERT INTO conversation (ts_unix, role, text, session_id) VALUES (?,?,?,?)');
	ins.run(100, 'user', 'hello', 's1');
	ins.run(101, 'assistant', 'hi', 's1');
	ins.run(102, 'sutando', 'yes', 's1');
	ins.run(103, 'core-agent', 'note', 's1');       // passthrough kind
	ins.run(104, 'SESSION_END', '', 's1');           // passthrough kind
	ins.run(200, 'phone-caller', 'ring', 'p1');
	ins.run(201, 'phone-agent', 'speaking', 'p1');
	ins.run(202, 'phone-weird', 'other', 'p1');      // substr(role,7) => 'weird'

	runConversationStoreMigrations(d);

	assert.ok(!tables(d).includes('conversation'), 'legacy table dropped');
	assert.deepEqual(rows(d, 'voice').map(r => [r.ts_unix, r.kind, r.text]), [
		[100, 'user', 'hello'],
		[101, 'agent', 'hi'],
		[102, 'agent', 'yes'],
		[103, 'core-agent', 'note'],
		[104, 'SESSION_END', ''],
	]);
	assert.deepEqual(rows(d, 'phone').map(r => [r.ts_unix, r.kind, r.text]), [
		[200, 'user', 'ring'],
		[201, 'agent', 'speaking'],
		[202, 'weird', 'other'],
	]);
	// session_id and duration_ms mapping
	assert.equal(rows(d, 'voice')[0].session_id, 's1');
	assert.equal(rows(d, 'voice')[0].duration_ms, null);
});

// ── 3. legacy tool_calls only ────────────────────────────────────────────────
test('legacy tool_calls only: rows routed by source, table dropped', () => {
	const d = freshDb();
	currentSchema(d);
	legacyToolCalls(d);
	const ins = d.prepare('INSERT INTO tool_calls (ts_unix, source, name, duration_ms, session_id) VALUES (?,?,?,?,?)');
	ins.run(300, 'voice', 'search', 42, 's1');
	ins.run(301, 'phone', 'hang_up', 7, 'p1');
	ins.run(302, 'other', 'ignored', 1, 'x1');   // neither surface

	runConversationStoreMigrations(d);

	assert.ok(!tables(d).includes('tool_calls'));
	assert.deepEqual(rows(d, 'voice').map(r => [r.ts_unix, r.kind, r.text, r.duration_ms]),
		[[300, 'tool_call', 'search', 42]]);
	assert.deepEqual(rows(d, 'phone').map(r => [r.ts_unix, r.kind, r.text, r.duration_ms]),
		[[301, 'tool_call', 'hang_up', 7]]);
});

// ── 4. both legacy tables ────────────────────────────────────────────────────
test('both legacy tables: all transformations occur and both are dropped', () => {
	const d = freshDb();
	currentSchema(d);
	legacyConversation(d);
	legacyToolCalls(d);
	d.prepare('INSERT INTO conversation (ts_unix, role, text, session_id) VALUES (?,?,?,?)').run(100, 'user', 'u', 's1');
	d.prepare('INSERT INTO conversation (ts_unix, role, text, session_id) VALUES (?,?,?,?)').run(200, 'phone-caller', 'c', 'p1');
	d.prepare('INSERT INTO tool_calls (ts_unix, source, name, duration_ms, session_id) VALUES (?,?,?,?,?)').run(300, 'voice', 't', 5, 's1');

	runConversationStoreMigrations(d);

	const t = tables(d);
	assert.ok(!t.includes('conversation') && !t.includes('tool_calls'));
	assert.equal(count(d, 'voice'), 2);   // utterance + tool call
	assert.equal(count(d, 'phone'), 1);
});

// ── 5. both surfaces already populated ───────────────────────────────────────
test('both current surfaces populated: no backfill, legacy tables still dropped', () => {
	const d = freshDb();
	currentSchema(d);
	legacyConversation(d);
	legacyToolCalls(d);
	d.prepare('INSERT INTO voice (ts_unix, kind, text) VALUES (?,?,?)').run(1, 'user', 'existing');
	d.prepare('INSERT INTO phone (ts_unix, kind, text) VALUES (?,?,?)').run(2, 'user', 'existing');
	d.prepare('INSERT INTO conversation (ts_unix, role, text) VALUES (?,?,?)').run(100, 'user', 'legacy');

	runConversationStoreMigrations(d);

	const t = tables(d);
	assert.ok(!t.includes('conversation') && !t.includes('tool_calls'), 'legacy cleaned up');
	assert.equal(count(d, 'voice'), 1, 'no backfill into a populated surface');
	assert.equal(count(d, 'phone'), 1);
});

// ── 6. asymmetric: one surface empty, one populated ──────────────────────────
test('one surface populated, one empty: only the empty one is backfilled', () => {
	const d = freshDb();
	currentSchema(d);
	legacyConversation(d);
	d.prepare('INSERT INTO voice (ts_unix, kind, text) VALUES (?,?,?)').run(1, 'user', 'existing');
	d.prepare('INSERT INTO conversation (ts_unix, role, text) VALUES (?,?,?)').run(100, 'user', 'legacy-voice');
	d.prepare('INSERT INTO conversation (ts_unix, role, text) VALUES (?,?,?)').run(200, 'phone-caller', 'legacy-phone');

	runConversationStoreMigrations(d);

	// voice was non-empty so its backfill is skipped; phone was empty so it fills.
	assert.deepEqual(rows(d, 'voice').map(r => r.text), ['existing']);
	assert.deepEqual(rows(d, 'phone').map(r => r.text), ['legacy-phone']);
	assert.ok(!tables(d).includes('conversation'));
});

// ── 7. null timestamp row ────────────────────────────────────────────────────
test('legacy row with NULL ts_unix is dropped, not migrated, and does not abort', () => {
	const d = freshDb();
	currentSchema(d);
	legacyConversation(d);
	d.prepare('INSERT INTO conversation (ts_unix, role, text) VALUES (?,?,?)').run(null, 'user', 'no-timestamp');
	d.prepare('INSERT INTO conversation (ts_unix, role, text) VALUES (?,?,?)').run(100, 'user', 'kept');

	runConversationStoreMigrations(d);

	assert.deepEqual(rows(d, 'voice').map(r => r.text), ['kept']);
	assert.ok(!tables(d).includes('conversation'), 'migration still completed');
});

// ── 8. forced failure rolls back ─────────────────────────────────────────────
test('forced migration failure: transaction rolls back, legacy tables and rows survive', () => {
	const d = freshDb();
	currentSchema(d);
	legacyConversation(d);
	d.prepare('INSERT INTO conversation (ts_unix, role, text) VALUES (?,?,?)').run(100, 'user', 'legacy');
	// Force the INSERT to fail mid-transaction: make `voice` reject the write.
	d.exec('CREATE TRIGGER voice_boom BEFORE INSERT ON voice BEGIN SELECT RAISE(ABORT, "boom"); END');

	runConversationStoreMigrations(d);   // must swallow, not throw

	assert.ok(tables(d).includes('conversation'), 'legacy table survives rollback');
	assert.equal(count(d, 'conversation'), 1, 'legacy rows survive rollback');
	assert.equal(count(d, 'voice'), 0, 'no partial write committed');
});

// ── 9-11. obsolete sessions columns ──────────────────────────────────────────
test('old sessions with BOTH obsolete columns: both dropped, other columns and rows survive', () => {
	const d = freshDb();
	currentSchema(d, ', tool_calls TEXT, events TEXT');
	d.prepare('INSERT INTO sessions (ts_unix, source, session_id, tool_calls, events) VALUES (?,?,?,?,?)')
		.run(1, 'voice', 's1', '[]', '[]');

	runConversationStoreMigrations(d);

	const c = cols(d, 'sessions');
	assert.ok(!c.includes('tool_calls') && !c.includes('events'));
	for (const keep of ['ts_unix', 'source', 'session_id', 'duration_ms', 'pending_tasks']) {
		assert.ok(c.includes(keep), `${keep} survived`);
	}
	assert.equal(count(d, 'sessions'), 1, 'row survived');
	assert.equal(rows(d, 'sessions')[0].session_id, 's1');
});

test('old sessions with only ONE obsolete column: only that one is dropped', () => {
	const d = freshDb();
	currentSchema(d, ', events TEXT');
	runConversationStoreMigrations(d);
	const c = cols(d, 'sessions');
	assert.ok(!c.includes('events'));
	assert.ok(c.includes('pending_tasks'));
});

test('current sessions schema: no-op', () => {
	const d = freshDb();
	currentSchema(d);
	const before = cols(d, 'sessions');
	runConversationStoreMigrations(d);
	assert.deepEqual(cols(d, 'sessions'), before);
});

// ── fault: the schema probe must not escape (CR #2541) ──────────────────────
test('a failing sessions schema probe is swallowed, not propagated', () => {
	const d = freshDb();
	currentSchema(d);
	const realPrepare = d.prepare.bind(d);
	// Fault-inject the probe only; everything else behaves normally.
	(d as unknown as { prepare: (sql: string) => unknown }).prepare = (sql: string) => {
		if (sql.includes('PRAGMA table_info(sessions)')) throw new Error('session-probe-boom');
		return realPrepare(sql);
	};
	// Must NOT throw. The caller marks the whole store initFailed on any escape,
	// which would silently disable ALL conversation/session recording for the
	// process lifetime — the runner's contract is best-effort.
	assert.doesNotThrow(() => runConversationStoreMigrations(d));
});

test('a failing legacy-table probe is also swallowed', () => {
	const d = freshDb();
	currentSchema(d);
	const realPrepare = d.prepare.bind(d);
	(d as unknown as { prepare: (sql: string) => unknown }).prepare = (sql: string) => {
		if (sql.includes('sqlite_master')) throw new Error('legacy-probe-boom');
		return realPrepare(sql);
	};
	assert.doesNotThrow(() => runConversationStoreMigrations(d));
});

// ── 12. idempotence ──────────────────────────────────────────────────────────
test('second invocation is idempotent and does not duplicate rows', () => {
	const d = freshDb();
	currentSchema(d);
	legacyConversation(d);
	legacyToolCalls(d);
	d.prepare('INSERT INTO conversation (ts_unix, role, text) VALUES (?,?,?)').run(100, 'user', 'u');
	d.prepare('INSERT INTO tool_calls (ts_unix, source, name, duration_ms) VALUES (?,?,?,?)').run(300, 'voice', 't', 5);

	runConversationStoreMigrations(d);
	const firstVoice = rows(d, 'voice');
	const firstPhone = rows(d, 'phone');

	runConversationStoreMigrations(d);   // second run

	assert.deepEqual(rows(d, 'voice'), firstVoice, 'no duplicate voice rows');
	assert.deepEqual(rows(d, 'phone'), firstPhone, 'no duplicate phone rows');
});

// ── 13. compatibility view is not mistaken for a legacy table ────────────────
test('a `conversation` VIEW created after migration is not treated as a legacy table', () => {
	const d = freshDb();
	currentSchema(d);
	legacyConversation(d);
	d.prepare('INSERT INTO conversation (ts_unix, role, text) VALUES (?,?,?)').run(100, 'user', 'u');
	runConversationStoreMigrations(d);
	assert.equal(count(d, 'voice'), 1);

	// The store rebuilds a compatibility `conversation` VIEW after migrating.
	d.exec(`CREATE VIEW conversation AS
		SELECT ts_unix, kind AS role, text, session_id FROM voice`);

	runConversationStoreMigrations(d);   // next startup

	assert.equal(count(d, 'voice'), 1, 'view did not trigger a second backfill');
	const views = (d.prepare("SELECT name FROM sqlite_master WHERE type='view'").all() as Array<{ name: string }>)
		.map(r => r.name);
	assert.ok(views.includes('conversation'), 'view was not dropped as if it were a legacy table');
});
