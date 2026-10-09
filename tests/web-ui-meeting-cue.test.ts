// The meeting-mode confirmation is spoken by the page itself with the mic muted, not by the model:
// model speech can be cut off by any sound (client-VAD barge-in), and the transcriber would write it
// into the note. The server enters dictation at once and sends the fixed text.
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readdirSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..');
const CLIENT = readFileSync(join(REPO, 'src', 'web-client.ts'), 'utf-8');
const AGENT = readFileSync(join(REPO, 'src', 'voice-agent.ts'), 'utf-8');
const { meetingCueAudio, pcmToWav } = await import('../src/meeting-cue-audio.js');

/** Pull the shipped function out of the served page rather than restating it. */
function extract(): string {
	const start = CLIENT.indexOf('function playMeetingCue(text, audio)');
	assert.notEqual(start, -1, 'playMeetingCue() is gone or renamed');
	return CLIENT.slice(start, CLIENT.indexOf('\n}\n', start) + 2);
}

function harness(opts: { userMuted?: boolean; noSpeech?: boolean; clip?: 'ok' | 'error' | 'reject' } = {}) {
	const mic: boolean[] = [];
	const spoken: Array<{ text: string; lang: string; onend?: () => void; onerror?: () => void }> = [];
	const system: string[] = [];
	const page = { muted: !!opts.userMuted };
	const voice = { setMicMuted: (m: boolean) => mic.push(m) };
	class Utterance { text: string; lang = ''; onend?: () => void; onerror?: () => void; constructor(t: string) { this.text = t; } }
	const win = opts.noSpeech ? {} : { speechSynthesis: { cancel() {}, speak: (u: Utterance) => spoken.push(u) } };
	const timers: Array<() => void> = [];
	const clips: Array<{ src: string; onended?: () => void; onerror?: () => void }> = [];
	class FakeAudio {
		src: string; onended?: () => void; onerror?: () => void;
		constructor(src: string) { this.src = src; clips.push(this); }
		play() { return opts.clip === 'reject' ? Promise.reject(new Error('autoplay blocked')) : Promise.resolve(); }
	}
	const play = new Function(
		'window', 'SpeechSynthesisUtterance', 'Audio', 'voice', 'page', 'addSystem', 'setTimeout',
		`with (page) { ${extract()} return playMeetingCue; }`,
	)(win, Utterance, FakeAudio, voice, page, (t: string) => system.push(t), (f: () => void) => timers.push(f)) as (t: string, a?: string) => void;
	return { play, mic, spoken, system, page, timers, clips };
}

const CUE = 'Meeting mode on. To bring me back, say "Sutando, come back". Until then I\'ll stay silent and take notes.';

describe('meeting-mode cue', () => {
	it('speaks the fixed sentence with the mic muted, and unmutes when it ends', () => {
		const h = harness();
		h.play(CUE);
		assert.deepEqual(h.mic, [true], 'muted before speaking');
		assert.equal(h.spoken.length, 1);
		assert.equal(h.spoken[0].text, CUE);
		assert.equal(h.spoken[0].lang, 'en-US');
		assert.deepEqual(h.system, [CUE], 'shown in the transcript too');
		h.spoken[0].onend?.();
		h.timers.forEach((f) => f());
		assert.deepEqual(h.mic, [true, false], 'unmuted once, not twice');
	});

	it('a mic the user had muted is left alone', () => {
		const h = harness({ userMuted: true });
		h.play(CUE);
		h.spoken[0].onend?.();
		assert.deepEqual(h.mic, []);
	});

	it('a user who mutes during the cue stays muted', () => {
		const h = harness();
		h.play(CUE);
		h.page.muted = true;
		h.spoken[0].onend?.();
		assert.deepEqual(h.mic, [true]);
	});

	it('a speech error, or no end event at all, still gives the mic back', () => {
		const a = harness();
		a.play(CUE);
		a.spoken[0].onerror?.();
		assert.deepEqual(a.mic, [true, false]);
		const b = harness();
		b.play(CUE);
		b.timers.forEach((f) => f());
		assert.deepEqual(b.mic, [true, false], 'the timeout releases it');
	});

	it('without speech synthesis it only shows the text and never touches the mic', () => {
		const h = harness({ noSpeech: true });
		h.play(CUE);
		assert.deepEqual(h.mic, []);
		assert.deepEqual(h.system, [CUE]);
	});

	it('plays the recording in the session voice when the server sent one, mic muted, and not the browser voice', () => {
		const h = harness();
		h.play(CUE, 'UklGRg==');
		assert.equal(h.clips.length, 1);
		assert.equal(h.clips[0].src, 'data:audio/wav;base64,UklGRg==');
		assert.deepEqual(h.spoken, [], 'no speech synthesis');
		assert.deepEqual(h.mic, [true]);
		h.clips[0].onended?.();
		assert.deepEqual(h.mic, [true, false]);
	});

	it('a recording that fails to play falls back to the browser voice once, and the mic still comes back', async () => {
		const a = harness({ clip: 'error' });
		a.play(CUE, 'UklGRg==');
		a.clips[0].onerror?.();
		a.clips[0].onerror?.();
		assert.equal(a.spoken.length, 1);
		a.spoken[0].onend?.();
		assert.deepEqual(a.mic, [true, false]);
		const b = harness({ clip: 'reject' });
		b.play(CUE, 'UklGRg==');
		await new Promise((r) => setTimeout(r, 0));
		assert.equal(b.spoken.length, 1, 'a blocked play() falls back too');
	});

	it('the page routes a meeting.cue frame to it, with the recording when there is one', () => {
		assert.match(CLIENT, /msg\.type === 'meeting\.cue'\) \{\s*playMeetingCue\(String\(msg\.text \|\| ''\), typeof msg\.audio === 'string' \? msg\.audio : ''\);/);
	});
});

