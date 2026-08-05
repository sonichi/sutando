import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { setTimeout as delay } from 'node:timers/promises';
import {
	downsample,
	float32ToInt16,
	int16ToFloat32,
	classifyMicError,
	classifyMicErrorCode,
	describeAgentFailure,
	VoiceTransport,
	CLOSE_CODE_CLIENT_BUSY,
	CLOSE_CODE_SUPERSEDED_BY_TAKEOVER,
	CONNECT_TIMEOUT_MS,
	AGENT_STATE_LEGACY_MS,
	VOICE_FAILURE_REMEDIATION,
	type VoiceConnectFailure,
	type VoiceTransportOptions,
	type AgentStateV1,
} from '../src/web-voice-transport.js';

// Feed a JSON frame straight into the private router. The frame path touches no
// browser API (flushPlayback over an empty queue is a no-op, ArrayBuffer is a
// Node global), so the turn.end/turn.interrupted contract is Node-testable.
function feed(t: VoiceTransport, obj: unknown): void {
	(t as any).onMessage({ data: JSON.stringify(obj) });
}

describe('web-voice-transport DSP', () => {
	it('downsample: identity when rates match', () => {
		const input = new Float32Array([0.1, -0.2, 0.3, -0.4]);
		const out = downsample(input, 48000, 48000);
		assert.equal(out, input, 'returns the same reference when fromRate === toRate');
	});

	it('downsample: 48k → 16k reduces length by ~3x', () => {
		const input = new Float32Array(48).fill(0.5);
		const out = downsample(input, 48000, 16000);
		assert.equal(out.length, 16); // floor(48 / (48000/16000)) = floor(48/3) = 16
	});

	it('downsample: linear interpolation between samples', () => {
		// fromRate 2 → toRate 1: ratio 2, out[i] = input[2i] (frac 0)
		const input = new Float32Array([0, 1, 0, 1, 0, 1]);
		const out = downsample(input, 2, 1);
		assert.equal(out.length, 3);
		assert.deepEqual(Array.from(out), [0, 0, 0]);
	});

	it('downsample: last-sample edge uses 0 for the missing neighbour', () => {
		// ratio 1.5 → pos for last index can land on idx = len-1 with frac>0;
		// input[idx+1] || 0 must not throw / NaN.
		const input = new Float32Array([1, 1, 1, 1]);
		const out = downsample(input, 3, 2);
		assert.ok(out.every((v) => Number.isFinite(v)), 'no NaN at the tail');
	});

	it('float32ToInt16: full-scale + clamp', () => {
		const i16 = float32ToInt16(new Float32Array([1, -1, 0, 2, -2]));
		assert.equal(i16[0], 0x7fff); // +1 → +32767
		assert.equal(i16[1], -0x8000); // -1 → -32768
		assert.equal(i16[2], 0);
		assert.equal(i16[3], 0x7fff); // clamps > 1
		assert.equal(i16[4], -0x8000); // clamps < -1
	});

	it('int16ToFloat32: little-endian decode', () => {
		const buf = new ArrayBuffer(4);
		const dv = new DataView(buf);
		dv.setInt16(0, 32767, true);
		dv.setInt16(2, -32768, true);
		const f = int16ToFloat32(buf);
		assert.ok(Math.abs(f[0] - 0.99997) < 1e-3);
		assert.equal(f[1], -1);
	});

	it('round-trip f32 → i16 → f32 is near-identity (< 2 LSB)', () => {
		// Bound is 2 LSB, not 1: the encode scale (×0x7FFF) and decode scale
		// (÷0x8000) are deliberately asymmetric (faithful to web-client), which
		// adds a small systematic error growing with amplitude on top of the
		// 0.5-LSB quantization. Both are preserved intentionally.
		const src = new Float32Array([0, 0.25, -0.25, 0.5, -0.5, 0.9, -0.9]);
		const back = int16ToFloat32(float32ToInt16(src).buffer);
		for (let i = 0; i < src.length; i++) {
			assert.ok(Math.abs(back[i] - src[i]) < 2 / 32768, `sample ${i} within 2 LSB`);
		}
	});

	it('int16ToFloat32: empty buffer → empty array (playChunk guard)', () => {
		assert.equal(int16ToFloat32(new ArrayBuffer(0)).length, 0);
	});
});

