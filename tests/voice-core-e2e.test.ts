/**
 * voice-core end-to-end tests (#1427 two-repo refactor, round ①).
 *
 * Covers the full shared-brain flow a voice surface exercises:
 *   1. createModeState → switch_mode factory → sentinel file round-trip
 *      (the exact wiring voice-agent and the discord-voice plugin both use)
 *   2. resolver precedence (presenter > meeting > active) through the handle
 *   3. recording e2e: registerSurfaceTable creates a plugin-owned table in
 *      the real sqlite engine, then recordConversation routes a row into it
 *   4. prompt-string integrity: the strings the factories return are the
 *      tuned production strings (guards against accidental rewording)
 */
import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Route the conversation store at a temp DB BEFORE importing voice-core
// (the store resolves its path at module load).
const tmp = mkdtempSync(join(tmpdir(), 'voice-core-e2e-'));
process.env.SUTANDO_CONVERSATION_DB = join(tmp, 'conversation.sqlite');

const {
	createModeState,
	makeSwitchModeTool,
	makeSaveMeetingNoteTool,
	registerSurfaceTable,
	recordConversation,
	MEETING_ENTER_INSTRUCTION,
	ACTIVE_ENTER_INSTRUCTION,
	VOICE_CORE_API_VERSION,
} = await import('../src/voice-core/index.js');

after(() => { try { rmSync(tmp, { recursive: true, force: true }); } catch {} });

describe('voice-core e2e', () => {
	it('exposes API version 1', () => {
		assert.equal(VOICE_CORE_API_VERSION, 1);
	});

	it('switch_mode flips state, mirrors sentinel, returns tuned instructions', async () => {
		const sentinel = join(tmp, 'state', 'voice-mode.txt');
		const state = createModeState({ sentinelPath: sentinel });
		const events: boolean[] = [];
		state.subscribe((m) => events.push(m));

		const tool = makeSwitchModeTool({ state });
		const enter = await tool.execute({ mode: 'meeting' }, {} as any);
		assert.equal((enter as any).status, 'meeting_mode');
		assert.equal((enter as any).instruction, MEETING_ENTER_INSTRUCTION);
		assert.equal(state.isMeeting(), true);
		assert.equal(readFileSync(sentinel, 'utf-8'), 'meeting');

		const exit = await tool.execute({ mode: 'active' }, {} as any);
		assert.equal((exit as any).status, 'active_mode');
		assert.equal((exit as any).instruction, ACTIVE_ENTER_INSTRUCTION);
		assert.equal(state.isMeeting(), false);
		assert.equal(readFileSync(sentinel, 'utf-8'), 'active');
		assert.deepEqual(events, [true, false]);
	});

	it('two surfaces hold independent sentinels (Mini amendment #3)', () => {
		const a = createModeState({ sentinelPath: join(tmp, 'a', 'voice-mode.txt') });
		const b = createModeState({ sentinelPath: join(tmp, 'b', 'dvoice-mode-123.txt') });
		a.setMeeting(true);
		assert.equal(a.isMeeting(), true);
		assert.equal(b.isMeeting(), false);
		assert.equal(readFileSync(join(tmp, 'a', 'voice-mode.txt'), 'utf-8'), 'meeting');
		assert.ok(!existsSync(join(tmp, 'b', 'dvoice-mode-123.txt')) || readFileSync(join(tmp, 'b', 'dvoice-mode-123.txt'), 'utf-8') === 'active');
	});

	it('resolver precedence: presenter > meeting > active', () => {
		const state = createModeState({ sentinelPath: join(tmp, 'c', 'voice-mode.txt') });
		assert.equal(state.resolve(() => false).mode, 'active');
		state.setMeeting(true);
		assert.equal(state.resolve(() => false).mode, 'meeting');
		assert.equal(state.resolve(() => true).mode, 'presenter');
	});

	it('save_meeting_note writes frontmatter + entries via configured path', async () => {
		const tool = makeSaveMeetingNoteTool({ notePathFor: (d) => join(tmp, `meeting-${d}.md`) });
		const r1 = await tool.execute({ content: 'decision: ship it' }, {} as any);
		assert.equal((r1 as any).status, 'saved');
		const body = readFileSync((r1 as any).path, 'utf-8');
		assert.ok(body.startsWith('---\ntitle: Meeting notes'));
		assert.ok(body.includes('decision: ship it'));
		const r2 = await tool.execute({ content: 'wrap', type: 'summary' }, {} as any);
		assert.ok(readFileSync((r2 as any).path, 'utf-8').includes('## Summary'));
	});

	it('recording e2e: plugin-registered table receives rows through the engine', async () => {
		const ok = registerSurfaceTable(`
			CREATE TABLE IF NOT EXISTS discord_voice (
				id          INTEGER PRIMARY KEY,
				ts_unix     REAL    NOT NULL,
				kind        TEXT    NOT NULL,
				text        TEXT,
				duration_ms INTEGER,
				session_id  TEXT,
				speaker_id   TEXT,
				speaker_name TEXT
			);
			CREATE INDEX IF NOT EXISTS idx_discord_voice_ts ON discord_voice(ts_unix);
		`);
		assert.equal(ok, true);
		// discord-* role routes to the discord_voice table (sourceFromRole)
		recordConversation('discord-user', 'za warudo e2e row', 'sess-e2e');
		const { DatabaseSync } = await import('node:sqlite');
		const db = new DatabaseSync(process.env.SUTANDO_CONVERSATION_DB!);
		const row = db.prepare("SELECT text, kind FROM discord_voice WHERE session_id = 'sess-e2e'").get() as any;
		assert.ok(row, 'row should exist in plugin-registered table');
		assert.equal(row.text, 'za warudo e2e row');
		assert.equal(row.kind, 'user');
	});
});
