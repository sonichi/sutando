import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { GeminiLiveTranscribeSTTProvider, resamplePcm16, type WebSocketLike } from '../src/gemini-live-transcribe-stt.js';
import { attachMeetingDictation, createMeetingEntryGate, isMeetingExitPhrase } from '../src/meeting-dictation.js';

class FakeSocket implements WebSocketLike {
	readyState = 0;
	sent: any[] = [];
	closed = false;
	private handlers: Record<string, Array<(...a: any[]) => void>> = {};
	on(ev: string, fn: (...a: any[]) => void) { (this.handlers[ev] ??= []).push(fn); }
	emit(ev: string, ...a: any[]) { for (const fn of this.handlers[ev] ?? []) fn(...a); }
	send(d: string) { this.sent.push(JSON.parse(d)); }
	close() { this.closed = true; this.readyState = 3; }
	openAndSetup() { this.readyState = 1; this.emit('open'); this.emit('message', JSON.stringify({ setupComplete: {} })); }
	audioCount() { return this.sent.filter((m) => m.realtimeInput?.audio).length; }
}

const chunk = Buffer.alloc(4800).toString('base64'); // 100 ms of silence @ 24 kHz
const loud = (() => { const b = Buffer.alloc(4800); for (let i = 0; i < 2400; i++) b.writeInt16LE(i % 2 ? 8000 : -8000, i * 2); return b.toString('base64'); })();
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

function makeProvider(opts: { rotateAfterMs?: number } = {}) {
	const sockets: FakeSocket[] = [];
	const p = new GeminiLiveTranscribeSTTProvider({
		apiKey: 'k',
		rotateAfterMs: opts.rotateAfterMs ?? 60_000,
		drainMs: 5,
		createSocket: () => { const s = new FakeSocket(); sockets.push(s); return s; },
	});
	p.configure({ sampleRate: 24000, bitDepth: 16, channels: 1 });
	return { p, sockets };
}