describe('web-voice-transport classifyMicError', () => {
	it('permission-class → settings guidance', () => {
		for (const n of ['NotAllowedError', 'SecurityError']) {
			assert.match(classifyMicError(n), /denied|browser settings/i);
		}
	});
	it('busy-class → close-other-app guidance (not a permission message)', () => {
		for (const n of ['NotReadableError', 'AbortError']) {
			const m = classifyMicError(n);
			assert.match(m, /in use|another app/i);
			assert.doesNotMatch(m, /denied/i);
		}
	});
	it('absent-class → connect-a-device guidance', () => {
		for (const n of ['NotFoundError', 'OverconstrainedError']) {
			assert.match(classifyMicError(n), /no microphone found/i);
		}
	});
	it('unknown/undefined → generic retry with the name echoed', () => {
		assert.match(classifyMicError('WeirdError'), /WeirdError/);
		assert.match(classifyMicError(undefined), /unknown/);
	});
});

describe('web-voice-transport turn lifecycle', () => {
	it('turn.end fires onTurnEnd and does NOT flush/interrupt (final audio drains)', () => {
		let ended = 0;
		let interrupted = 0;
		const t = new VoiceTransport({
			onTurnEnd: () => ended++,
			onInterrupted: () => interrupted++,
		});
		feed(t, { type: 'turn.end' });
		assert.equal(ended, 1);
		assert.equal(interrupted, 0, 'turn.end must not trigger a barge-in flush');
	});

	it('turn.interrupted fires onInterrupted only (barge-in cut-off)', () => {
		let ended = 0;
		let interrupted = 0;
		const t = new VoiceTransport({
			onTurnEnd: () => ended++,
			onInterrupted: () => interrupted++,
		});
		// Must not throw flushing an empty playback queue.
		feed(t, { type: 'turn.interrupted' });
		assert.equal(interrupted, 1);
		assert.equal(ended, 0, 'turn.interrupted is distinct from turn.end');
	});

	it('session.config negotiates rates via onSessionConfig', () => {
		let rates: [number, number] | null = null;
		const t = new VoiceTransport({ onSessionConfig: (i, o) => (rates = [i, o]) });
		feed(t, { type: 'session.config', audioFormat: { inputSampleRate: 8000, outputSampleRate: 48000 } });
		assert.deepEqual(rates, [8000, 48000]);
	});

	it('every frame is also forwarded raw to onProtocolMessage', () => {
		const seen: string[] = [];
		const t = new VoiceTransport({ onProtocolMessage: (m) => seen.push(m.type) });
		feed(t, { type: 'image', base64: 'x' });
		feed(t, { type: 'turn.end' });
		assert.deepEqual(seen, ['image', 'turn.end']);
	});

	it('disconnect()/close() are idempotent with no live session (no throw)', () => {
		const t = new VoiceTransport();
		assert.doesNotThrow(() => t.disconnect());
		assert.doesNotThrow(() => t.close());
		assert.doesNotThrow(() => t.disconnect());
		assert.equal(t.connected, false);
	});
});

// ─── Capability gaps closed so the web UI can adopt this module ──────────────
//
// Each of these existed in web-client's inline transport and NOT here, which is
// why the UI could not switch over without a behavior change. They are pinned
// so the switch (and any later tidy) cannot quietly drop them again.

describe('web-voice-transport close info (reconnect policy lives in the surface)', () => {
	it('close carries the raw code and reason, not just a status string', () => {
		const seen: Array<{ status: string; detail?: string; close?: { code: number; reason: string } }> = [];
		const t = new VoiceTransport({
			onStatus: (status, detail, close) => seen.push({ status, detail, close }),
		});

		// Drive ws.onclose the way the browser would, without a real socket.
		(t as any).ws = null;
		(t as any).teardownAudio = () => {};
		(t as any).status('closed', 'Disconnected', { code: 4000, reason: 'goodbye' });

		const closed = seen.find(s => s.status === 'closed');
		assert.ok(closed, 'a closed status must be emitted');
		assert.equal(closed!.close?.code, 4000);
		assert.equal(closed!.close?.reason, 'goodbye');
	});

	it('the surface can distinguish a clean goodbye from an unexpected drop', () => {
		// This is the whole reason the code is exposed: web-client treats 4000
		// (and a user-initiated disconnect) as clean, and everything else as a
		// drop that should trigger its reconnect ladder.
		const isClean = (code: number) => code === 4000;
		assert.equal(isClean(4000), true);
		assert.equal(isClean(1006), false, 'abnormal closure must NOT read as clean');
	});
});

