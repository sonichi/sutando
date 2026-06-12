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

	it('two surfaces hold independent sentinels (Mini amendment #3) — both toggled, both orders, exact contents', () => {
		const aPath = join(tmp, 'a', 'voice-mode.txt');
		const bPath = join(tmp, 'b', 'dvoice-mode-123.txt');
		const a = createModeState({ sentinelPath: aPath });
		const b = createModeState({ sentinelPath: bPath });

		// order 1: a → meeting while b → active
		a.setMeeting(true);
		b.setMeeting(false);
		assert.equal(a.isMeeting(), true);
		assert.equal(b.isMeeting(), false);
		assert.equal(readFileSync(aPath, 'utf-8'), 'meeting');
		assert.equal(readFileSync(bPath, 'utf-8'), 'active');

		// order 2: b → meeting must NOT clobber a's sentinel (the multi-surface
		// bug this design exists to prevent), then a → active leaves b alone
		b.setMeeting(true);
		assert.equal(readFileSync(aPath, 'utf-8'), 'meeting', 'plugin surface write must not touch desktop sentinel');
		assert.equal(readFileSync(bPath, 'utf-8'), 'meeting');
		a.setMeeting(false);
		assert.equal(readFileSync(aPath, 'utf-8'), 'active');
		assert.equal(readFileSync(bPath, 'utf-8'), 'meeting', 'desktop surface write must not touch plugin sentinel');
		assert.equal(a.isMeeting(), false);
		assert.equal(b.isMeeting(), true);
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

	it('recording e2e: REGISTRATION drives routing — the engine has no built-in plugin table', async () => {
		const { DatabaseSync } = await import('node:sqlite');

		// Pre-registration: the engine must NOT know discord_voice. A discord-*
		// role falls through to the host voice table, and no discord_voice
		// table exists. (Mini review 2026-06-12: previously this passed
		// vacuously because init() still owned the table.)
		recordConversation('discord-user', 'pre-registration row', 'sess-pre');
		const probe = new DatabaseSync(process.env.SUTANDO_CONVERSATION_DB!);
		assert.equal(
			probe.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='discord_voice'").get(),
			undefined, 'engine must not create plugin tables on its own');
		assert.ok(
			probe.prepare("SELECT text FROM voice WHERE session_id = 'sess-pre'").get(),
			'unregistered role prefix falls through to host voice table');

		// Register the surface the way the plugin does at startup.
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
		`, { source: 'discord-voice', table: 'discord_voice', rolePrefix: 'discord-' });
		assert.equal(ok, true);

		// Post-registration: discord-* routes to the plugin table, speaker meta
		// persists into the columns the table declares, and the compat view
		// includes the new surface.
		recordConversation('discord-user', 'za warudo e2e row', 'sess-e2e', { speakerId: 'u1', speakerName: 'Susan' });
		const db = new DatabaseSync(process.env.SUTANDO_CONVERSATION_DB!);
		const row = db.prepare("SELECT text, kind, speaker_name FROM discord_voice WHERE session_id = 'sess-e2e'").get() as any;
		assert.ok(row, 'row should exist in plugin-registered table');
		assert.equal(row.text, 'za warudo e2e row');
		assert.equal(row.kind, 'user');
		assert.equal(row.speaker_name, 'Susan');
		const viaView = db.prepare("SELECT role FROM conversation WHERE session_id = 'sess-e2e'").get() as any;
		assert.equal(viaView?.role, 'user', 'compat view rebuilt to include registered surface');
	});

	it('prompt strings match the tuned production text (independent pins — Mini finding 4)', async () => {
		// Literal copies pinned HERE, not imported — if anyone edits prompts.ts,
		// this fails instead of drifting along with it. Wording changes require
		// a live test round AND a deliberate re-pin in this file.
		const prompts = await import('../src/voice-core/prompts.js');
		assert.equal(
			prompts.MEETING_ENTER_INSTRUCTION,
			'You are now in meeting mode. Listen and track the discussion internally. Produce ZERO audio output unless someone says "Sutando." The ONLY tool you may call unprompted is save_meeting_note — call it every 5-10 minutes to capture key decisions, action items, and discussion points. When you exit meeting mode, call save_meeting_note with type "summary" for a final recap. Do not call work or any other tools unless explicitly addressed.',
		);
		assert.equal(
			prompts.ACTIVE_ENTER_INSTRUCTION,
			'Back to active mode. You can speak and use all tools normally.',
		);
		assert.ok(prompts.RULE_MEETING_ACTIVE.startsWith('⚠️ MEETING MODE IS CURRENTLY ACTIVE. You are an invisible note-taker.'));
		assert.ok(prompts.RULE_MEETING_ACTIVE.endsWith('call switch_mode("active") and save_meeting_note(summary).'));
		assert.equal(prompts.RULE_WHEN_IN_DOUBT, '- When in doubt, call work.');
		assert.ok(prompts.SWITCH_MODE_DESCRIPTION.includes('Call switch_mode("meeting") when user says "take notes", "be silent", "meeting mode", "passive mode", or joins a meeting.'));
	});
});