describe('GeminiLiveTranscribeSTTProvider', () => {
	it('sends the Transcribe Live setup and 16 kHz audio', async () => {
		const { p, sockets } = makeProvider();
		await p.start();
		sockets[0].openAndSetup();
		p.feedAudio(chunk);
		const setup = sockets[0].sent[0].setup;
		assert.equal(setup.model, 'models/gemini-3.5-transcribe-live');
		assert.deepEqual(setup.generationConfig.responseModalities, ['TEXT']);
		assert.ok(setup.inputAudioTranscription);
		const audio = sockets[0].sent[1].realtimeInput.audio;
		assert.equal(audio.mimeType, 'audio/pcm;rate=16000');
		assert.equal(Buffer.from(audio.data, 'base64').length, 3200);
		await p.stop();
	});

	it('sends custom vocabulary when given', async () => {
		const sockets: FakeSocket[] = [];
		const p = new GeminiLiveTranscribeSTTProvider({
			apiKey: 'k', customVocabulary: ['Sutando'],
			createSocket: () => { const s = new FakeSocket(); sockets.push(s); return s; },
		});
		p.configure({ sampleRate: 24000, bitDepth: 16, channels: 1 });
		await p.start();
		sockets[0].openAndSetup();
		assert.deepEqual(sockets[0].sent[0].setup.inputAudioTranscription.customVocabulary, ['Sutando']);
		await p.stop();
	});

	it('buffers audio until setup completes', async () => {
		const { p, sockets } = makeProvider();
		await p.start();
		p.feedAudio(chunk);
		p.feedAudio(chunk);
		assert.equal(sockets[0].audioCount(), 0);
		sockets[0].openAndSetup();
		p.feedAudio(chunk);
		assert.equal(sockets[0].audioCount(), 3);
		await p.stop();
	});

	it('forwards final and interim transcripts', async () => {
		const { p, sockets } = makeProvider();
		const finals: string[] = [];
		const partials: string[] = [];
		p.onTranscript = (t) => finals.push(t);
		p.onPartialTranscript = (t) => partials.push(t);
		await p.start();
		sockets[0].openAndSetup();
		sockets[0].emit('message', JSON.stringify({ serverContent: { interimInputTranscription: { text: 'hel' } } }));
		sockets[0].emit('message', Buffer.from(JSON.stringify({ serverContent: { inputTranscription: { text: ' hello world ' } } })));
		assert.deepEqual(partials, ['hel']);
		assert.deepEqual(finals, ['hello world']);
		await p.stop();
	});

	it('rotates sessions at a pause without dropping or duplicating audio', async () => {
		const { p, sockets } = makeProvider({ rotateAfterMs: 20 });
		const finals: string[] = [];
		p.onTranscript = (t) => finals.push(t);
		await p.start();
		sockets[0].openAndSetup();
		p.feedAudio(loud);
		await tick(30);
		assert.equal(sockets.length, 2, 'a second session opened before the limit');
		sockets[1].openAndSetup();
		p.feedAudio(loud); // speech continues → stays on the old session
		p.feedAudio(chunk);
		p.feedAudio(chunk);
		assert.equal(sockets[1].audioCount(), 0, 'no handover mid-speech');
		p.feedAudio(chunk); // 300 ms of quiet → hand over
		p.feedAudio(loud);
		assert.equal(sockets[0].audioCount(), 4);
		assert.equal(sockets[1].audioCount(), 2);
		assert.ok(sockets[0].sent.some((m) => m.realtimeInput?.audioStreamEnd), 'old session told the stream ended');
		// The old socket still delivers its last utterance while draining.
		sockets[0].emit('message', JSON.stringify({ serverContent: { inputTranscription: { text: 'last words' } } }));
		await tick(20);
		assert.ok(sockets[0].closed);
		assert.deepEqual(finals, ['last words']);
		await p.stop();
	});

	it('hands over at the deadline when nobody pauses', async () => {
		const sockets: FakeSocket[] = [];
		const p = new GeminiLiveTranscribeSTTProvider({
			apiKey: 'k', rotateAfterMs: 10, pauseWaitMs: 20, drainMs: 5,
			createSocket: () => { const s = new FakeSocket(); sockets.push(s); return s; },
		});
		p.configure({ sampleRate: 24000, bitDepth: 16, channels: 1 });
		await p.start();
		sockets[0].openAndSetup();
		await tick(15);
		sockets[1].openAndSetup();
		p.feedAudio(loud);
		assert.equal(sockets[1].audioCount(), 0);
		await tick(25);
		p.feedAudio(loud);
		assert.equal(sockets[1].audioCount(), 1);
		await p.stop();
	});

	it('reconnects when the active socket drops', async () => {
		const { p, sockets } = makeProvider();
		await p.start();
		sockets[0].openAndSetup();
		sockets[0].readyState = 3;
		sockets[0].emit('close', 1011, 'boom');
		p.feedAudio(chunk);
		assert.equal(sockets.length, 2);
		sockets[1].openAndSetup();
		assert.equal(sockets[1].audioCount(), 1);
		await p.stop();
	});

	it('resamples 24 kHz to 16 kHz', () => {
		assert.equal(resamplePcm16(Buffer.alloc(480), 24000, 16000).length, 320);
	});
});