describe('web-voice-transport debug sink (feeds the panel + downloadable dump)', () => {
	it('forwards protocol frames to onDebug with the event channel', () => {
		const lines: Array<[string, string | undefined]> = [];
		const t = new VoiceTransport({ onDebug: (msg, kind) => lines.push([msg, kind]) });

		feed(t, { type: 'turn.end' });

		const evt = lines.find(([, kind]) => kind === 'event');
		assert.ok(evt, 'a JSON frame must produce an event-channel debug line');
		assert.match(evt![0], /turn\.end/, 'the frame itself must appear in the trace');
	});

	it('reports an unparseable text frame rather than dropping it silently', () => {
		const lines: Array<[string, string | undefined]> = [];
		const t = new VoiceTransport({ onDebug: (msg, kind) => lines.push([msg, kind]) });

		(t as any).onMessage({ data: 'not json at all' });

		assert.ok(
			lines.some(([msg, kind]) => kind === 'warn' && /bad json/i.test(msg)),
			'a malformed frame must be visible in the debug trace',
		);
	});

	it('is entirely optional — no onDebug means no throw', () => {
		const t = new VoiceTransport({});
		assert.doesNotThrow(() => feed(t, { type: 'turn.end' }));
		assert.doesNotThrow(() => (t as any).onMessage({ data: 'nope' }));
	});
});

describe('web-voice-transport mic-error wording matches the shipped web UI', () => {
	it('the unclassified case echoes the underlying browser message', () => {
		// web-client showed the raw DOMException text here; this module used to
		// drop it, which would have made the guidance strictly worse for exactly
		// the failures nobody has classified yet.
		const m = classifyMicError('WeirdError', 'device exploded');
		assert.match(m, /WeirdError/);
		assert.match(m, /device exploded/);
		assert.match(m, /Click Connect to retry/);
	});

	it('falls back to a concrete phrase when the browser gives no message', () => {
		const m = classifyMicError('WeirdError');
		assert.match(m, /could not start capture/);
		assert.doesNotMatch(m, /undefined/, 'must never render the string "undefined" at a user');
	});

	it('classified cases are unaffected by the added message argument', () => {
		assert.match(classifyMicError('NotAllowedError', 'ignored'), /denied/i);
		assert.match(classifyMicError('NotFoundError', 'ignored'), /no microphone found/i);
	});
});

// ═════════════════════════════════════════════════════════════════════════════
// Step 15/18 state-machine coverage (impl plan WS1; amendments R10/R12/S6/W5/
// X6/Z6). The class is driven from Node through the wsFactory seam plus
// minimal AudioContext/getUserMedia stand-ins — no browser required.
// ═════════════════════════════════════════════════════════════════════════════

class FakeMicTrack {
	stopped = false;
	enabled = true;
	stop(): void {
		this.stopped = true;
	}
}

class FakeMediaStream {
	tracks = [new FakeMicTrack()];
	getTracks(): FakeMicTrack[] {
		return this.tracks;
	}
	getAudioTracks(): FakeMicTrack[] {
		return this.tracks;
	}
}

class FakeAudioContext {
	static created: FakeAudioContext[] = [];
	/** State the NEXT constructed context starts in ('running' | 'suspended'). */
	static nextState = 'running';
	/** Optional gate awaited inside resume() — lets tests park an attempt
	 *  inside the exact `await` R10 fences. */
	static resumeHook: (() => Promise<void>) | null = null;

	state: string;
	sampleRate = 48000;
	currentTime = 0;
	destination = {};
	bufferSourcesStarted = 0;

	constructor() {
		this.state = FakeAudioContext.nextState;
		FakeAudioContext.nextState = 'running';
		FakeAudioContext.created.push(this);
	}
	async resume(): Promise<void> {
		if (FakeAudioContext.resumeHook) await FakeAudioContext.resumeHook();
		this.state = 'running';
	}
	close(): void {
		this.state = 'closed';
	}
	createMediaStreamSource(): any {
		return { connect() {} };
	}
	createScriptProcessor(): any {
		return { onaudioprocess: null, connect() {}, disconnect() {} };
	}
	createGain(): any {
		return { gain: { value: 0 }, connect() {} };
	}
	createAnalyser(): any {
		return { fftSize: 0, connect() {} };
	}
	createBuffer(_ch: number, len: number, rate: number): any {
		return { duration: len / rate, getChannelData: () => new Float32Array(len) };
	}
	createBufferSource(): any {
		return {
			buffer: null,
			playbackRate: { value: 1 },
			connect() {},
			start: () => {
				this.bufferSourcesStarted++;
			},
			stop() {},
			onended: null,
		};
	}
}

class FakeSocket {
	static instances: FakeSocket[] = [];
	url: string;
	binaryType = '';
	readyState = 0; // CONNECTING
	sent: Array<ArrayBuffer | string> = [];
	closeCalls = 0;
	onopen: (() => void) | null = null;
	onmessage: ((ev: { data: unknown }) => void) | null = null;
	onerror: (() => void) | null = null;
	onclose: ((ev: { code: number; reason: string }) => void) | null = null;

