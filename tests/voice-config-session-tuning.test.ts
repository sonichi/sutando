// Phase 0.5 session-tuning seams (design §2.1/§2.2): compressionConfig and
// mediaResolution resolve from the config file plus the two env overrides,
// OFF by default. The load-time contract this file pins:
//   - nothing set ⇒ `{}` with NEITHER key present — the wire behaviour is
//     byte-identical to a build without the seams (the Phase 0.5 gate);
//   - `compressionConfig: {}` ⇒ enabled with the SERVER's defaults;
//   - explicit thresholds are positive safe integers, both-or-neither,
//     0 < target < trigger — anything else throws (a half-set or inverted
//     pair is a silently-degrading misconfiguration and must fail startup);
//   - env (VOICE_CTX_TRIGGER_TOKENS/VOICE_CTX_TARGET_TOKENS) beats the file
//     and on its own enables compression;
//   - null/false is DELIBERATELY DISABLED, distinct from absent: both resolve
//     to real key absence today, but only the explicit form survives the value
//     becoming a built-in default (design Phase 3, step 3e — without it the
//     user-side rollback after release is downgrading the app).
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
	resolveSessionTuning,
	VOICE_CONFIG_DEFAULTS,
	type VoiceConfig,
} from '../src/voice-config.js';

function cfg(over: Partial<VoiceConfig> = {}): VoiceConfig {
	return { ...VOICE_CONFIG_DEFAULTS, channels: {}, ...over };
}

const NO_ENV: Record<string, string | undefined> = {};

