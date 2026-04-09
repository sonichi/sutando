/**
 * Cartesia sonic-3 TTS — generates WAV audio files from text.
 *
 * Used for non-realtime speech: task results, briefings, proactive messages.
 * Does NOT replace Gemini native audio for live voice conversation.
 *
 * Usage as module:
 *   import { generateSpeech } from './cartesia-tts.js';
 *   const wavPath = await generateSpeech('Hello world');
 *
 * Usage as CLI:
 *   npx tsx src/cartesia-tts.ts "Hello world"
 */

// `@cartesia/cartesia-js` is an optional dependency. voice-agent.ts only
// dynamically imports this file when CARTESIA_API_KEY is set, so the module
// missing is never a runtime error for Gemini-only users. We use @ts-ignore
// (not @ts-expect-error) so tsc tolerates both states:
//   - package NOT installed → ignore suppresses the "cannot find module" error
//   - package IS installed   → ignore is a no-op (@ts-expect-error would
//                                fail here with "unused directive")
// @ts-ignore -- optional dependency, resolved at runtime
import Cartesia from '@cartesia/cartesia-js';
import { writeFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';

const getCartesiaApiKey = () => process.env.CARTESIA_API_KEY || '';
const getCartesiaVoiceId = () => process.env.CARTESIA_VOICE_ID || 'f786b574-daa5-4673-aa0c-cbe3e8534c02';
const getWorkspace = () => process.env.WORKSPACE_DIR || process.cwd();

const SAMPLE_RATE = 24000;
const CHANNELS = 1;
const BIT_DEPTH = 16;

/** Split text into sentences for chunked TTS. Captures trailing non-punctuated text. */
export function splitSentences(text: string): string[] {
	const matched = text.match(/[^.!?]+[.!?]+/g) || [];
	const matchedText = matched.join('');
	const tail = text.slice(matchedText.length).trim();
	return matched.length > 0
		? (tail ? [...matched, tail] : matched)
		: [text];
}

/**
 * Generate speech audio from text.
 * @param text Text to speak
 * @param options.outputPath Override the output file path
 * @param options.category Organize into subdirectory (e.g., 'briefing', 'result', 'proactive')
 * @param options.label Human-readable label for the filename (e.g., 'morning-briefing')
 */
export async function generateSpeech(
	text: string,
	options: { outputPath?: string; category?: string; label?: string } = {},
): Promise<string> {
	if (!getCartesiaApiKey()) throw new Error('CARTESIA_API_KEY not set');
	if (!text.trim()) throw new Error('Empty text');

	// Organize: results/audio/{category}/{label}-{timestamp}.wav
	const category = options.category || 'general';
	const label = options.label || 'tts';
	const outDir = join(getWorkspace(), 'results', 'audio', category);
	mkdirSync(outDir, { recursive: true });
	const outPath = options.outputPath || join(outDir, `${label}-${Date.now()}.wav`);

	const client = new Cartesia({ apiKey: getCartesiaApiKey() });
	const ws = await client.tts.websocket();
	try {
		const ctx = ws.context({
			model_id: 'sonic-3',
			voice: { mode: 'id', id: getCartesiaVoiceId() },
			output_format: {
				container: 'raw',
				encoding: 'pcm_s16le',
				sample_rate: SAMPLE_RATE,
			},
		});

		// Push text in sentence chunks for natural prosody
		const sentences = splitSentences(text);
		for (const s of sentences) {
			await ctx.push({ transcript: s.trim() + ' ' });
		}
		await ctx.no_more_inputs();

		const chunks: Buffer[] = [];
		for await (const event of ctx.receive()) {
			if (event.type === 'chunk' && event.audio) {
				chunks.push(Buffer.isBuffer(event.audio) ? event.audio : Buffer.from(event.audio));
			} else if (event.type === 'done') {
				break;
			}
		}

		// Write WAV with header
		const pcm = Buffer.concat(chunks);
		const header = createWavHeader(pcm.length, SAMPLE_RATE, CHANNELS, BIT_DEPTH);
		writeFileSync(outPath, Buffer.concat([header, pcm]));
		return outPath;
	} finally {
		ws.close();
	}
}

export function createWavHeader(dataSize: number, sampleRate: number, channels: number, bitDepth: number): Buffer {
	const header = Buffer.alloc(44);
	header.write('RIFF', 0);
	header.writeUInt32LE(36 + dataSize, 4);
	header.write('WAVE', 8);
	header.write('fmt ', 12);
	header.writeUInt32LE(16, 16);           // fmt chunk size
	header.writeUInt16LE(1, 20);            // PCM format
	header.writeUInt16LE(channels, 22);
	header.writeUInt32LE(sampleRate, 24);
	header.writeUInt32LE(sampleRate * channels * bitDepth / 8, 28); // byte rate
	header.writeUInt16LE(channels * bitDepth / 8, 32);              // block align
	header.writeUInt16LE(bitDepth, 34);
	header.write('data', 36);
	header.writeUInt32LE(dataSize, 40);
	return header;
}

// CLI entrypoint
if (process.argv[1]?.endsWith('cartesia-tts.ts') || process.argv[1]?.endsWith('cartesia-tts.js')) {
	const text = process.argv[2];
	if (!text) {
		console.error('Usage: npx tsx src/cartesia-tts.ts "text to speak"');
		process.exit(1);
	}
	generateSpeech(text, { category: 'cli', label: 'speech' })
		.then(path => console.log(path))
		.catch(err => { console.error(err.message); process.exit(1); });
}