	constructor(url: string) {
		this.url = url;
		FakeSocket.instances.push(this);
	}
	send(data: ArrayBuffer | string): void {
		this.sent.push(data);
	}
	close(): void {
		this.closeCalls++;
		this.readyState = 3; // CLOSED
	}
	// ── test drivers ──
	open(): void {
		this.readyState = 1; // OPEN
		this.onopen?.();
	}
	message(obj: unknown): void {
		this.onmessage?.({ data: JSON.stringify(obj) });
	}
	binary(buf: ArrayBuffer): void {
		this.onmessage?.({ data: buf });
	}
	error(): void {
		this.onerror?.();
	}
	serverClose(code: number, reason = ''): void {
		this.readyState = 3;
		this.onclose?.({ code, reason });
	}
}

// Browser globals the class touches. Each test FILE runs in its own process
// under the node:test runner, so this stubbing cannot leak into other suites.
let gumImpl: () => Promise<FakeMediaStream>;
Object.defineProperty(globalThis, 'AudioContext', {
	value: FakeAudioContext,
	configurable: true,
	writable: true,
});
Object.defineProperty(globalThis, 'navigator', {
	value: { mediaDevices: { getUserMedia: () => gumImpl() } },
	configurable: true,
	writable: true,
});

interface SeenStatus {
	status: string;
	detail?: string;
	close?: { code: number; reason: string };
}

function harness(opts: Partial<VoiceTransportOptions> = {}) {
	const statuses: SeenStatus[] = [];
	const failures: VoiceConnectFailure[] = [];
	const micErrors: Array<{ name: string; message: string; friendly: string }> = [];
	const frames: AgentStateV1[] = [];
	const t = new VoiceTransport({
		connectTimeoutMs: 20,
		agentStateLegacyMs: 20,
		wsFactory: (url: string) => new FakeSocket(url) as unknown as WebSocket,
		onStatus: (status, detail, close) => statuses.push({ status, detail, close }),
		onConnectFailure: (f) => failures.push(f),
		onMicError: (name, message, friendly) => micErrors.push({ name, message, friendly }),
		onAgentState: (s) => frames.push(s),
		...opts,
	});
	const sock = (): FakeSocket => FakeSocket.instances[FakeSocket.instances.length - 1];
	return { t, statuses, failures, micErrors, frames, sock };
}

/** Drive a transport to fully-live over the newest fake socket. */
async function goLive(h: ReturnType<typeof harness>): Promise<FakeSocket> {
	await h.t.connect('ws://fake:9900/');
	const s = h.sock();
	s.open();
	await delay(5); // let handleOpen's startMic await settle
	assert.ok(
		h.statuses.some((x) => x.status === 'live' && x.detail === 'Live — speak now'),
		'harness precondition: attempt must reach live',
	);
	return s;
}

beforeEach(() => {
	FakeSocket.instances = [];
	FakeAudioContext.created = [];
	FakeAudioContext.nextState = 'running';
	FakeAudioContext.resumeHook = null;
	gumImpl = async () => new FakeMediaStream();
});

describe('classifyMicErrorCode (Step 15 — machine-readable class)', () => {
	it('partitions the DOMException names exactly like classifyMicError', () => {
		for (const n of ['NotAllowedError', 'SecurityError']) {
			assert.equal(classifyMicErrorCode(n), 'permission');
		}
		for (const n of ['NotReadableError', 'AbortError', 'NotFoundError', 'OverconstrainedError']) {
			assert.equal(classifyMicErrorCode(n), 'device');
		}
		assert.equal(classifyMicErrorCode('WeirdError'), 'unknown');
		assert.equal(classifyMicErrorCode(undefined), 'unknown');
	});
});