describe('meeting dictation', () => {
	it('recognises exit phrases only', () => {
		for (const t of ['Sutando, come back', 'okay the meeting is over', 'end meeting', 'stop dictation', 'active mode please', 'done dictating'])
			assert.ok(isMeetingExitPhrase(t), t);
		for (const t of ['we should end the quarter strong', 'the meeting starts at noon', 'come back to this later'])
			assert.ok(!isMeetingExitPhrase(t), t);
	});

	function setup() {
		const dir = mkdtempSync(join(tmpdir(), 'meet-'));
		let mode: 'agent' | 'transcription' = 'agent';
		const buffer: string[] = [];
		const injected: string[] = [];
		let exitedByVoice = 0;
		const provider: any = { onTranscript: (t: string) => buffer.push(t) };
		const session = {
			setTranscriptionMode: async (m: 'agent' | 'transcription') => { mode = m; },
			getTranscriptionMode: () => mode,
			clearDictationBuffer: () => { buffer.length = 0; },
			injectText: async (t: string) => { injected.push(t); return true; },
		};
		const md = attachMeetingDictation({
			session, provider, notePathFor: (d) => join(dir, `notes/meeting-${d}.md`),
			onExitByVoice: () => { exitedByVoice++; }, log: () => {},
		});
		return { md, provider, buffer, injected, get mode() { return mode; }, get exitedByVoice() { return exitedByVoice; } };
	}

	it('writes each sentence to the note and exits on the phrase', async () => {
		const t = setup();
		await t.md.enter();
		assert.equal(t.mode, 'transcription');
		const path = t.md.notePath!;
		t.provider.onTranscript('first point', undefined);
		t.provider.onTranscript('second point', undefined);
		t.provider.onTranscript('Sutando, come back', undefined);
		await tick();
		assert.equal(t.mode, 'agent');
		assert.equal(t.exitedByVoice, 1);
		const note = readFileSync(path, 'utf-8');
		assert.match(note, /^---\ntitle: Meeting notes/);
		assert.match(note, /## Transcript/);
		assert.match(note, /- \[\d\d:\d\d:\d\d\] first point\n- \[\d\d:\d\d:\d\d\] second point\n$/);
		assert.doesNotMatch(note, /come back/);
		assert.equal(t.injected.length, 1);
		assert.match(t.injected[0], /2 lines/);
		assert.match(t.injected[0], /<MEETING_TRANSCRIPT_START>\nfirst point\nsecond point\n<MEETING_TRANSCRIPT_END>$/, 'the agent gets what was said');
	});

	it('keeps the words spoken before the exit phrase in the same segment', async () => {
		const t = setup();
		await t.md.enter();
		const path = t.md.notePath!;
		t.provider.onTranscript("We ship on Friday. Sutando, come back.", undefined);
		await tick();
		assert.equal(t.mode, 'agent');
		assert.match(readFileSync(path, 'utf-8'), /\] We ship on Friday\.\n$/);
	});

	it('exit() from the menu returns to agent mode without the voice callback', async () => {
		const t = setup();
		await t.md.enter();
		await t.md.exit();
		assert.equal(t.mode, 'agent');
		assert.equal(t.exitedByVoice, 0);
		await t.md.exit(); // idempotent
		assert.equal(t.injected.length, 1);
	});
});

describe('meeting entry gate', () => {
	it('enters after the confirmation turn, not the tool-call turn', () => {
		let fired = 0;
		const g = createMeetingEntryGate({ fallbackMs: 10_000, onFire: () => fired++ });
		g.schedule();
		g.noteTurnCompleted(); // turn that carried switch_mode
		assert.equal(fired, 0);
		g.noteTurnCompleted(); // "I have switched to meeting mode."
		assert.equal(fired, 1);
		g.noteTurnCompleted();
		assert.equal(fired, 1, 'fires once');
	});

	it('falls back when no confirmation turn arrives', async () => {
		let fired = 0;
		const g = createMeetingEntryGate({ fallbackMs: 10, onFire: () => fired++ });
		g.schedule();
		g.noteTurnCompleted();
		await tick(20);
		assert.equal(fired, 1);
	});

	it('cancel (switching back before entry) prevents entering', async () => {
		let fired = 0;
		const g = createMeetingEntryGate({ fallbackMs: 10, onFire: () => fired++ });
		g.schedule();
		g.cancel();
		g.noteTurnCompleted();
		g.noteTurnCompleted();
		await tick(20);
		assert.equal(fired, 0);
		assert.equal(g.pending, false);
	});
});

describe('meeting transcript carried back to the agent', () => {
	it('keeps a long meeting\'s end and says where the rest is', async () => {
		const { meetingEndedContext } = await import('../src/meeting-dictation.js');
		const long = Array.from({ length: 2000 }, (_, i) => `line ${i} ${'x'.repeat(30)}`);
		const ctx = meetingEndedContext('/n.md', long);
		assert.match(ctx, /only the end of the meeting/);
		assert.ok(ctx.includes('line 1999 '));
		assert.ok(!ctx.includes('line 0 '));
		assert.match(ctx, /<MEETING_TRANSCRIPT_START>\nline \d+ x/, 'starts on a whole line');
	});
});