describe('voice agent: the model no longer speaks the confirmation', () => {
	it('every entry (switch_mode and the menu bar) sends the fixed cue, and switch_mode tells the model to say nothing', () => {
		const enter = AGENT.slice(AGENT.indexOf('function enterMeetingDictation()'), AGENT.indexOf('\n}\n', AGENT.indexOf('function enterMeetingDictation()')));
		assert.match(enter, /sendJsonToClient\(\{ type: 'meeting\.cue', text: MEETING_ENTRY_SAY, audio: meetingCueWav \?\? undefined \}/);
		assert.match(AGENT, /meetingCueAudio\(\{ apiKey: GEMINI_VOICE_API_KEY, voice: VOICE_NAME, text: MEETING_ENTRY_SAY/, 'rendered in the session voice from the same text');
		assert.doesNotMatch(AGENT, /Say exactly this, then end your turn/);
		assert.match(AGENT, /if \(want\) enterMeetingDictation\(\);/, 'the menu-bar path enters through the same function');
		assert.doesNotMatch(AGENT, /meetingEntry\./, 'no wait for the model\'s turn before transcribing');
	});
});

describe('meeting cue audio (Gemini TTS, cached)', () => {
	const pcm = Buffer.from([1, 0, 2, 0, 3, 0]);
	const ok = (calls: Array<{ url: string; body: Record<string, unknown> }>) => (async (url: string, init: { body: string }) => {
		calls.push({ url, body: JSON.parse(init.body) });
		return { ok: true, json: async () => ({ candidates: [{ content: { parts: [{ inlineData: { data: pcm.toString('base64') } }] } }] }) };
	}) as unknown as typeof fetch;

	it('wraps 24 kHz mono 16-bit PCM in a WAV header', () => {
		const wav = pcmToWav(pcm);
		assert.equal(wav.subarray(0, 4).toString(), 'RIFF');
		assert.equal(wav.subarray(8, 12).toString(), 'WAVE');
		assert.equal(wav.readUInt32LE(24), 24_000);
		assert.equal(wav.readUInt16LE(22), 1);
		assert.equal(wav.readUInt16LE(34), 16);
		assert.equal(wav.readUInt32LE(40), pcm.length);
		assert.equal(wav.length, 44 + pcm.length);
	});

	it('renders once in the given voice, then serves the cache; a new text renders again', async () => {
		const dir = mkdtempSync(join(tmpdir(), 'cue-'));
		const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
		const first = await meetingCueAudio({ apiKey: 'k', voice: 'Puck', text: CUE, dir, fetchImpl: ok(calls) });
		const again = await meetingCueAudio({ apiKey: 'k', voice: 'Puck', text: CUE, dir, fetchImpl: ok(calls) });
		assert.equal(first, pcmToWav(pcm).toString('base64'));
		assert.equal(again, first);
		assert.equal(calls.length, 1, 'cached');
		assert.match(JSON.stringify(calls[0].body), /"voiceName":"Puck"/);
		await meetingCueAudio({ apiKey: 'k', voice: 'Puck', text: CUE + ' ', dir, fetchImpl: ok(calls) });
		assert.equal(calls.length, 2);
		assert.equal(readdirSync(dir).filter((f) => f.endsWith('.wav')).length, 2);
		rmSync(dir, { recursive: true, force: true });
	});

	it('no key, an HTTP error or a response without audio gives null, so the page speaks the text', async () => {
		const dir = mkdtempSync(join(tmpdir(), 'cue-'));
		assert.equal(await meetingCueAudio({ apiKey: '', voice: 'Puck', text: CUE, dir }), null);
		const bad = (async () => ({ ok: false, json: async () => ({}) })) as unknown as typeof fetch;
		assert.equal(await meetingCueAudio({ apiKey: 'k', voice: 'Puck', text: CUE, dir, fetchImpl: bad }), null);
		const empty = (async () => ({ ok: true, json: async () => ({ candidates: [] }) })) as unknown as typeof fetch;
		assert.equal(await meetingCueAudio({ apiKey: 'k', voice: 'Puck', text: CUE, dir, fetchImpl: empty }), null);
		const thrown = (async () => { throw new Error('offline'); }) as unknown as typeof fetch;
		assert.equal(await meetingCueAudio({ apiKey: 'k', voice: 'Puck', text: CUE, dir, fetchImpl: thrown }), null);
		rmSync(dir, { recursive: true, force: true });
	});
});