describe('mute/deafen call controls (Step 15 — reconciled from the cinny surface)', () => {
	it('setMicMuted gates the capture send path and flips the track', async () => {
		const h = harness();
		const s = await goLive(h);
		const processor = (h.t as any).processor;
		assert.ok(processor?.onaudioprocess, 'capture processor must be wired');
		const fakeEvent = { inputBuffer: { getChannelData: () => new Float32Array([0.5, -0.5, 0.25, -0.25]) } };

		processor.onaudioprocess(fakeEvent);
		const sentBefore = s.sent.length;
		assert.ok(sentBefore > 0, 'unmuted capture must send PCM');

		h.t.setMicMuted(true);
		processor.onaudioprocess(fakeEvent);
		assert.equal(s.sent.length, sentBefore, 'muted capture must not send');
		const track = ((h.t as any).micStream as FakeMediaStream).getAudioTracks()[0];
		assert.equal(track.enabled, false, 'OS mic indicator: track disabled while muted');

		h.t.setMicMuted(false);
		processor.onaudioprocess(fakeEvent);
		assert.ok(s.sent.length > sentBefore, 'unmute resumes sending');
		assert.equal(track.enabled, true);
		h.t.disconnect();
	});

	it('setDeafened drops incoming playback (and undeafen restores it)', async () => {
		const h = harness();
		const s = await goLive(h);
		const ctx = FakeAudioContext.created[FakeAudioContext.created.length - 1];

		s.binary(new Int16Array([1000, -1000, 500]).buffer);
		assert.equal(ctx.bufferSourcesStarted, 1, 'undeafened audio plays');

		h.t.setDeafened(true);
		s.binary(new Int16Array([1000, -1000, 500]).buffer);
		assert.equal(ctx.bufferSourcesStarted, 1, 'deafened audio is dropped');

		h.t.setDeafened(false);
		s.binary(new Int16Array([1000, -1000, 500]).buffer);
		assert.equal(ctx.bufferSourcesStarted, 2, 'undeafen resumes playback');
		h.t.disconnect();
	});
});

describe('connect timeout (Step 18 — design 1e)', () => {
	it('exports the 6s default; the constructor can override it', () => {
		assert.equal(CONNECT_TIMEOUT_MS, 6000);
		assert.equal(AGENT_STATE_LEGACY_MS, 3000);
	});

	it('no onopen within the window → latched error + timeout failure; the self-inflicted close never emits closed', async () => {
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		const s = h.sock();
		await delay(45); // > connectTimeoutMs
		assert.equal(h.statuses[h.statuses.length - 1].status, 'error');
		assert.equal(h.statuses[h.statuses.length - 1].detail, 'Connection timed out');
		assert.equal(h.failures.length, 1);
		assert.equal(h.failures[0].kind, 'timeout');
		assert.equal(h.failures[0].remediation, VOICE_FAILURE_REMEDIATION.timeout);
		assert.ok(s.closeCalls >= 1, 'socket closed on timeout');

		s.serverClose(1006); // the close the timeout triggered
		assert.ok(!h.statuses.some((x) => x.status === 'closed'), 'terminal latch: no closed after timeout');
		assert.equal(h.failures.length, 1, 'exactly one failure per attempt');
	});

	it('timer is cleared on open — a live session never times out retroactively', async () => {
		const h = harness();
		const s = await goLive(h);
		await delay(45);
		assert.ok(!h.statuses.some((x) => x.status === 'error'), 'no timeout error after open');
		h.t.disconnect();
		assert.equal(s.closeCalls, 1);
	});
});

