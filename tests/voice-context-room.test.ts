import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Room-bound voice (Zerlinda 2026-09-17: spoken in a room, answered in the
// DM). The instructions carry a ROOM line whenever the live session is docked
// in a room, so the model knows where delegated work answers: the room when it
// is for the room's members, the owner's DM otherwise (a `[dm-only]` result).

// Hermetic: memory and workspace are tmp dirs, set BEFORE the source-ordered
// import binds voice-context's module-level paths.
const TMP = mkdtempSync(join(tmpdir(), 'sutando-voice-context-room-'));
const MEMORY = join(TMP, 'memory');
mkdirSync(MEMORY, { recursive: true });
process.env.SUTANDO_MEMORY_DIR = MEMORY;
process.env.SUTANDO_WORKSPACE = join(TMP, 'workspace');
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(process.env.SUTANDO_WORKSPACE, { recursive: true });

const { buildVoiceAgentContext } = await import('../src/voice-context.js');

after(() => {
	try { rmSync(TMP, { recursive: true, force: true }); } catch {}
});

const roomLine = (ctx: string) => ctx.split('\n').filter(l => l.startsWith('ROOM:'));

describe('buildVoiceAgentContext({ room }) — the ROOM line', () => {
	it('names the room and its id, and states where delegated work answers', () => {
		const ctx = buildVoiceAgentContext({ room: { id: '!abc123:ag2.space', name: 'Commorai' } });
		const lines = roomLine(ctx);
		assert.equal(lines.length, 1, 'exactly one ROOM line');
		assert.match(lines[0], /docked in room "Commorai" \(!abc123:ag2\.space\)/);
		assert.match(lines[0], /answered there only when it is for the room's members, otherwise in the owner's DM; say where it went/);
		assert.doesNotMatch(lines[0], /never "in your DM"/);
	});

	it('falls back to the id when the room has no name', () => {
		const ctx = buildVoiceAgentContext({ room: { id: '!abc123:ag2.space' } });
		assert.match(roomLine(ctx)[0], /docked in room !abc123:ag2\.space\./);
		assert.doesNotMatch(roomLine(ctx)[0], /""/);
	});

	it('emits no ROOM line for a DM session (null, undefined, or no options at all)', () => {
		assert.deepEqual(roomLine(buildVoiceAgentContext({ room: null })), []);
		assert.deepEqual(roomLine(buildVoiceAgentContext({})), []);
		assert.deepEqual(roomLine(buildVoiceAgentContext()), []);
	});

	it('the ROOM line comes after USER CONTEXT when a profile exists', () => {
		writeFileSync(join(MEMORY, 'user_profile.md'), '# Owner\n\nRuns a small company.\n');
		const ctx = buildVoiceAgentContext({ room: { id: '!abc123:ag2.space', name: 'Commorai' } });
		const lines = ctx.split('\n');
		const user = lines.indexOf('USER CONTEXT:');
		const room = lines.findIndex(l => l.startsWith('ROOM:'));
		assert.ok(user >= 0 && room > user, `USER CONTEXT at ${user}, ROOM at ${room}`);
		rmSync(join(MEMORY, 'user_profile.md'));
	});
});