describe('P7 Phase 0.5 — resolveSessionTuning', () => {
	it('nothing set ⇒ NEITHER key present (byte-identical wire behaviour)', () => {
		const out = resolveSessionTuning(cfg(), NO_ENV);
		assert.deepEqual(out, {});
		assert.equal('compressionConfig' in out, false, 'real key absence, not undefined');
		assert.equal('mediaResolution' in out, false);
	});

	it('compressionConfig: {} enables with the SERVER defaults — no local constants', () => {
		const out = resolveSessionTuning(cfg({ compressionConfig: {} }), NO_ENV);
		assert.deepEqual(out.compressionConfig, {});
		assert.equal('triggerTokens' in (out.compressionConfig ?? {}), false);
	});

	it('explicit thresholds pass through when 0 < target < trigger', () => {
		const out = resolveSessionTuning(
			cfg({ compressionConfig: { triggerTokens: 3000, targetTokens: 1500 } }),
			NO_ENV,
		);
		assert.deepEqual(out.compressionConfig, { triggerTokens: 3000, targetTokens: 1500 });
	});

	it('an inverted or equal pair throws — never ships silently degrading', () => {
		assert.throws(
			() =>
				resolveSessionTuning(
					cfg({ compressionConfig: { triggerTokens: 1500, targetTokens: 3000 } }),
					NO_ENV,
				),
			/0 < targetTokens < triggerTokens/,
		);
		assert.throws(
			() =>
				resolveSessionTuning(
					cfg({ compressionConfig: { triggerTokens: 3000, targetTokens: 3000 } }),
					NO_ENV,
				),
			/0 < targetTokens < triggerTokens/,
		);
	});

	it('half-set, non-integer, zero, negative, and non-object shapes all throw', () => {
		assert.throws(
			() => resolveSessionTuning(cfg({ compressionConfig: { triggerTokens: 3000 } }), NO_ENV),
			/BOTH/,
		);
		assert.throws(
			() => resolveSessionTuning(cfg({ compressionConfig: { targetTokens: 1500 } }), NO_ENV),
			/BOTH/,
		);
		assert.throws(
			() =>
				resolveSessionTuning(
					cfg({ compressionConfig: { triggerTokens: 3000.5, targetTokens: 1500 } }),
					NO_ENV,
				),
			/positive integer/,
		);
		assert.throws(
			() =>
				resolveSessionTuning(
					cfg({ compressionConfig: { triggerTokens: 3000, targetTokens: 0 } }),
					NO_ENV,
				),
			/positive integer/,
		);
		assert.throws(
			() =>
				resolveSessionTuning(
					cfg({ compressionConfig: { triggerTokens: 3000, targetTokens: -5 } }),
					NO_ENV,
				),
			/positive integer/,
		);
		assert.throws(
			() =>
				resolveSessionTuning(
					// a hand-edited file can carry any JSON shape
					cfg({ compressionConfig: [3000, 1500] as unknown as VoiceConfig['compressionConfig'] }),
					NO_ENV,
				),
			/must be an object/,
		);
	});

	it('file thresholds must BE numbers — a JSON string "3000" is a schema violation, not tuning', () => {
		assert.throws(
			() =>
				resolveSessionTuning(
					cfg({
						compressionConfig: {
							triggerTokens: '3000',
							targetTokens: 1500,
						} as unknown as VoiceConfig['compressionConfig'],
					}),
					NO_ENV,
				),
			/positive integer/,
		);
	});

	it('env thresholds are digits-only strings — floats, exponents, hex, padding all throw', () => {
		for (const bad of ['1e3', '0x10', ' 3000', '3000.0', '-3000', '+3000']) {
			assert.throws(
				() =>
					resolveSessionTuning(cfg(), {
						VOICE_CTX_TRIGGER_TOKENS: bad,
						VOICE_CTX_TARGET_TOKENS: '100',
					}),
				/positive integer/,
				`expected throw for ${JSON.stringify(bad)}`,
			);
		}
	});

	it('env overrides the file and on its own enables compression', () => {
		const env = { VOICE_CTX_TRIGGER_TOKENS: '4000', VOICE_CTX_TARGET_TOKENS: '2000' };
		const fromNothing = resolveSessionTuning(cfg(), env);
		assert.deepEqual(fromNothing.compressionConfig, { triggerTokens: 4000, targetTokens: 2000 });
		const overFile = resolveSessionTuning(
			cfg({ compressionConfig: { triggerTokens: 3000, targetTokens: 1500 } }),
			env,
		);
		assert.deepEqual(overFile.compressionConfig, { triggerTokens: 4000, targetTokens: 2000 });
	});

	it('a half-set or garbage env pair throws with the knob named', () => {
		assert.throws(
			() => resolveSessionTuning(cfg(), { VOICE_CTX_TRIGGER_TOKENS: '4000' }),
			/VOICE_CTX_TRIGGER_TOKENS\/VOICE_CTX_TARGET_TOKENS.*BOTH/,
		);
		assert.throws(
			() =>
				resolveSessionTuning(cfg(), {
					VOICE_CTX_TRIGGER_TOKENS: 'many',
					VOICE_CTX_TARGET_TOKENS: '2000',
				}),
			/positive integer/,
		);
		assert.throws(
			() =>
				resolveSessionTuning(cfg(), {
					VOICE_CTX_TRIGGER_TOKENS: '2000',
					VOICE_CTX_TARGET_TOKENS: '4000',
				}),
			/0 < targetTokens < triggerTokens/,
		);
	});

	it('mediaResolution: valid enum passes, absent stays absent, garbage throws', () => {
		const low = resolveSessionTuning(cfg({ mediaResolution: 'MEDIA_RESOLUTION_LOW' }), NO_ENV);
		assert.equal(low.mediaResolution, 'MEDIA_RESOLUTION_LOW');
		assert.throws(
			() =>
				resolveSessionTuning(
					cfg({ mediaResolution: 'low' as VoiceConfig['mediaResolution'] }),
					NO_ENV,
				),
			/mediaResolution must be one of/,
		);
	});

	it('null/false disables a seam explicitly — resolved as real key absence', () => {
		for (const off of [null, false] as const) {
			const out = resolveSessionTuning(
				cfg({
					compressionConfig: off as VoiceConfig['compressionConfig'],
					mediaResolution: off as VoiceConfig['mediaResolution'],
				}),
				NO_ENV,
			);
			assert.deepEqual(out, {}, `${JSON.stringify(off)} disables both seams`);
			assert.equal('compressionConfig' in out, false);
			assert.equal('mediaResolution' in out, false);
		}
	});

	it('a disabled seam never throws the shape error meant for garbage', () => {
		// Before the off-switch, null hit "must be an object" — which would have
		// left app-downgrade as the only rollback once the value is a default.
		assert.doesNotThrow(() =>
			resolveSessionTuning(
				cfg({ compressionConfig: null as VoiceConfig['compressionConfig'] }),
				NO_ENV,
			),
		);
		// Garbage still throws, naming both enabling and disabling forms.
		assert.throws(
			() =>
				resolveSessionTuning(
					cfg({ compressionConfig: 'on' as unknown as VoiceConfig['compressionConfig'] }),
					NO_ENV,
				),
			/null\/false disables/,
		);
	});

	it('env thresholds still win over a file-disabled compression seam', () => {
		// Precedence is unchanged by the off-switch: the env pair is the
		// operator's most explicit statement and still enables.
		const out = resolveSessionTuning(
			cfg({ compressionConfig: null as VoiceConfig['compressionConfig'] }),
			{ VOICE_CTX_TRIGGER_TOKENS: '4000', VOICE_CTX_TARGET_TOKENS: '2000' },
		);
		assert.deepEqual(out.compressionConfig, { triggerTokens: 4000, targetTokens: 2000 });
	});

	it('the shipped example template stays valid JSON with the seams OFF', async () => {
		const { readFileSync } = await import('fs');
		const raw = JSON.parse(
			readFileSync(new URL('../src/voice-agent.config.json.example', import.meta.url), 'utf-8'),
		);
		assert.equal('compressionConfig' in raw, false, 'template documents, never sets');
		assert.equal('mediaResolution' in raw, false);
		const out = resolveSessionTuning(cfg(raw), NO_ENV);
		assert.deepEqual(out, {}, 'seeded config produces the byte-identical default');
	});
});