describe('attempt-generation fencing (Step 18 / R10)', () => {
	it('a stale socket\'s onclose cannot clobber a newer attempt', async () => {
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		const s1 = h.sock();
		s1.open();
		await delay(5);
		await h.t.connect('ws://fake:9900/'); // second attempt supersedes
		const s2 = h.sock();
		assert.notEqual(s1, s2);
		assert.ok(s1.closeCalls >= 1, 'superseded socket is closed');
		const before = h.statuses.length;
		s1.serverClose(1006, 'stale'); // stale close event
		assert.equal(h.statuses.length, before, 'stale onclose is silently discarded');
		assert.ok(!h.statuses.some((x) => x.status === 'closed'));
		h.t.disconnect();
	});

	it('slow startMic resolving after a second connect() does not touch the new attempt', async () => {
		const streams: FakeMediaStream[] = [];
		let releaseFirst!: () => void;
		const firstGum = new Promise<void>((res) => (releaseFirst = res));
		let call = 0;
		gumImpl = async () => {
			call++;
			const stream = new FakeMediaStream();
			streams.push(stream);
			if (call === 1) await firstGum; // park attempt 1 in the permission prompt
			return stream;
		};
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		h.sock().open();
		await delay(5); // attempt 1 parked inside getUserMedia

		await h.t.connect('ws://fake:9900/'); // supersedes while the prompt is up
		h.sock().open();
		await delay(5);
		const liveCount = h.statuses.filter((x) => x.detail === 'Live — speak now').length;
		assert.equal(liveCount, 1, 'only the new attempt reports live');

		releaseFirst(); // the old prompt finally resolves
		await delay(5);
		assert.equal(streams.length, 2);
		assert.equal(streams[0].tracks[0].stopped, true, 'stale grant is stopped — no capture leak');
		assert.equal(streams[1].tracks[0].stopped, false, 'new attempt keeps its mic');
		assert.equal((h.t as any).micStream, streams[1], 'instance mic belongs to the new attempt');
		assert.equal(
			h.statuses.filter((x) => x.detail === 'Live — speak now').length,
			liveCount,
			'stale continuation adds no status',
		);
		h.t.disconnect();
	});

	it('disconnect during the connect-side AudioContext.resume await → no replacement socket', async () => {
		FakeAudioContext.nextState = 'suspended';
		let release!: () => void;
		FakeAudioContext.resumeHook = () => new Promise((res) => (release = res));
		const h = harness();
		const p = h.t.connect('ws://fake:9900/'); // parks inside resume()
		await delay(2);
		assert.equal(FakeSocket.instances.length, 0, 'no socket yet while resume pending');
		h.t.disconnect();
		release();
		await p;
		await delay(2);
		assert.equal(FakeSocket.instances.length, 0, 'R10: stale continuation must not create a socket');
		assert.deepEqual(
			h.statuses.map((x) => x.status),
			['connecting', 'closed'],
			'S6: exactly one synchronous closed; UI not stuck in connecting',
		);
	});

	it('disconnect during getUserMedia → grant stopped, exactly one closed, no live', async () => {
		let release!: () => void;
		const gate = new Promise<void>((res) => (release = res));
		const streams: FakeMediaStream[] = [];
		gumImpl = async () => {
			const stream = new FakeMediaStream();
			streams.push(stream);
			await gate;
			return stream;
		};
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		h.sock().open();
		await delay(5); // parked in the prompt
		h.t.disconnect();
		release();
		await delay(5);
		assert.equal(streams[0].tracks[0].stopped, true, 'no mic capture outlives the attempt');
		assert.ok(!h.statuses.some((x) => x.detail === 'Live — speak now'));
		assert.equal(h.statuses.filter((x) => x.status === 'closed').length, 1);
	});
});

describe('user disconnect() (Step 18 / S6)', () => {
	it('while connecting: synchronous single closed, socket close, timer cleared', async () => {
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		const s = h.sock();
		h.t.disconnect(); // before open
		assert.equal(h.statuses[h.statuses.length - 1].status, 'closed');
		assert.equal(s.closeCalls, 1);
		s.serverClose(1006); // fenced — must add nothing
		await delay(45); // past the connect timeout — timer must be cleared
		assert.equal(h.statuses.filter((x) => x.status === 'closed').length, 1, 'exactly one closed');
		assert.ok(!h.statuses.some((x) => x.status === 'error'), 'no late timeout after disconnect');
	});

	it('while live: synchronous single closed + full teardown; double disconnect adds nothing', async () => {
		const h = harness();
		const s = await goLive(h);
		const track = ((h.t as any).micStream as FakeMediaStream).getAudioTracks()[0];
		h.t.disconnect();
		assert.equal(h.statuses[h.statuses.length - 1].status, 'closed');
		assert.equal(track.stopped, true, 'mic stopped');
		assert.equal((h.t as any).statsTimer, null, 'stats stopped');
		s.serverClose(1000);
		h.t.disconnect(); // idempotent
		assert.equal(h.statuses.filter((x) => x.status === 'closed').length, 1, 'exactly one closed');
	});

	it('after a latched terminal error: cleanup only — the error status is preserved', async () => {
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		await delay(45); // timeout → latched error
		assert.equal(h.statuses[h.statuses.length - 1].status, 'error');
		h.t.disconnect();
		assert.equal(
			h.statuses[h.statuses.length - 1].status,
			'error',
			'S6: the terminal-error path stays separate — no closed overwrite',
		);
	});
});

