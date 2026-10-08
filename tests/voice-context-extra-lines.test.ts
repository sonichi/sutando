import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Optional skills contribute context lines about where the session is; the core
// renders them after USER CONTEXT and knows nothing about what they say.

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

const LINE = 'WHERE: the session is in the lobby.';
const whereLines = (ctx: string) => ctx.split('\n').filter(l => l.startsWith('WHERE:'));

describe('buildVoiceAgentContext({ extraLines })', () => {
	it('renders contributed lines verbatim, once', () => {
		assert.deepEqual(whereLines(buildVoiceAgentContext({ extraLines: [LINE] })), [LINE]);
	});

	it('adds nothing by default: no options, no lines, or an empty list', () => {
		const plain = buildVoiceAgentContext();
		assert.equal(buildVoiceAgentContext({}), plain);
		assert.equal(buildVoiceAgentContext({ extraLines: [] }), plain);
		assert.deepEqual(whereLines(plain), []);
	});

	it('the lines come after USER CONTEXT when a profile exists, followed by a blank line', () => {
		writeFileSync(join(MEMORY, 'user_profile.md'), '# Owner\n\nRuns a small company.\n');
		const lines = buildVoiceAgentContext({ extraLines: [LINE] }).split('\n');
		const user = lines.indexOf('USER CONTEXT:');
		const where = lines.indexOf(LINE);
		assert.ok(user >= 0 && where > user, `USER CONTEXT at ${user}, line at ${where}`);
		assert.equal(lines[where + 1], '');
		rmSync(join(MEMORY, 'user_profile.md'));
	});
});
