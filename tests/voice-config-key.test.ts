import { describe, it, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';

const {
	VOICE_CONFIG_DEFAULTS,
	loadVoiceConfig,
	resolveOwnerMode,
} = await import('../src/voice-config.js');

const { voiceApiKey } = await import('../src/voice-key.js');

// ── helpers ──────────────────────────────────────────────────────────────────

const TMP = join(tmpdir(), `sutando-voice-config-test-${Date.now()}`);
mkdirSync(TMP, { recursive: true });

after(() => {
	try { rmSync(TMP, { recursive: true, force: true }); } catch { /* best-effort */ }
});

function writeConfig(name: string, data: unknown): string {
	const p = join(TMP, name);
	writeFileSync(p, JSON.stringify(data));
	return p;
}

// ── VOICE_CONFIG_DEFAULTS ─────────────────────────────────────────────────────

describe('VOICE_CONFIG_DEFAULTS', () => {
	it('has a model string', () => {
		assert.ok(typeof VOICE_CONFIG_DEFAULTS.model === 'string');
		assert.ok(VOICE_CONFIG_DEFAULTS.model.length > 0);
	});

	it('has googleSearch defaulting to true', () => {
		assert.equal(VOICE_CONFIG_DEFAULTS.googleSearch, true);
	});

	it('has owner_mode defaulting to false (fail-closed)', () => {
		assert.equal(VOICE_CONFIG_DEFAULTS.owner_mode, false);
	});

	it('has an empty channels map by default', () => {
		assert.deepEqual(VOICE_CONFIG_DEFAULTS.channels, {});
	});
});

// ── loadVoiceConfig ───────────────────────────────────────────────────────────

describe('loadVoiceConfig', { concurrency: 1 }, () => {
	it('returns defaults when the file does not exist', () => {
		const cfg = loadVoiceConfig(join(TMP, 'nonexistent.json'));
		assert.equal(cfg.model, VOICE_CONFIG_DEFAULTS.model);
		assert.equal(cfg.googleSearch, VOICE_CONFIG_DEFAULTS.googleSearch);
		assert.equal(cfg.owner_mode, false);
		assert.deepEqual(cfg.channels, {});
	});

	it('returns defaults when the file contains invalid JSON', () => {
		const p = join(TMP, 'bad.json');
		writeFileSync(p, 'not json {{{');
		const cfg = loadVoiceConfig(p);
		assert.equal(cfg.model, VOICE_CONFIG_DEFAULTS.model);
		assert.equal(cfg.owner_mode, false);
	});

	it('merges partial config with defaults', () => {
		const p = writeConfig('partial.json', { model: 'gemini-custom' });
		const cfg = loadVoiceConfig(p);
		assert.equal(cfg.model, 'gemini-custom');
		assert.equal(cfg.googleSearch, VOICE_CONFIG_DEFAULTS.googleSearch);
		assert.equal(cfg.owner_mode, false);
	});

	it('loads a complete config overriding all defaults', () => {
		const p = writeConfig('full.json', {
			model: 'gemini-2.5-pro',
			googleSearch: false,
			owner_mode: true,
			channels: { ch1: { owner_mode: true } },
		});
		const cfg = loadVoiceConfig(p);
		assert.equal(cfg.model, 'gemini-2.5-pro');
		assert.equal(cfg.googleSearch, false);
		assert.equal(cfg.owner_mode, true);
		assert.deepEqual(cfg.channels, { ch1: { owner_mode: true } });
	});

	it('takes the file channels map verbatim (no deep-merge with default empty)', () => {
		const p = writeConfig('channels.json', {
			channels: { voice1: { owner_mode: true }, voice2: { owner_mode: false } },
		});
		const cfg = loadVoiceConfig(p);
		assert.equal(cfg.channels.voice1?.owner_mode, true);
		assert.equal(cfg.channels.voice2?.owner_mode, false);
	});

	it('uses an empty channels object when channels is absent from file', () => {
		const p = writeConfig('no-channels.json', { model: 'gemini-2.5-flash' });
		const cfg = loadVoiceConfig(p);
		assert.deepEqual(cfg.channels, {});
	});
});

// ── resolveOwnerMode ──────────────────────────────────────────────────────────

describe('resolveOwnerMode — trust-boundary logic', { concurrency: 1 }, () => {
	const baseConfig = { ...VOICE_CONFIG_DEFAULTS, channels: {} };

	it('returns false when no channelId and skill owner_mode is false', () => {
		const cfg = { ...baseConfig, owner_mode: false };
		assert.equal(resolveOwnerMode(cfg), false);
	});

	it('returns true when no channelId and skill owner_mode is true', () => {
		const cfg = { ...baseConfig, owner_mode: true };
		assert.equal(resolveOwnerMode(cfg), true);
	});

	it('returns false for an unknown channelId with skill owner_mode false', () => {
		const cfg = { ...baseConfig, owner_mode: false };
		assert.equal(resolveOwnerMode(cfg, 'unknown-ch'), false);
	});

	it('falls back to skill owner_mode when channel has no owner_mode key', () => {
		const cfg = {
			...baseConfig,
			owner_mode: true,
			channels: { ch1: {} },
		};
		// ch1 exists but has no owner_mode key → use skill default (true)
		assert.equal(resolveOwnerMode(cfg, 'ch1'), true);
	});

	it('channel owner_mode:true grants even if skill default is false', () => {
		const cfg = {
			...baseConfig,
			owner_mode: false,
			channels: { ch1: { owner_mode: true } },
		};
		assert.equal(resolveOwnerMode(cfg, 'ch1'), true);
	});

	it('channel owner_mode:false denies even if skill default is true', () => {
		const cfg = {
			...baseConfig,
			owner_mode: true,
			channels: { ch1: { owner_mode: false } },
		};
		assert.equal(resolveOwnerMode(cfg, 'ch1'), false);
	});

	it('fails closed — non-boolean string "true" is NOT owner mode', () => {
		// Fail-closed: only the boolean literal true grants owner tier.
		// A hand-edited config with "true" string must NOT grant access.
		const cfg = {
			...baseConfig,
			owner_mode: 'true' as unknown as boolean,
		};
		assert.equal(resolveOwnerMode(cfg), false);
	});

	it('fails closed — numeric 1 is NOT owner mode', () => {
		const cfg = {
			...baseConfig,
			owner_mode: 1 as unknown as boolean,
		};
		assert.equal(resolveOwnerMode(cfg), false);
	});

	it('channel-level string "true" also fails closed', () => {
		const cfg = {
			...baseConfig,
			owner_mode: false,
			channels: { ch1: { owner_mode: 'true' as unknown as boolean } },
		};
		assert.equal(resolveOwnerMode(cfg, 'ch1'), false);
	});

	it('channel explicit false does not bleed into other channels', () => {
		const cfg = {
			...baseConfig,
			owner_mode: true,
			channels: { ch1: { owner_mode: false } },
		};
		// ch1 is denied, ch2 falls back to skill true
		assert.equal(resolveOwnerMode(cfg, 'ch1'), false);
		assert.equal(resolveOwnerMode(cfg, 'ch2'), true);
	});
});

// ── voiceApiKey ───────────────────────────────────────────────────────────────

describe('voiceApiKey', { concurrency: 1 }, () => {
	const saved = {
		GEMINI_VOICE_API_KEY: process.env.GEMINI_VOICE_API_KEY,
		GEMINI_API_KEY: process.env.GEMINI_API_KEY,
	};

	after(() => {
		if (saved.GEMINI_VOICE_API_KEY === undefined) delete process.env.GEMINI_VOICE_API_KEY;
		else process.env.GEMINI_VOICE_API_KEY = saved.GEMINI_VOICE_API_KEY;
		if (saved.GEMINI_API_KEY === undefined) delete process.env.GEMINI_API_KEY;
		else process.env.GEMINI_API_KEY = saved.GEMINI_API_KEY;
	});

	it('returns empty string when neither var is set', () => {
		delete process.env.GEMINI_VOICE_API_KEY;
		delete process.env.GEMINI_API_KEY;
		assert.equal(voiceApiKey(), '');
	});

	it('returns GEMINI_VOICE_API_KEY when set', () => {
		process.env.GEMINI_VOICE_API_KEY = 'voice-key-123';
		delete process.env.GEMINI_API_KEY;
		assert.equal(voiceApiKey(), 'voice-key-123');
	});

	it('falls back to GEMINI_API_KEY when GEMINI_VOICE_API_KEY is absent', () => {
		delete process.env.GEMINI_VOICE_API_KEY;
		process.env.GEMINI_API_KEY = 'main-key-456';
		assert.equal(voiceApiKey(), 'main-key-456');
	});

	it('prefers GEMINI_VOICE_API_KEY over GEMINI_API_KEY when both are set', () => {
		process.env.GEMINI_VOICE_API_KEY = 'voice-wins';
		process.env.GEMINI_API_KEY = 'main-key';
		assert.equal(voiceApiKey(), 'voice-wins');
	});
});