describe('`agent.state` client handling (Step 18 — design 1a′)', () => {
	const frame = (over: Partial<AgentStateV1>): AgentStateV1 => ({
		type: 'agent.state',
		v: 1,
		initialized: true,
		upstream: 'live',
		clientAttached: true,
		...over,
	});

	it('legacy server: no frame within the window ⇒ behavior identical to today', async () => {
		const h = harness();
		const s = await goLive(h);
		assert.equal(h.t.agentStateSupport, 'unknown');
		await delay(45); // > agentStateLegacyMs
		assert.equal(h.t.agentStateSupport, 'legacy');
		assert.deepEqual(
			h.statuses.map((x) => x.status),
			['connecting', 'live', 'live'],
			'exactly the pre-protocol status sequence — no error, no extra states',
		);
		assert.equal(h.failures.length, 0);
		assert.equal(h.frames.length, 0);
		h.t.disconnect();
		void s;
	});

	it('connecting/backoff frames are progress detail, never an error', async () => {
		const h = harness();
		const s = await goLive(h);
		s.message(frame({ upstream: 'connecting' }));
		assert.equal(h.statuses[h.statuses.length - 1].status, 'live');
		assert.equal(h.statuses[h.statuses.length - 1].detail, 'Waking up…');
		s.message(frame({ upstream: 'backoff' }));
		assert.equal(h.statuses[h.statuses.length - 1].status, 'live');
		assert.equal(h.statuses[h.statuses.length - 1].detail, 'Reconnecting to the model…');
		assert.equal(h.failures.length, 0, 'backoff must not produce a failure');
		assert.ok(!h.statuses.some((x) => x.status === 'error'));
		// idle→connecting→live is the normal wake-up: live after progress restores the live detail
		s.message(frame({ upstream: 'live' }));
		assert.equal(h.statuses[h.statuses.length - 1].detail, 'Live — speak now');
		assert.equal(h.t.agentStateSupport, 'v1');
		assert.equal(h.frames.length, 3, 'every frame forwarded to onAgentState');
		h.t.disconnect();
	});

	it('upstream failed = terminal CLIENT transition: teardown, close, latched classified error, suppressed onclose', async () => {
		const h = harness();
		const s = await goLive(h);
		const track = ((h.t as any).micStream as FakeMediaStream).getAudioTracks()[0];
		s.message(frame({ upstream: 'failed', reason: 'upstream-auth', category: 'auth' }));

		const last = h.statuses[h.statuses.length - 1];
		assert.equal(last.status, 'error');
		assert.match(last.detail!, /credential/i, 'classified auth detail');
		assert.equal(track.stopped, true, 'mic stopped — not streaming behind the error card');
		assert.equal((h.t as any).statsTimer, null, 'stats stopped');
		assert.ok(s.closeCalls >= 1, 'client closes the socket itself (server stays reachable)');
		assert.equal((h.t as any).ws, null);

		assert.equal(h.failures.length, 1);
		assert.equal(h.failures[0].kind, 'agent-failed');
		assert.equal(h.failures[0].reason, 'upstream-auth');
		assert.equal(h.failures[0].category, 'auth');
		assert.match(h.failures[0].remediation, /key|voice setup/i);

		s.serverClose(1000); // the self-inflicted close
		assert.ok(!h.statuses.some((x) => x.status === 'closed'), 'suppressed — error stays latched');
	});

	it('describeAgentFailure classifies auth/quota/network/other', () => {
		assert.match(describeAgentFailure('r', 'auth').detail, /credential/i);
		assert.match(describeAgentFailure('r', 'quota').detail, /quota|credits/i);
		assert.match(describeAgentFailure('r', 'network').detail, /reach/i);
		assert.match(describeAgentFailure('r', undefined).detail, /upstream failed/i);
	});
});

describe('close-code decoding (Step 18 — W5 client-busy 4409 + superseded 4410)', () => {
	it('4409 → terminal client-busy error with close info + take-over remediation', async () => {
		const h = harness();
		const s = await goLive(h);
		s.serverClose(CLOSE_CODE_CLIENT_BUSY, 'client-busy');
		const last = h.statuses[h.statuses.length - 1];
		assert.equal(last.status, 'error');
		assert.equal(last.close?.code, 4409);
		assert.equal(h.failures.length, 1);
		assert.equal(h.failures[0].kind, 'client-busy');
		assert.equal(h.failures[0].close?.code, 4409);
		assert.match(h.failures[0].detail, /in use/i);
		assert.match(h.failures[0].remediation, /take over/i);
		assert.ok(!h.statuses.some((x) => x.status === 'closed'), 'not a plain close');
	});

	it('4410 → its own terminal superseded state, no connect failure, disconnect preserves it', async () => {
		const h = harness();
		const s = await goLive(h);
		const track = ((h.t as any).micStream as FakeMediaStream).getAudioTracks()[0];
		s.serverClose(CLOSE_CODE_SUPERSEDED_BY_TAKEOVER, 'superseded-by-takeover');
		const last = h.statuses[h.statuses.length - 1];
		assert.equal(last.status, 'superseded');
		assert.equal(last.close?.code, 4410);
		assert.equal(track.stopped, true, 'audio torn down for the moved call');
		assert.equal(h.failures.length, 0, 'a takeover is not a connect failure');
		h.t.disconnect();
		assert.equal(
			h.statuses[h.statuses.length - 1].status,
			'superseded',
			'terminal latch: no closed overwrite',
		);
	});
});

