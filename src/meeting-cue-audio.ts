/**
 * The meeting-mode cue in the session's own voice: rendered once with Gemini TTS, using the same
 * prebuilt voice name as the live session, and cached on disk keyed by voice and text.
 */

import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const TTS_MODEL = 'gemini-2.5-flash-preview-tts';
const PCM_RATE = 24_000;

/** Wraps Gemini TTS output (16-bit mono PCM) in a WAV header so a browser can play it. */
export function pcmToWav(pcm: Buffer, sampleRate = PCM_RATE): Buffer {
	const header = Buffer.alloc(44);
	header.write('RIFF', 0);
	header.writeUInt32LE(36 + pcm.length, 4);
	header.write('WAVE', 8);
	header.write('fmt ', 12);
	header.writeUInt32LE(16, 16);
	header.writeUInt16LE(1, 20);
	header.writeUInt16LE(1, 22);
	header.writeUInt32LE(sampleRate, 24);
	header.writeUInt32LE(sampleRate * 2, 28);
	header.writeUInt16LE(2, 32);
	header.writeUInt16LE(16, 34);
	header.write('data', 36);
	header.writeUInt32LE(pcm.length, 40);
	return Buffer.concat([header, pcm]);
}

export interface CueAudioOptions {
	apiKey: string;
	voice: string;
	text: string;
	/** Cache directory (under the workspace state). */
	dir: string;
	fetchImpl?: typeof fetch;
}

/** The cue as base64 WAV, from the cache or rendered now; null when it cannot be had (the page then speaks it itself). */
export async function meetingCueAudio(opts: CueAudioOptions): Promise<string | null> {
	const key = createHash('sha256').update(`${TTS_MODEL}|${opts.voice}|${opts.text}`).digest('hex').slice(0, 16);
	const path = join(opts.dir, `meeting-entry-${key}.wav`);
	try {
		if (existsSync(path)) return readFileSync(path).toString('base64');
	} catch { /* render again */ }
	if (!opts.apiKey) return null;
	try {
		const res = await (opts.fetchImpl ?? fetch)(`https://generativelanguage.googleapis.com/v1beta/models/${TTS_MODEL}:generateContent`, {
			method: 'POST',
			headers: { 'Content-Type': 'application/json', 'x-goog-api-key': opts.apiKey },
			body: JSON.stringify({
				contents: [{ parts: [{ text: opts.text }] }],
				generationConfig: { responseModalities: ['AUDIO'], speechConfig: { voiceConfig: { prebuiltVoiceConfig: { voiceName: opts.voice } } } },
			}),
		});
		if (!res.ok) return null;
		const body = await res.json() as { candidates?: Array<{ content?: { parts?: Array<{ inlineData?: { data?: string } }> } }> };
		const data = body.candidates?.[0]?.content?.parts?.find((p) => p.inlineData?.data)?.inlineData?.data;
		if (!data) return null;
		const wav = pcmToWav(Buffer.from(data, 'base64'));
		mkdirSync(opts.dir, { recursive: true });
		const tmp = `${path}.${process.pid}.tmp`;
		writeFileSync(tmp, wav);
		renameSync(tmp, path);
		return wav.toString('base64');
	} catch {
		return null;
	}
}
