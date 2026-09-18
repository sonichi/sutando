import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// The language rule (CLAUDE.md Operating Style): a stated preference is
// written to user_profile.md as `Language: <name>` and kept. The voice prompt
// reads it back so the session opens in the owner's language.

// Hermetic: memory and workspace are tmp dirs, set BEFORE the source-ordered
// import binds voice-context's module-level paths.
const TMP = mkdtempSync(join(tmpdir(), 'sutando-voice-context-language-'));
const MEMORY = join(TMP, 'memory');
mkdirSync(MEMORY, { recursive: true });
process.env.SUTANDO_MEMORY_DIR = MEMORY;
process.env.SUTANDO_WORKSPACE = join(TMP, 'workspace');
process.env.SUTANDO_TEST_MODE = '1';
mkdirSync(process.env.SUTANDO_WORKSPACE, { recursive: true });

const { pickLanguage, buildVoiceAgentContext } = await import('../src/voice-context.js');

after(() => {
	try { rmSync(TMP, { recursive: true, force: true }); } catch {}
});

const languageLines = (ctx: string) => ctx.split('\n').filter(l => l.startsWith('LANGUAGE:'));

describe('pickLanguage (pure) — the Language: line of user_profile.md', () => {
	it('reads a bare, bulleted or bold-keyed line, case-insensitively', () => {
		assert.equal(pickLanguage('Name: Zerlinda\nLanguage: Chinese\nRole: founder'), 'Chinese');
		assert.equal(pickLanguage('- language: French'), 'French');
		assert.equal(pickLanguage('* **Language**: German'), 'German');
		assert.equal(pickLanguage('**Language:** Brazilian Portuguese'), 'Brazilian Portuguese');
		assert.equal(pickLanguage('LANGUAGE: 中文'), '中文');
	});

	it('keeps one short phrase: stops at a sentence break or parenthesis, caps the length', () => {
		assert.equal(pickLanguage('Language: Spanish. Prefers informal tone.'), 'Spanish');
		assert.equal(pickLanguage('Language: Japanese (switches to English for code)'), 'Japanese');
		assert.equal(pickLanguage('Language: ' + 'x'.repeat(100)), 'x'.repeat(40));
	});

	it('returns null when there is no such line, or the value is empty', () => {
		assert.equal(pickLanguage(''), null);
		assert.equal(pickLanguage('Name: Zerlinda\nSpeaks three languages fluently.'), null, 'prose mentioning languages is not a preference');
		assert.equal(pickLanguage('Programming language: Rust'), null, 'a different key that merely ends in the word');
		assert.equal(pickLanguage('Language:'), null);
		assert.equal(pickLanguage('Language: .'), null);
	});

	it('the first Language: line wins', () => {
		assert.equal(pickLanguage('Language: Chinese\n\nLanguage: English'), 'Chinese');
	});
});

describe('buildVoiceAgentContext — the LANGUAGE line', () => {
	it('names the stated language and the one-clause rule for English', () => {
		writeFileSync(join(MEMORY, 'user_profile.md'), '# Owner\n\nLanguage: Chinese\nRuns Commorai.\n');
		const ctx = buildVoiceAgentContext();
		const lines = languageLines(ctx);
		assert.equal(lines.length, 1, 'exactly one LANGUAGE line');
		assert.match(lines[0], /stated language is Chinese\. Speak Chinese unless they ask to switch/);
		assert.match(lines[0], /answer in English .* say so in one clause/);
		const all = ctx.split('\n');
		assert.ok(all.indexOf('USER CONTEXT:') < all.findIndex(l => l.startsWith('LANGUAGE:')), 'LANGUAGE follows the profile it came from');
	});

	it('emits no LANGUAGE line when the profile states none, or there is no profile', () => {
		writeFileSync(join(MEMORY, 'user_profile.md'), '# Owner\n\nRuns Commorai.\n');
		assert.deepEqual(languageLines(buildVoiceAgentContext()), []);
		rmSync(join(MEMORY, 'user_profile.md'));
		assert.deepEqual(languageLines(buildVoiceAgentContext()), []);
	});
});