describe('pre-open failures (Step 18 / Z6 — the browser-observable kind is connect-error)', () => {
	it('onerror before open → latched connect-error; the trailing 1006 close is suppressed', async () => {
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		const s = h.sock();
		s.error();
		assert.equal(h.statuses[h.statuses.length - 1].status, 'error');
		assert.equal(h.failures.length, 1);
		assert.equal(h.failures[0].kind, 'connect-error');
		s.serverClose(1006);
		assert.ok(!h.statuses.some((x) => x.status === 'closed'));
		assert.equal(h.failures.length, 1, 'exactly one failure per attempt');
	});

	it('pre-open close without onerror → connect-error carrying the close info', async () => {
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		h.sock().serverClose(1006, '');
		const last = h.statuses[h.statuses.length - 1];
		assert.equal(last.status, 'error');
		assert.equal(last.close?.code, 1006);
		assert.equal(h.failures.length, 1);
		assert.equal(h.failures[0].kind, 'connect-error');
		assert.equal(h.failures[0].close?.code, 1006);
	});

	it('post-open ordinary close stays a plain closed with code/reason (surface reconnect policy)', async () => {
		const h = harness();
		const s = await goLive(h);
		s.serverClose(4000, 'goodbye');
		const last = h.statuses[h.statuses.length - 1];
		assert.equal(last.status, 'closed');
		assert.equal(last.close?.code, 4000);
		assert.equal(last.close?.reason, 'goodbye');
		assert.equal(h.failures.length, 0);
	});
});

describe('mic failure classification (Step 18 / R12 + X6)', () => {
	async function failMic(name: string, message = 'boom') {
		gumImpl = async () => {
			const err = new Error(message);
			err.name = name;
			throw err;
		};
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		const s = h.sock();
		s.open();
		await delay(5);
		return { h, s };
	}

	it('NotAllowedError → mic-permission failure + onMicError + latched error', async () => {
		const { h, s } = await failMic('NotAllowedError');
		assert.equal(h.statuses[h.statuses.length - 1].status, 'error');
		assert.equal(h.micErrors.length, 1);
		assert.match(h.micErrors[0].friendly, /denied/i);
		assert.equal(h.failures.length, 1);
		assert.equal(h.failures[0].kind, 'mic-permission');
		assert.ok(s.closeCalls >= 1, 'no auto-reconnect loop on a hard mic failure');
		s.serverClose(1006);
		assert.ok(!h.statuses.some((x) => x.status === 'closed'), 'latched — no closed overwrite');
	});

	it('NotReadableError → mic-device', async () => {
		const { h } = await failMic('NotReadableError');
		assert.equal(h.failures[0].kind, 'mic-device');
		assert.match(h.failures[0].detail, /in use/i);
	});

	it('X6: unclassified DOM exception → mic-other with Retry remediation (never credential repair)', async () => {
		const { h } = await failMic('SomethingNewError');
		assert.equal(h.failures[0].kind, 'mic-other');
		assert.equal(h.failures[0].remediation, 'Retry.');
		assert.doesNotMatch(h.failures[0].remediation, /key|credential|setup/i);
	});
});

describe('transport public API additions (Steps 15/16/18)', () => {
	it('sendTextInput: false when not open, true + JSON frame when live', async () => {
		const h = harness();
		assert.equal(h.t.sendTextInput('hi'), false);
		const s = await goLive(h);
		const before = s.sent.length;
		assert.equal(h.t.sendTextInput('hello agent'), true);
		const sent = s.sent[s.sent.length - 1];
		assert.equal(typeof sent, 'string');
		assert.deepEqual(JSON.parse(sent as string), { type: 'text_input', text: 'hello agent' });
		assert.equal(s.sent.length, before + 1);
		h.t.disconnect();
	});

	it('setPlaybackRate drives subsequent playback scheduling', async () => {
		const h = harness();
		const s = await goLive(h);
		h.t.setPlaybackRate(1.2);
		s.binary(new Int16Array([100, 200, 300]).buffer);
		assert.equal((h.t as any).playbackRate, 1.2);
		h.t.disconnect();
	});

	it('failure-union remediation table covers every kind exactly once', () => {
		const kinds = [
			'timeout',
			'connect-error',
			'mic-permission',
			'mic-device',
			'mic-other',
			'agent-failed',
			'service-down',
			'client-busy',
		];
		assert.deepEqual(Object.keys(VOICE_FAILURE_REMEDIATION).sort(), [...kinds].sort());
		for (const k of kinds) {
			assert.ok(
				(VOICE_FAILURE_REMEDIATION as Record<string, string>)[k].length > 0,
				k + ' has a remediation hint',
			);
		}
	});
});
