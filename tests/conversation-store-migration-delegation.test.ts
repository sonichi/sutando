// Guards the conversation-store → conversation-store-migrations boundary.
//
// 1. The store must actually invoke the migration module (not keep a drifting
//    copy), and must invoke it AFTER current-table DDL and BEFORE views /
//    prepared statements — the ordering the migration's preconditions rely on.
// 2. Legacy-table migration SQL must not reappear inside the live recording
//    functions. The scan is deliberately narrow: it flags the legacy table
//    names and the destructive/transaction verbs, NOT current-schema DDL, so
//    `CREATE TABLE voice ...` and friends stay legal in the store.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..');
const store = readFileSync(join(REPO, 'src/conversation-store.ts'), 'utf8');

test('the store delegates to the migration module', () => {
	assert.match(store, /import \{ runConversationStoreMigrations \} from '\.\/conversation-store-migrations\.js'/);
	assert.match(store, /runConversationStoreMigrations\(db\)/);
});

test('migrations run after current DDL and before views + prepared statements', () => {
	const ddl = store.indexOf('CREATE INDEX IF NOT EXISTS idx_session_events_session');
	const migrate = store.indexOf('runConversationStoreMigrations(db)');
	const views = store.indexOf('rebuildViews(db)', migrate);
	const stmts = store.indexOf('sessionInsertStmt = db.prepare');
	assert.ok(ddl > 0 && migrate > 0 && views > 0 && stmts > 0, 'all four anchors present');
	assert.ok(ddl < migrate, 'current table DDL precedes migrations');
	assert.ok(migrate < views, 'migrations precede view rebuild');
	assert.ok(migrate < stmts, 'migrations precede statement preparation');
});

test('legacy migration SQL is not reintroduced into the store', () => {
	// Strip comments — the store legitimately *describes* the old schema in its
	// header docs; only executable references matter.
	const code = store
		.replace(/\/\*[\s\S]*?\*\//g, '')
		.split('\n')
		.filter(l => !l.trim().startsWith('//') && !l.trim().startsWith('*'))
		.join('\n');

	// Token-specific: the legacy table names, and only in a SQL context.
	for (const pattern of [
		/DROP TABLE IF EXISTS conversation/i,
		/DROP TABLE IF EXISTS tool_calls/i,
		/FROM conversation\b/i,
		/FROM tool_calls\b/i,
		/ALTER TABLE sessions DROP COLUMN/i,
	]) {
		assert.ok(!pattern.test(code), `store must not contain migration SQL: ${pattern}`);
	}

	// Transaction control belongs to the migration module now.
	assert.ok(!/\.exec\('BEGIN'\)/.test(code), 'store must not open a migration transaction');
	assert.ok(!/\.exec\('ROLLBACK'\)/.test(code), 'store must not roll back a migration');
});

test('current-schema DDL is still allowed in the store (guard is not over-broad)', () => {
	// If this fails, the scan above has become too aggressive and would block
	// legitimate table creation.
	assert.match(store, /CREATE TABLE IF NOT EXISTS sessions/);
	assert.match(store, /CREATE TABLE IF NOT EXISTS session_events/);
});
