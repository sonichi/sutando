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
	DISCONNECT_CLOSE_TIMEOUT_MS,
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
		const src = {
			buffer: null,
			playbackRate: { value: 1 },
			connect() {},
			start: () => {
				this.bufferSourcesStarted++;
			},
			stop() {},
			onended: null as (() => void) | null,
		};
		this.bufferSources.push(src);
		return src;
	}
	/** Every source created, in order — P7 ended/cancelled split tests fire
	 *  their onended by hand. */
	bufferSources: Array<{ onended: (() => void) | null; stop: () => void }> = [];
	/** P7 D7.5: the transport assigns onstatechange; tests flip `state` and
	 *  call this to simulate a browser lifecycle transition. */
	onstatechange: (() => void) | null = null;
	fireStateChange(): void {
		this.onstatechange?.();
	}
}

class FakeSocket {
	static instances: FakeSocket[] = [];
	url: string;
	binaryType = '';
	readyState = 0; // CONNECTING
	sent: Array<ArrayBuffer | string> = [];
	closeCalls = 0;
	/** P7: settable for bufferedAmount-skip tests (real sockets expose this). */
	bufferedAmount = 0;
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
/** P7 D7.5 input-device fingerprinting seam: tests swap the device list to
 *  drive the devicechange input-filter. */
let enumImpl: () => Promise<Array<{ kind: string; deviceId: string }>>;
const mediaDevicesListeners: Array<{ type: string; h: () => void }> = [];
function fireDeviceChange(): void {
	for (const l of [...mediaDevicesListeners]) if (l.type === 'devicechange') l.h();
}
Object.defineProperty(globalThis, 'AudioContext', {
	value: FakeAudioContext,
	configurable: true,
	writable: true,
});
Object.defineProperty(globalThis, 'navigator', {
	value: {
		mediaDevices: {
			getUserMedia: () => gumImpl(),
			enumerateDevices: () => enumImpl(),
			addEventListener: (type: string, h: () => void) => {
				mediaDevicesListeners.push({ type, h });
			},
			removeEventListener: (type: string, h: () => void) => {
				const i = mediaDevicesListeners.findIndex((l) => l.type === type && l.h === h);
				if (i >= 0) mediaDevicesListeners.splice(i, 1);
			},
		},
	},
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
	enumImpl = async () => [];
	mediaDevicesListeners.length = 0;
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
		assert.equal(DISCONNECT_CLOSE_TIMEOUT_MS, 1500);
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

describe('attempt conclusion invalidates the generation (fix round — server-close-while-parked repro)', () => {
	/** Park getUserMedia behind a gate and capture every granted stream. */
	function parkGum() {
		let release!: () => void;
		const gate = new Promise<void>((res) => (release = res));
		const streams: FakeMediaStream[] = [];
		gumImpl = async () => {
			const stream = new FakeMediaStream();
			streams.push(stream);
			await gate;
			return stream;
		};
		return { release: () => release(), streams };
	}

	it('server close (1006) while getUserMedia is parked → closed stays final, grant stopped, no stats leak', async () => {
		const gum = parkGum();
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		const s = h.sock();
		s.open();
		await delay(5); // attempt parked in the permission prompt
		s.serverClose(1006, 'dropped'); // the server drops while the prompt is up
		assert.equal(h.statuses[h.statuses.length - 1].status, 'closed');
		assert.equal(h.statuses[h.statuses.length - 1].close?.code, 1006);

		gum.release(); // the user then grants the mic — for a dead attempt
		await delay(5);
		assert.equal(
			h.statuses[h.statuses.length - 1].status,
			'closed',
			'the close-derived final status stays — the dead attempt must not resume',
		);
		assert.ok(
			!h.statuses.some((x) => x.detail === 'Live — speak now'),
			'the dead attempt never reports live',
		);
		assert.equal(h.statuses.filter((x) => x.status === 'closed').length, 1, 'exactly one final status');
		assert.equal(gum.streams.length, 1);
		assert.equal(gum.streams[0].tracks[0].stopped, true, 'granted tracks stopped — no capture leak');
		assert.equal((h.t as any).micStream, null, 'no stream captured for the dead attempt');
		assert.equal((h.t as any).statsTimer, null, 'no statsTimer keeps the runner alive');
	});

	it('close 4409 while getUserMedia is parked → latched client-busy error survives the late grant', async () => {
		const gum = parkGum();
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		const s = h.sock();
		s.open();
		await delay(5); // attempt parked in the permission prompt
		s.serverClose(CLOSE_CODE_CLIENT_BUSY, 'client-busy'); // another surface owns the call
		assert.equal(h.statuses[h.statuses.length - 1].status, 'error');
		assert.equal(h.failures.length, 1);
		assert.equal(h.failures[0].kind, 'client-busy');

		gum.release();
		await delay(5);
		assert.equal(
			h.statuses[h.statuses.length - 1].status,
			'error',
			'latched client-busy error survives — never overwritten by live',
		);
		assert.ok(!h.statuses.some((x) => x.detail === 'Live — speak now'));
		assert.ok(!h.statuses.some((x) => x.status === 'closed'), 'terminal latch holds');
		assert.equal(h.failures.length, 1, 'exactly one failure per attempt');
		assert.equal(gum.streams[0].tracks[0].stopped, true, 'granted tracks stopped');
		assert.equal((h.t as any).statsTimer, null, 'no stats leak');
	});

	it('R10: disconnect during startMic\'s INTERNAL resume() await — grant stopped, newer attempt untouched', async () => {
		const streams: FakeMediaStream[] = [];
		gumImpl = async () => {
			const stream = new FakeMediaStream();
			streams.push(stream);
			return stream;
		};
		let release!: () => void;
		const h = harness();
		await h.t.connect('ws://fake:9900/'); // ctx born 'running' → no connect-side resume
		h.sock().open(); // handleOpen → startMic parks at the getUserMedia microtask
		// Suspend the context BEFORE getUserMedia resolves so the await the
		// attempt parks in is startMic's OWN resume() (post-capture), not
		// connect()'s — the R10 fence at that await is otherwise uncovered.
		FakeAudioContext.created[0].state = 'suspended';
		FakeAudioContext.resumeHook = () => new Promise((res) => (release = res));
		await delay(5); // getUserMedia resolved → stream captured → parked in resume()
		assert.equal(streams.length, 1);
		// P7 (wireCaptureGraph): the captured stream is not published to
		// micStream until AFTER the resume fence passes — while parked, the
		// grant is held only by the continuation, so a stale one can't leave
		// even a transient publication behind.
		assert.equal((h.t as any).micStream, null, 'precondition: parked in resume, not yet published');

		h.t.disconnect(); // while parked in resume()
		assert.equal(h.statuses[h.statuses.length - 1].status, 'closed');

		FakeAudioContext.resumeHook = null; // the newer attempt must not park
		await h.t.connect('ws://fake:9900/'); // newer attempt while attempt 1 is still parked
		h.sock().open();
		await delay(5);
		assert.equal(streams.length, 2, 'newer attempt captured its own stream');
		assert.equal((h.t as any).micStream, streams[1], 'newer attempt owns the mic');

		release(); // attempt 1's parked resume finally resolves
		await delay(5);
		assert.equal(streams[0].tracks[0].stopped, true, 'attempt-1 grant stopped, not leaked');
		assert.equal(streams[1].tracks[0].stopped, false, 'stale continuation must not stop the newer mic');
		assert.equal((h.t as any).micStream, streams[1], 'stale continuation must not clobber micStream');
		assert.equal(h.statuses.filter((x) => x.status === 'closed').length, 1, 'exactly one closed emitted');
		assert.equal(
			h.statuses.filter((x) => x.detail === 'Live — speak now').length,
			1,
			'only the newer attempt reports live',
		);
		h.t.disconnect();
	});

	it('a direct second connect() over live flushes the old session\'s scheduled playback', async () => {
		const h = harness();
		const s = await goLive(h);
		s.binary(new Int16Array([1000, -1000, 500]).buffer);
		assert.ok(((h.t as any).activeSources as unknown[]).length > 0, 'precondition: audio scheduled');
		await h.t.connect('ws://fake:9900/'); // no disconnect() in between
		assert.equal(
			((h.t as any).activeSources as unknown[]).length,
			0,
			'old session audio is stopped, not carried into the new attempt',
		);
		assert.equal((h.t as any).nextPlayTime, 0, 'playback clock restarts for the new attempt');
		h.t.disconnect();
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

describe('disconnect() awaitable teardown (T8 — teardown awaited before lease release)', () => {
	it('over a live session: the promise resolves only after the socket\'s real close event', async () => {
		const h = harness();
		const s = await goLive(h);
		let resolved = false;
		const p = h.t.disconnect().then(() => {
			resolved = true;
		});
		assert.equal(
			h.statuses[h.statuses.length - 1].status,
			'closed',
			'the synchronous closed status fires before the promise resolves',
		);
		await delay(10);
		assert.equal(resolved, false, 'not resolved while the close handshake is still in flight');
		s.serverClose(1000); // the async close handshake completes
		await p;
		assert.equal(resolved, true);
		assert.equal(
			h.statuses.filter((x) => x.status === 'closed').length,
			1,
			'the real close event resolves the promise but adds no second status (still fenced)',
		);
	});

	it('close() alias returns the same awaitable completion', async () => {
		const h = harness();
		const s = await goLive(h);
		let resolved = false;
		void h.t.close().then(() => {
			resolved = true;
		});
		await delay(10);
		assert.equal(resolved, false, 'alias also waits for the close handshake');
		s.serverClose(1000);
		await delay(0);
		assert.equal(resolved, true);
	});

	it('with no socket: resolves immediately', async () => {
		const t = new VoiceTransport();
		let resolved = false;
		void t.disconnect().then(() => {
			resolved = true;
		});
		await delay(0);
		assert.equal(resolved, true, 'no socket ⇒ nothing to await');
	});

	it('over an already-CLOSED socket: resolves immediately (no close event will ever fire)', async () => {
		const h = harness();
		const s = await goLive(h);
		s.readyState = 3; // CLOSED under the transport — its event already spent/fenced
		let resolved = false;
		void h.t.disconnect().then(() => {
			resolved = true;
		});
		await delay(0);
		assert.equal(resolved, true, 'already-CLOSED socket ⇒ immediate resolve');
	});

	it('a wedged handshake is bounded: the fallback resolves after disconnectCloseTimeoutMs', async () => {
		const h = harness({ disconnectCloseTimeoutMs: 20 });
		const s = await goLive(h);
		let resolved = false;
		void h.t.disconnect().then(() => {
			resolved = true;
		});
		await delay(5);
		assert.equal(resolved, false, 'still waiting on a close that never comes');
		await delay(40); // past the injected bound
		assert.equal(resolved, true, 'fallback fires — a wedged handshake cannot hang teardown');
		void s; // the fake deliberately never emits close
	});
});

describe('closeSettled() — attempt-conclusion close completion (P1: lease release must await the handshake)', () => {
	it('fresh transport: resolved immediately (no socket ever existed)', async () => {
		const t = new VoiceTransport();
		let settled = false;
		void t.closeSettled().then(() => {
			settled = true;
		});
		await delay(0);
		assert.equal(settled, true);
	});

	it('mic-error while the socket is open → the completion read INSIDE onConnectFailure settles only after the socket close event', async () => {
		gumImpl = async () => {
			const err = new Error('denied');
			err.name = 'NotAllowedError';
			throw err;
		};
		const failures: VoiceConnectFailure[] = [];
		let atFailure: Promise<void> | null = null;
		const h = harness({
			onConnectFailure: (f) => {
				failures.push(f);
				// The contract consumers rely on: the completion is already
				// latched when the failure callback runs.
				atFailure = h.t.closeSettled();
			},
		});
		await h.t.connect('ws://fake:9900/');
		const s = h.sock();
		s.open();
		await delay(5); // startMic throws → latched mic error
		assert.equal(failures[0]?.kind, 'mic-permission');
		assert.ok(atFailure, 'closeSettled() readable from inside onConnectFailure');
		assert.ok(s.closeCalls >= 1, 'transport closed its socket');
		let settled = false;
		void atFailure!.then(() => {
			settled = true;
		});
		await delay(10);
		assert.equal(settled, false, 'not settled while the self-inflicted close handshake is in flight');
		s.serverClose(1006); // the handshake completes
		await delay(0);
		assert.equal(settled, true, 'settles once the socket close event fires');
	});

	it('connect timeout → closeSettled() resolves only after the socket close event', async () => {
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		const s = h.sock();
		await delay(45); // > connectTimeoutMs → latched timeout error
		assert.equal(h.failures[0]?.kind, 'timeout');
		let settled = false;
		void h.t.closeSettled().then(() => {
			settled = true;
		});
		await delay(10);
		assert.equal(settled, false, 'timeout latched but the close handshake is still in flight');
		s.serverClose(1006);
		await delay(0);
		assert.equal(settled, true);
	});

	it('pre-open socket error → closeSettled() resolves only after the trailing close event', async () => {
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		const s = h.sock();
		s.error(); // pre-open → latched connect-error
		assert.equal(h.failures[0]?.kind, 'connect-error');
		let settled = false;
		void h.t.closeSettled().then(() => {
			settled = true;
		});
		await delay(10);
		assert.equal(settled, false, 'error latched but the trailing 1006 close has not fired yet');
		s.serverClose(1006); // the browser's trailing close
		await delay(0);
		assert.equal(settled, true);
	});

	it('upstream-failed teardown → closeSettled() resolves only after the self-inflicted close event', async () => {
		const h = harness();
		const s = await goLive(h);
		s.message({
			type: 'agent.state',
			v: 1,
			initialized: true,
			upstream: 'failed',
			reason: 'upstream-auth',
			category: 'auth',
			clientAttached: true,
		});
		assert.equal(h.failures[0]?.kind, 'agent-failed');
		let settled = false;
		void h.t.closeSettled().then(() => {
			settled = true;
		});
		await delay(10);
		assert.equal(settled, false, 'agent-failed latched but the close handshake is still in flight');
		s.serverClose(1000); // the self-inflicted close completes
		await delay(0);
		assert.equal(settled, true);
	});

	it('server-initiated closes (plain / 4409 / 4410) → closeSettled() captured at the terminal status is already settled', async () => {
		for (const code of [4000, CLOSE_CODE_CLIENT_BUSY, CLOSE_CODE_SUPERSEDED_BY_TAKEOVER]) {
			let captured: Promise<void> | null = null;
			const h = harness({
				onStatus: (status, detail, close) => {
					h.statuses.push({ status, detail, close });
					if (status === 'closed' || status === 'error' || status === 'superseded') {
						captured = h.t.closeSettled();
					}
				},
			});
			const s = await goLive(h);
			s.serverClose(code, 'server-close');
			assert.ok(captured, `code ${code}: terminal status observed`);
			let settled = false;
			void captured!.then(() => {
				settled = true;
			});
			await delay(0); // no close event, no fallback — a microtask must suffice
			assert.equal(settled, true, `code ${code}: socket already closed ⇒ completion already settled`);
		}
	});

	it('bounded fallback: a wedged self-close handshake resolves after disconnectCloseTimeoutMs', async () => {
		gumImpl = async () => {
			const err = new Error('busy');
			err.name = 'NotReadableError';
			throw err;
		};
		const h = harness({ disconnectCloseTimeoutMs: 20 });
		await h.t.connect('ws://fake:9900/');
		h.sock().open();
		await delay(5); // mic-error latch; the fake deliberately never emits its close event
		assert.equal(h.failures[0]?.kind, 'mic-device');
		let settled = false;
		void h.t.closeSettled().then(() => {
			settled = true;
		});
		await delay(5);
		assert.equal(settled, false, 'still awaiting a close that never comes');
		await delay(40); // past the injected bound
		assert.equal(settled, true, 'fallback fires — a wedged handshake cannot hang lease release');
	});

	it('a later disconnect() with the socket already gone returns the still-pending conclusion completion', async () => {
		// Consumer shape: transport-initiated failure → surface ALSO calls
		// disconnect() for cleanup. The returned promise must still cover the
		// in-flight handshake of the socket the latch already closed.
		gumImpl = async () => {
			const err = new Error('denied');
			err.name = 'NotAllowedError';
			throw err;
		};
		const h = harness();
		await h.t.connect('ws://fake:9900/');
		const s = h.sock();
		s.open();
		await delay(5); // mic-error latch: socket closed + nulled by the transport
		assert.equal(h.failures[0]?.kind, 'mic-permission');
		let settled = false;
		void h.t.disconnect().then(() => {
			settled = true;
		});
		await delay(10);
		assert.equal(settled, false, 'disconnect() after the latch still awaits the old handshake');
		s.serverClose(1006);
		await delay(0);
		assert.equal(settled, true);
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

	it('sendClientCommand: false when not open, JSON-serialized frame when live', async () => {
		// A NAMED command interface (no index signature) must assign — the
		// parameter is constrained on `type`, not on Record<string, unknown>.
		interface RetryUpstreamCmd {
			type: 'voice.retryUpstream';
			version: 1;
			voiceSessionId: string;
			clientEpoch: number;
			stalledAttemptEpoch: number;
			requestId: string;
		}
		const h = harness();
		const probe: RetryUpstreamCmd = {
			type: 'voice.retryUpstream',
			version: 1,
			voiceSessionId: 'vs_0',
			clientEpoch: 0,
			stalledAttemptEpoch: 1,
			requestId: 'req-0',
		};
		assert.equal(h.t.sendClientCommand(probe), false);
		const s = await goLive(h);
		const before = s.sent.length;
		const msg: RetryUpstreamCmd = {
			type: 'voice.retryUpstream',
			version: 1,
			voiceSessionId: 'vs_1',
			clientEpoch: 2,
			stalledAttemptEpoch: 7,
			requestId: 'req-1',
		};
		assert.equal(h.t.sendClientCommand(msg), true);
		const sent = s.sent[s.sent.length - 1];
		assert.equal(typeof sent, 'string');
		assert.deepEqual(JSON.parse(sent as string), msg);
		assert.equal(s.sent.length, before + 1);
		h.t.disconnect();
	});

	it('sendClientCommand: a close between the readyState check and send() returns false, not a throw', async () => {
		const h = harness();
		const s = await goLive(h);
		// The real race: readyState is still OPEN when the guard samples it, and
		// the socket closes before send() runs, so send() throws InvalidStateError.
		const before = s.sent.length;
		s.send = () => { throw new Error('InvalidStateError: still in CONNECTING state'); };
		assert.equal(s.readyState, 1, 'guard must pass — otherwise this tests the wrong branch');
		assert.doesNotThrow(() => h.t.sendClientCommand({ type: 'voice.retryUpstream' }));
		assert.equal(h.t.sendClientCommand({ type: 'voice.retryUpstream' }), false, 'a frame that did not go out must report false');
		assert.equal(s.sent.length, before, 'nothing was recorded as sent');
		h.t.disconnect();
	});

	it('sendTextInput: a close between the readyState check and send() returns false, not a throw', async () => {
		const h = harness();
		const s = await goLive(h);
		// The real race: readyState is still OPEN when the guard samples it, and
		// the socket closes before send() runs, so send() throws InvalidStateError.
		const before = s.sent.length;
		s.send = () => { throw new Error('InvalidStateError: still in CONNECTING state'); };
		assert.equal(s.readyState, 1, 'guard must pass — otherwise this tests the wrong branch');
		assert.doesNotThrow(() => h.t.sendTextInput('lost frame'));
		assert.equal(h.t.sendTextInput('lost frame'), false, 'a frame that did not go out must report false');
		assert.equal(s.sent.length, before, 'nothing was recorded as sent');
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

// ═════════════════════════════════════════════════════════════════════════════
// P7 step 1 — audio-progress ledger client hop (D7.1): counters, latched
// episodes, watchdog, audio_health heartbeat; capture recovery FSM (D7.5);
// §D7.0b frame-path budget guard.
// ═════════════════════════════════════════════════════════════════════════════

type FrameEvent = { inputBuffer: { getChannelData: () => Float32Array } };
function frame(samples: Float32Array): FrameEvent {
	return { inputBuffer: { getChannelData: () => samples } };
}
const LOUD = new Float32Array(2048).fill(0.1); // rms 0.1 ≥ 0.02 floor
const QUIET = new Float32Array(2048); // zeros

function proc(h: ReturnType<typeof harness>): (e: FrameEvent) => void {
	const p = (h.t as any).processor;
	assert.ok(p?.onaudioprocess, 'capture processor must be wired');
	return p.onaudioprocess;
}

/** Kill the real 500 ms interval so ticks are driven deterministically. */
function stopRealStats(h: ReturnType<typeof harness>): void {
	const timer = (h.t as any).statsTimer;
	if (timer) clearInterval(timer);
	(h.t as any).statsTimer = null;
}
function tick(h: ReturnType<typeof harness>): void {
	(h.t as any).runStatsTick();
}
function healthFrames(s: FakeSocket): any[] {
	return s.sent
		.filter((x): x is string => typeof x === 'string')
		.map((x) => {
			try {
				return JSON.parse(x);
			} catch {
				return null;
			}
		})
		.filter((m) => m?.t === 'audio_health');
}

describe('P7 D7.1 ledger counters', () => {
	it('scheduled/ended/cancelled split: barge-in cancellations never count as completion', async () => {
		const h = harness();
		const s = await goLive(h);
		const ctx = FakeAudioContext.created[0];
		s.binary(new Int16Array([1000, -1000, 500, -500]).buffer);
		s.binary(new Int16Array([1000, -1000]).buffer);
		s.binary(new Int16Array([500]).buffer);
		assert.equal((h.t as any).chunksScheduled, 3);
		assert.equal((h.t as any).activeSources.length, 3);
		ctx.bufferSources[0].onended?.(); // natural completion of the first
		assert.equal((h.t as any).chunksEnded, 1);
		assert.ok((h.t as any).lastEndedAt != null);
		feed(h.t, { type: 'turn.interrupted' }); // flush cancels the remaining two
		assert.equal((h.t as any).chunksCancelled, 2);
		assert.equal((h.t as any).activeSources.length, 0);
		// The browser fires onended for stop()ped sources too — a cancelled
		// chunk must not later masquerade as a completion.
		ctx.bufferSources[1].onended?.();
		assert.equal((h.t as any).chunksEnded, 1, 'cancelled chunk never counts as ended');
		h.t.disconnect();
	});

	it('bufferedAmount watermark: skip + sendSkipped + high-water, resumes when drained', async () => {
		const h = harness();
		const s = await goLive(h);
		const p = proc(h);
		const bin = () => s.sent.filter((x) => typeof x !== 'string').length;
		const before = bin();
		s.bufferedAmount = 300 * 1024;
		p(frame(LOUD));
		assert.equal((h.t as any).sendSkipped, 1);
		assert.equal((h.t as any).bufferedHighWater, 300 * 1024);
		assert.equal(bin(), before, 'no PCM past the watermark');
		s.bufferedAmount = 0;
		p(frame(LOUD));
		assert.equal(bin(), before + 1, 'send resumes when drained');
		assert.equal((h.t as any).sendSkipped, 1);
		h.t.disconnect();
	});

	it('send try/catch: a throwing socket increments sendFailed, never throws off the graph', async () => {
		const h = harness();
		const s = await goLive(h);
		const p = proc(h);
		s.send = () => {
			throw new Error('boom');
		};
		assert.doesNotThrow(() => p(frame(LOUD)));
		assert.equal((h.t as any).sendFailed, 1);
		assert.equal((h.t as any).capCallbacks, 1, 'callback still accounted');
		assert.equal((h.t as any).bytesSent, 0, 'failed send not counted as sent');
		h.t.disconnect();
	});
});

describe('P7 D7.1 latched episodes + watchdog', () => {
	it('RMS floor latches a speech interval {onsetSeq, offsetSeq, maxRms, aboveFloorMs}', async () => {
		let now = 10_000;
		const h = harness({ nowFn: () => now });
		await goLive(h);
		const p = proc(h);
		p(frame(LOUD)); // onset at seq 1
		now += 43;
		p(frame(LOUD));
		now += 700; // past the 600 ms hangover
		p(frame(QUIET)); // offset
		const ep = ((h.t as any).episodeRing as any[]).find((sl) => sl.id === 1);
		assert.ok(ep, 'episode latched');
		assert.equal(ep.kind, 'speech');
		assert.equal(ep.onsetSeq, 1);
		assert.equal(ep.offsetSeq, 3);
		assert.equal(ep.maxRmsPm, 100); // rms 0.1 → 100‰
		assert.equal(ep.aboveFloorMs, 86); // 2 loud frames × 43 ms
		assert.equal((h.t as any).speechActive, false);
		h.t.disconnect();
	});

	it('capture stall opens a gap (capStalled + og on the wire); return latches the episode; the latch survives fast resume and re-sends idempotently', async () => {
		let now = 10_000;
		const h = harness({ nowFn: () => now });
		const s = await goLive(h);
		stopRealStats(h);
		const p = proc(h);
		p(frame(QUIET)); // lastCapAt = 10000
		now += 1100; // > max(3×43, 1000)
		tick(h); // tick 0: gap opens + first heartbeat
		assert.equal((h.t as any).capStalled, true);
		let hb = healthFrames(s);
		assert.equal(hb.length, 1);
		assert.deepEqual(hb[0].og, [0, 1100], 'open gap travels — a permanent stall never closes an episode');
		// Fast resume (S2 shape): capture returns before the next tick — the
		// interval must LATCH, not vanish with the overwritten lastCapAt.
		p(frame(QUIET));
		assert.equal((h.t as any).capStalled, false);
		const ep = ((h.t as any).episodeRing as any[]).find((sl) => sl.id === 1);
		assert.equal(ep.kind, 'gap');
		assert.equal(ep.durationMs, 1100);
		for (let i = 0; i < 4; i++) tick(h); // ticks 1-4 → heartbeat at 4
		for (let i = 0; i < 4; i++) tick(h); // ticks 5-8 → heartbeat at 8
		hb = healthFrames(s);
		assert.equal(hb.length, 3);
		assert.deepEqual(hb[1].ep.map((e: any[]) => e[0]), [1]);
		assert.deepEqual(hb[2].ep.map((e: any[]) => e[0]), [1], 'idempotent window re-sends by id');
		assert.equal(hb[1].og, undefined, 'gap closed — og gone');
		h.t.disconnect();
	});

	it('watchdog is gated: no gap while muted', async () => {
		let now = 10_000;
		const h = harness({ nowFn: () => now });
		await goLive(h);
		stopRealStats(h);
		proc(h)(frame(QUIET));
		h.t.setMicMuted(true);
		now += 5000;
		tick(h);
		assert.equal((h.t as any).capStalled, false);
		assert.equal((h.t as any).gapOpenedAt, 0);
		h.t.disconnect();
	});
});

describe('P7 D7.1 audio_health heartbeat', () => {
	it('cadence (first tick, then every 4th), stable 8-char nonce, absolute counters, seq', async () => {
		let now = 10_000;
		const h = harness({ nowFn: () => now });
		const s = await goLive(h);
		stopRealStats(h);
		const p = proc(h);
		p(frame(LOUD));
		p(frame(LOUD));
		for (let i = 0; i < 9; i++) {
			now += 500;
			tick(h);
		}
		const hb = healthFrames(s);
		assert.equal(hb.length, 3, 'heartbeats at ticks 0/4/8 — 2 s cadence on the 500 ms timer');
		assert.equal(hb[0].n.length, 8);
		assert.equal(hb[0].n, hb[2].n, 'nonce stable within the epoch');
		assert.equal(hb[0].c[0], 2, 'first beat carries the delta since epoch start');
		assert.equal(hb[1].c[0] + hb[2].c[0], 0, 'quiet beats carry zero deltas');
		assert.deepEqual([hb[0].q, hb[1].q, hb[2].q], [0, 1, 2]);
		const enc = new TextEncoder();
		for (const x of s.sent.filter((v): v is string => typeof v === 'string')) {
			assert.ok(enc.encode(x).byteLength <= 300, 'every frame ≤ 300 B (≤150 B/s at 2 s cadence)');
		}
		h.t.disconnect();
	});

	it('episode window: last 4 by id; unsent episodes aging out surface as eo', async () => {
		let now = 10_000;
		const h = harness({ nowFn: () => now, speechOffsetHangMs: 100 });
		const s = await goLive(h);
		stopRealStats(h);
		const p = proc(h);
		for (let k = 0; k < 6; k++) {
			p(frame(LOUD));
			now += 43;
			p(frame(LOUD));
			now += 200; // past the 100 ms hangover
			p(frame(QUIET));
			now += 10;
		}
		assert.equal((h.t as any).episodeSeq, 6);
		tick(h);
		const hb = healthFrames(s);
		assert.deepEqual(hb[0].ep.map((e: any[]) => e[0]), [3, 4, 5, 6], 'window = newest 4, oldest first');
		assert.equal(hb[0].eo, 2, 'episodes 1-2 can never be sent — evidence loss is visible');
		tick(h);
		tick(h);
		tick(h);
		tick(h); // next heartbeat
		const hb2 = healthFrames(s);
		assert.deepEqual(hb2[1].ep.map((e: any[]) => e[0]), [3, 4, 5, 6], 'idempotent re-send');
		assert.equal(hb2[1].eo, 2, 'overflow counted exactly once');
		h.t.disconnect();
	});

	it('worst-case serialization: hard ≤300 B by construction — window trims, flags survive', async () => {
		let now = 10_000;
		const h = harness({ nowFn: () => now, speechOffsetHangMs: 100 });
		const s = await goLive(h);
		stopRealStats(h);
		const p = proc(h);
		for (let k = 0; k < 6; k++) {
			p(frame(LOUD));
			now += 43;
			p(frame(LOUD));
			now += 200;
			p(frame(QUIET));
			now += 10;
		}
		const t: any = h.t;
		t.capCallbacks = 9_999_999;
		t.bytesSent = 9_999_999_999;
		t.sendSkipped = 99_999;
		t.sendFailed = 99_999;
		t.audioChunksRecv = 9_999_999;
		t.chunksScheduled = 9_999_999;
		t.chunksEnded = 9_999_999;
		t.chunksCancelled = 9_999_999;
		t.ctxSuspendCount = 999;
		t.bufferedHighWater = 99_999_999;
		t.lastEndedAt = 1;
		t.gapOpenedAt = 1; // ancient open gap (worst-width og values)
		s.bufferedAmount = 99_999_999;
		h.t.setMicMuted(true);
		// Model episodes from hour 3 of a call: every numeric field at its
		// realistic maximum width, so the serialized window genuinely overflows.
		for (const slot of t.episodeRing as any[]) {
			if (slot.id === 0) continue;
			slot.startMs = 9_999_999;
			slot.endMs = 9_999_999;
			slot.durationMs = 9_999_999;
			slot.onsetSeq = 9_999_999;
			slot.offsetSeq = 9_999_999;
			slot.maxRmsPm = 1000;
			slot.aboveFloorMs = 9_999_999;
		}
		tick(h);
		const raw = s.sent
			.filter((v): v is string => typeof v === 'string')
			.find((x) => {
				try {
					return JSON.parse(x)?.t === 'audio_health';
				} catch {
					return false;
				}
			})!;
		assert.ok(new TextEncoder().encode(raw).byteLength <= 300, 'hard cap holds under worst case');
		const hb = JSON.parse(raw);
		assert.ok((hb.ep?.length ?? 0) < 4, 'window trimmed to fit');
		// Core evidence survives trimming/dropping; only diagnostics (x/ba/sc)
		// may be sacrificed for the cap.
		assert.equal(hb.mu, 1);
		assert.ok(hb.og, 'open gap still travels');
		assert.equal(hb.c.length, 8, 'delta counters intact');
		assert.equal(hb.n.length, 8, 'nonce intact');
		h.t.disconnect();
	});

	it('skipped silently when the socket is not open', async () => {
		const h = harness();
		const s = await goLive(h);
		stopRealStats(h);
		s.serverClose(4000, 'bye');
		assert.doesNotThrow(() => tick(h));
		assert.equal(healthFrames(s).length, 0);
	});

	it('epoch scoping: reconnect resets counters/episodes and rotates the nonce', async () => {
		const h = harness();
		const s1 = await goLive(h);
		stopRealStats(h);
		proc(h)(frame(LOUD));
		tick(h);
		const n1 = healthFrames(s1)[0].n;
		assert.equal((h.t as any).capCallbacks, 1);
		await h.t.connect('ws://fake:9900/');
		const s2 = h.sock();
		s2.open();
		await delay(5);
		stopRealStats(h);
		assert.equal((h.t as any).capCallbacks, 0, 'counters reset per epoch');
		assert.equal((h.t as any).episodeSeq, 0, 'episodes reset per epoch');
		tick(h);
		const n2 = healthFrames(s2)[0].n;
		assert.notEqual(n1, n2, 'fresh nonce per connection epoch');
		h.t.disconnect();
	});

	it('demux: heartbeat text rides between binary PCM frames without disturbing egress', async () => {
		const h = harness();
		const s = await goLive(h);
		stopRealStats(h);
		const p = proc(h);
		p(frame(LOUD));
		tick(h);
		p(frame(LOUD));
		const kinds = s.sent.map((x) => (typeof x === 'string' ? 's' : 'b')).join('');
		assert.ok(kinds.includes('bsb'), 'text heartbeat between binary PCM frames');
		assert.equal((h.t as any).bytesSent, 2 * 682 * 2, 'PCM byte accounting unaffected');
		h.t.disconnect();
	});

	it('onStats: extended payload is additive alongside the byte totals', async () => {
		const statsSeen: any[] = [];
		const h = harness({ onStats: (st: any) => statsSeen.push(st) });
		await goLive(h);
		stopRealStats(h);
		proc(h)(frame(LOUD));
		tick(h);
		const st = statsSeen[statsSeen.length - 1];
		assert.equal(typeof st.bytesSent, 'number');
		assert.equal(typeof st.bytesRecv, 'number');
		assert.equal(st.capCallbacks, 1);
		assert.equal(st.captureState, 'observing');
		assert.equal(st.speechActive, true);
		assert.equal(st.ctxState, 'running');
		assert.equal(st.epochNonce.length, 8);
		h.t.disconnect();
	});
});

describe('P7 D7.5 capture recovery FSM', () => {
	it('suspension → resume recovery succeeds; ctxSuspendCount + transitions recorded', async () => {
		const events: Array<{ state: string; kind: string }> = [];
		const h = harness({
			recoveryBackoffMs: [0, 0, 0],
			recoveryResumeTimeoutMs: 100,
			onCaptureHealth: (state: any, kind: any) => events.push({ state, kind }),
		});
		await goLive(h);
		const ctx = FakeAudioContext.created[0];
		ctx.state = 'suspended';
		ctx.fireStateChange();
		await delay(10);
		assert.deepEqual(events, [
			{ state: 'recovering', kind: 'resume' },
			{ state: 'recovered', kind: 'resume' },
		]);
		assert.equal((h.t as any).captureState, 'observing');
		assert.equal((h.t as any).ctxSuspendCount, 1);
		assert.equal(ctx.state, 'running');
		h.t.disconnect();
	});

	it('exhausted recovery → degraded, NOT terminal; single-flight; retryCapture recovers', async () => {
		const events: Array<{ s: string; k: string }> = [];
		const h = harness({
			recoveryBackoffMs: [0, 0, 0],
			recoveryResumeTimeoutMs: 30,
			onCaptureHealth: (s: any, k: any) => events.push({ s, k }),
		});
		const s = await goLive(h);
		const ctx = FakeAudioContext.created[0];
		let resumeCalls = 0;
		ctx.resume = async () => {
			resumeCalls++; // resolves but stays suspended — resume ineffective
		};
		ctx.state = 'suspended';
		ctx.fireStateChange();
		ctx.fireStateChange(); // second signal mid-recovery: no second loop
		await delay(30);
		assert.equal((h.t as any).captureState, 'degraded');
		assert.equal(resumeCalls, 3, 'bounded attempts, single-flight');
		assert.equal(events.filter((e) => e.s === 'recovering').length, 1, 'one recovering emission');
		assert.equal(events[events.length - 1].s, 'degraded');
		assert.equal(h.failures.length, 0, 'degraded is NOT onConnectFailure');
		assert.equal(s.closeCalls, 0, 'socket stays live — voice lease retained');
		assert.ok(
			h.statuses.every((x) => x.status !== 'error' && x.status !== 'closed'),
			'no terminal status',
		);
		// The UI retry affordance: fix the ctx, reacquire from degraded.
		ctx.resume = async () => {
			ctx.state = 'running';
		};
		let gumCalls = 0;
		gumImpl = async () => {
			gumCalls++;
			return new FakeMediaStream();
		};
		h.t.retryCapture();
		await delay(10);
		assert.equal(gumCalls, 1, 'retry reacquires');
		assert.equal((h.t as any).captureState, 'observing');
		assert.equal(events[events.length - 1].s, 'recovered');
		h.t.disconnect();
	});

	it('devicechange filter: output-only change ignored; input-set change reacquires', async () => {
		let devices = [
			{ kind: 'audioinput', deviceId: 'a' },
			{ kind: 'audiooutput', deviceId: 'x' },
		];
		enumImpl = async () => devices;
		let gumCalls = 0;
		gumImpl = async () => {
			gumCalls++;
			return new FakeMediaStream();
		};
		const h = harness({ recoveryBackoffMs: [0, 0, 0], recoveryResumeTimeoutMs: 50 });
		await goLive(h);
		await delay(5); // initial input-device snapshot settles
		assert.equal(gumCalls, 1);
		assert.equal((h.t as any).inputDeviceSig, 'a');
		devices = [
			{ kind: 'audioinput', deviceId: 'a' },
			{ kind: 'audiooutput', deviceId: 'y' },
		];
		fireDeviceChange();
		await delay(10);
		assert.equal(gumCalls, 1, 'output-only change: no reacquire');
		devices = [
			{ kind: 'audioinput', deviceId: 'b' },
			{ kind: 'audiooutput', deviceId: 'y' },
		];
		fireDeviceChange();
		await delay(10);
		assert.equal(gumCalls, 2, 'input-set change: reacquire');
		assert.equal((h.t as any).inputDeviceSig, 'b');
		h.t.disconnect();
	});

	it('devicechange before the initial input snapshot settles does not reacquire blindly', async () => {
		const resolvers: Array<(v: Array<{ kind: string; deviceId: string }>) => void> = [];
		enumImpl = () => new Promise((resolve) => resolvers.push(resolve));
		let gumCalls = 0;
		gumImpl = async () => {
			gumCalls++;
			return new FakeMediaStream();
		};
		const h = harness({ recoveryBackoffMs: [0, 0, 0], recoveryResumeTimeoutMs: 50 });
		await goLive(h);
		assert.equal(resolvers.length, 1, 'initial snapshot is pending');
		fireDeviceChange();
		await delay(2);
		assert.equal(resolvers.length, 2, 'change snapshot is pending');
		resolvers[1]([{ kind: 'audioinput', deviceId: 'a' }]);
		await delay(5);
		assert.equal(gumCalls, 1, 'no baseline means no blind reacquire');
		resolvers[0]([{ kind: 'audioinput', deviceId: 'a' }]);
		await delay(5);
		assert.equal((h.t as any).inputDeviceSig, 'a', 'initial snapshot still establishes the baseline');
		h.t.disconnect();
	});

	it('track ended → reacquire replaces the capture stream', async () => {
		const streams: FakeMediaStream[] = [];
		gumImpl = async () => {
			const st = new FakeMediaStream();
			streams.push(st);
			return st;
		};
		const h = harness({ recoveryBackoffMs: [0, 0, 0], recoveryResumeTimeoutMs: 50 });
		await goLive(h);
		(streams[0].tracks[0] as any).onended?.();
		await delay(10);
		assert.equal(streams.length, 2, 'reacquired');
		assert.equal((h.t as any).micStream, streams[1]);
		assert.equal(streams[0].tracks[0].stopped, true, 'old track stopped');
		h.t.disconnect();
	});

	it('fencing: disconnect during a parked reacquire — the late grant leaks nothing', async () => {
		const streams: FakeMediaStream[] = [];
		gumImpl = async () => {
			const st = new FakeMediaStream();
			streams.push(st);
			return st;
		};
		const h = harness({ recoveryBackoffMs: [0, 0, 0], recoveryResumeTimeoutMs: 5000 });
		await goLive(h);
		let releaseGum!: (s: FakeMediaStream) => void;
		gumImpl = () =>
			new Promise<FakeMediaStream>((res) => {
				releaseGum = (st) => {
					streams.push(st);
					res(st);
				};
			});
		(streams[0].tracks[0] as any).onended?.(); // recovery parks in getUserMedia
		await delay(5);
		assert.equal((h.t as any).captureState, 'recovering');
		await h.t.disconnect(); // teardown bumps captureGen
		const late = new FakeMediaStream();
		releaseGum(late);
		await delay(5);
		assert.equal(late.tracks[0].stopped, true, 'late grant stopped, not leaked');
		assert.equal((h.t as any).micStream, null);
		assert.equal((h.t as any).processor, null);
	});

	it('listener teardown: disconnect removes devicechange + statechange listeners', async () => {
		const h = harness();
		await goLive(h);
		assert.equal(mediaDevicesListeners.length, 1, 'devicechange listener installed');
		const ctx = FakeAudioContext.created[0];
		assert.ok(ctx.onstatechange, 'statechange adopted');
		await h.t.disconnect();
		assert.equal(mediaDevicesListeners.length, 0, 'devicechange removed');
		assert.equal(ctx.onstatechange, null, 'statechange cleared before close');
	});
});

describe('P7 §D7.0b frame-path budget', () => {
	it('instrumented frame handler adds ≤50 µs p99 over the pinned-replica baseline', async () => {
		const h = harness();
		await goLive(h);
		stopRealStats(h);
		const p = proc(h);
		const s = h.sock();
		const buf = new Float32Array(2048);
		for (let i = 0; i < buf.length; i++) buf[i] = Math.sin(i / 10) * 0.05;
		const ev = frame(buf);
		// Pinned-replica baseline: the pre-P7 handler body with a same-shape sink.
		const baseSink: ArrayBuffer[] = [];
		let baseBytes = 0;
		const baseline = (e: FrameEvent): void => {
			const raw = e.inputBuffer.getChannelData(0);
			const down = downsample(raw, 48000, 16000);
			const pcm = float32ToInt16(down);
			baseSink.push(pcm.buffer as ArrayBuffer);
			baseBytes += pcm.buffer.byteLength;
		};
		const N = 2000;
		const WARM = 300;
		const tInst = new Float64Array(N);
		const tBase = new Float64Array(N);
		for (let i = 0; i < WARM; i++) {
			p(ev);
			baseline(ev);
		}
		s.sent.length = 0;
		baseSink.length = 0;
		for (let i = 0; i < N; i++) {
			let t0 = process.hrtime.bigint();
			baseline(ev);
			let t1 = process.hrtime.bigint();
			tBase[i] = Number(t1 - t0);
			t0 = process.hrtime.bigint();
			p(ev);
			t1 = process.hrtime.bigint();
			tInst[i] = Number(t1 - t0);
			if (i % 250 === 0) {
				s.sent.length = 0; // keep sink growth from skewing either side
				baseSink.length = 0;
			}
		}
		const p99 = (a: Float64Array): number => {
			const c = Array.from(a).sort((x, y) => x - y);
			return c[Math.floor(c.length * 0.99)];
		};
		const addedUs = (p99(tInst) - p99(tBase)) / 1000;
		assert.ok(addedUs <= 50, `added p99 ${addedUs.toFixed(1)} µs exceeds the 50 µs budget`);
		assert.ok(baseBytes > 0);
		h.t.disconnect();
	});

	it('PCM displacement: heartbeat bytes are <1% of egress while frames flow', async () => {
		let now = 10_000;
		const h = harness({ nowFn: () => now });
		const s = await goLive(h);
		stopRealStats(h);
		const p = proc(h);
		for (let i = 0; i < 47; i++) {
			now += 43;
			p(frame(LOUD));
		}
		tick(h); // one heartbeat within the ~2 s of PCM
		let stringBytes = 0;
		let binBytes = 0;
		for (const x of s.sent) {
			if (typeof x === 'string') stringBytes += new TextEncoder().encode(x).byteLength;
			else binBytes += (x as ArrayBuffer).byteLength;
		}
		assert.ok(binBytes > 0);
		assert.ok(
			stringBytes / (stringBytes + binBytes) < 0.01,
			`displacement ${((stringBytes / (stringBytes + binBytes)) * 100).toFixed(2)}% ≥ 1%`,
		);
		h.t.disconnect();
	});

	it('zero added frame-path allocation (GC A/B soak — needs --expose-gc, else skips)', async (t) => {
		const gc = (globalThis as { gc?: () => void }).gc;
		if (typeof gc !== 'function') {
			t.skip('run with node --expose-gc to enable');
			return;
		}
		const h = harness();
		await goLive(h);
		stopRealStats(h);
		const p = proc(h);
		const s = h.sock();
		const buf = new Float32Array(2048).fill(0.05);
		const ev = frame(buf);
		const baseSink: ArrayBuffer[] = [];
		const baseline = (e: FrameEvent): void => {
			const raw = e.inputBuffer.getChannelData(0);
			baseSink.push(float32ToInt16(downsample(raw, 48000, 16000)).buffer as ArrayBuffer);
		};
		const measure = (fn: (e: FrameEvent) => void, sink: () => void): number => {
			for (let i = 0; i < 500; i++) fn(ev); // warm
			sink();
			gc();
			const before = process.memoryUsage().heapUsed;
			for (let i = 0; i < 3000; i++) {
				fn(ev);
				if (i % 250 === 0) sink();
			}
			sink();
			gc();
			return process.memoryUsage().heapUsed - before;
		};
		const baseDelta = measure(baseline, () => {
			baseSink.length = 0;
		});
		const instDelta = measure(p, () => {
			s.sent.length = 0;
		});
		assert.ok(
			instDelta <= baseDelta + 512 * 1024,
			`instrumented retained ${instDelta - baseDelta} B more than baseline`,
		);
		h.t.disconnect();
	});
});

describe('P7 codex round-1 fixes', () => {
	it('gap that starts AND ends between watchdog ticks still latches (frozen-timer self-latch)', async () => {
		let now = 10_000;
		const h = harness({ nowFn: () => now });
		await goLive(h);
		stopRealStats(h);
		const p = proc(h);
		p(frame(QUIET)); // lastCapAt = 10000
		now += 1200; // whole-thread freeze: no tick ever observed it
		p(frame(QUIET)); // resumed callback must latch before overwriting
		const ep = ((h.t as any).episodeRing as any[]).find((sl) => sl.id === 1);
		assert.ok(ep, 'episode latched without any watchdog tick');
		assert.equal(ep.kind, 'gap');
		assert.equal(ep.durationMs, 1200);
		assert.equal((h.t as any).episodeSeq, 1);
		h.t.disconnect();
	});

	it('capture that never fires its first callback still produces a stall (wire-time baseline)', async () => {
		let now = 10_000;
		const h = harness({ nowFn: () => now });
		const s = await goLive(h);
		stopRealStats(h);
		// no frames at all
		now += 1500;
		tick(h);
		assert.equal((h.t as any).capStalled, true, 'baseline armed at graph wire');
		const hb = healthFrames(s);
		assert.ok(hb[0].og, 'open gap travels');
		h.t.disconnect();
	});

	it('watchdog stays armed during a reacquire outage (processor torn down)', async () => {
		let now = 10_000;
		const streams: FakeMediaStream[] = [];
		gumImpl = async () => {
			const st = new FakeMediaStream();
			streams.push(st);
			return st;
		};
		const h = harness({ nowFn: () => now, recoveryBackoffMs: [0, 0, 0], recoveryResumeTimeoutMs: 5000 });
		await goLive(h);
		stopRealStats(h);
		proc(h)(frame(QUIET));
		gumImpl = () => new Promise<FakeMediaStream>(() => {}); // park the reacquire
		(streams[0].tracks[0] as any).onended?.();
		await delay(5);
		assert.equal((h.t as any).captureState, 'recovering');
		assert.equal((h.t as any).processor, null, 'precondition: capture torn down');
		now += 2000;
		tick(h);
		assert.equal((h.t as any).capStalled, true, 'reacquire outage is a gap');
		h.t.disconnect();
	});

	it('reacquire requested during an in-flight resume is honored after the resume succeeds', async () => {
		const events: Array<{ s: string; k: string }> = [];
		const streams: FakeMediaStream[] = [];
		let gumCalls = 0;
		gumImpl = async () => {
			gumCalls++;
			const st = new FakeMediaStream();
			streams.push(st);
			return st;
		};
		const h = harness({
			recoveryBackoffMs: [0, 0, 0],
			recoveryResumeTimeoutMs: 500,
			onCaptureHealth: (s: any, k: any) => events.push({ s, k }),
		});
		await goLive(h);
		const ctx = FakeAudioContext.created[0];
		let releaseResume!: () => void;
		ctx.resume = () =>
			new Promise<void>((res) => {
				releaseResume = () => {
					ctx.state = 'running';
					res();
				};
			});
		ctx.state = 'suspended';
		ctx.fireStateChange(); // resume-recovery parks inside resume()
		await delay(5);
		(streams[0].tracks[0] as any).onended?.(); // dead track during the resume
		releaseResume(); // resume SUCCEEDS — but the track is still dead
		await delay(10);
		assert.equal(gumCalls, 2, 'escalated reacquire ran after the successful resume');
		assert.equal((h.t as any).micStream, streams[1], 'fresh stream wired');
		const last = events[events.length - 1];
		assert.deepEqual(last, { s: 'recovered', k: 'reacquire' });
		h.t.disconnect();
	});

	it('reacquire with the context stuck suspended reports failure, not recovery', async () => {
		const events: Array<{ s: string; k: string }> = [];
		const streams: FakeMediaStream[] = [];
		let gumCalls = 0;
		gumImpl = async () => {
			gumCalls++;
			const st = new FakeMediaStream();
			streams.push(st);
			return st;
		};
		const h = harness({
			recoveryBackoffMs: [0, 0, 0],
			recoveryResumeTimeoutMs: 30,
			onCaptureHealth: (s: any, k: any) => events.push({ s, k }),
		});
		await goLive(h);
		const ctx = FakeAudioContext.created[0];
		ctx.resume = async () => {
			/* resolves but the context never runs again */
		};
		ctx.state = 'suspended';
		(streams[0].tracks[0] as any).onended?.();
		await delay(30);
		assert.equal((h.t as any).captureState, 'degraded', 'suspended ctx is NOT a recovery');
		assert.equal(gumCalls, 4, 'initial + 3 bounded attempts');
		for (const st of streams.slice(1)) {
			assert.equal(st.tracks[0].stopped, true, 'unused acquisition stopped');
		}
		assert.equal(events[events.length - 1].s, 'degraded');
		h.t.disconnect();
	});

	it('a stale attempt\'s in-flight recovery cannot swallow the new attempt\'s trigger', async () => {
		const events: Array<{ s: string; k: string }> = [];
		const streams: FakeMediaStream[] = [];
		gumImpl = async () => {
			const st = new FakeMediaStream();
			streams.push(st);
			return st;
		};
		const h = harness({
			recoveryBackoffMs: [0, 0, 0],
			recoveryResumeTimeoutMs: 5000,
			onCaptureHealth: (s: any, k: any) => events.push({ s, k }),
		});
		await goLive(h);
		// Park attempt 1's recovery inside getUserMedia.
		gumImpl = () => new Promise<FakeMediaStream>(() => {});
		(streams[0].tracks[0] as any).onended?.();
		await delay(5);
		assert.equal(events.filter((e) => e.s === 'recovering').length, 1);
		// New attempt over the same transport.
		gumImpl = async () => {
			const st = new FakeMediaStream();
			streams.push(st);
			return st;
		};
		await h.t.connect('ws://fake:9900/');
		h.sock().open();
		await delay(5);
		const ctx = FakeAudioContext.created[0]; // reused (never closed)
		ctx.resume = async () => {
			ctx.state = 'running';
		};
		ctx.state = 'suspended';
		ctx.fireStateChange();
		await delay(10);
		assert.equal(
			events.filter((e) => e.s === 'recovering').length,
			2,
			'the live attempt took recovery ownership from the fenced-dead loop',
		);
		assert.equal(events[events.length - 1].s, 'recovered');
		h.t.disconnect();
	});

	it('a stale device enumeration cannot reacquire over a fresh recovery (captureGen fence)', async () => {
		const streams: FakeMediaStream[] = [];
		let gumCalls = 0;
		gumImpl = async () => {
			gumCalls++;
			const st = new FakeMediaStream();
			streams.push(st);
			return st;
		};
		const enumResolvers: Array<(v: Array<{ kind: string; deviceId: string }>) => void> = [];
		enumImpl = () => new Promise((res) => enumResolvers.push(res));
		const h = harness({ recoveryBackoffMs: [0, 0, 0], recoveryResumeTimeoutMs: 100 });
		await goLive(h);
		// r0: wireCaptureGraph's initial snapshot — settle it as 'a'.
		enumResolvers.shift()?.([{ kind: 'audioinput', deviceId: 'a' }]);
		await delay(5);
		fireDeviceChange(); // r1 parks (captures captureGen)
		await delay(2);
		(streams[0].tracks[0] as any).onended?.(); // recovery replaces the graph (bumps captureGen)
		await delay(10);
		assert.equal(gumCalls, 2, 'precondition: track-ended reacquired');
		// r2: the NEW graph's snapshot; r1 is still the parked devicechange.
		const afterRecovery = gumCalls;
		enumResolvers.pop()?.([{ kind: 'audioinput', deviceId: 'b' }]); // settle r2 (fresh graph)
		enumResolvers.shift()?.([{ kind: 'audioinput', deviceId: 'zzz' }]); // stale r1: changed set!
		await delay(10);
		assert.equal(gumCalls, afterRecovery, 'stale enumeration fenced — no spurious reacquire');
		h.t.disconnect();
	});

	it('delta heartbeats: a failed send folds its interval into the next beat (lossless)', async () => {
		const now = 10_000;
		const h = harness({ nowFn: () => now });
		const s = await goLive(h);
		stopRealStats(h);
		const p = proc(h);
		p(frame(LOUD));
		p(frame(LOUD));
		tick(h); // tick 0 → hb0: delta 2
		for (let i = 0; i < 3; i++) p(frame(LOUD));
		let failNext = true;
		const origSend = s.send.bind(s);
		s.send = (d: ArrayBuffer | string) => {
			if (typeof d === 'string' && failNext) {
				failNext = false;
				throw new Error('socket hiccup');
			}
			origSend(d);
		};
		tick(h);
		tick(h);
		tick(h);
		tick(h); // tick 4 → hb send FAILS (baseline not advanced)
		p(frame(LOUD));
		tick(h);
		tick(h);
		tick(h);
		tick(h); // tick 8 → next successful hb
		const hb = healthFrames(s);
		assert.equal(hb.length, 2, 'failed beat never landed');
		assert.equal(hb[0].c[0], 2);
		assert.equal(hb[1].c[0], 4, 'lost interval (3) + new (1) folded into one delta');
		assert.deepEqual([hb[0].q, hb[1].q], [0, 1], 'q counts SENT beats — contiguous');
		h.t.disconnect();
	});

	it('lifecycle evidence rides onStats: ctx transition, device + recovery events, drop counters', async () => {
		const statsSeen: any[] = [];
		const streams: FakeMediaStream[] = [];
		gumImpl = async () => {
			const st = new FakeMediaStream();
			streams.push(st);
			return st;
		};
		const h = harness({
			recoveryBackoffMs: [0, 0, 0],
			recoveryResumeTimeoutMs: 100,
			onStats: (st: any) => statsSeen.push(st),
		});
		await goLive(h);
		stopRealStats(h);
		const ctx = FakeAudioContext.created[0];
		ctx.state = 'suspended';
		ctx.fireStateChange(); // resume recovery (fake resume → running)
		await delay(10);
		(streams[0].tracks[0] as any).onended?.(); // reacquire
		await delay(10);
		tick(h);
		const st = statsSeen[statsSeen.length - 1];
		assert.equal(st.ctxLastTransition?.to, 'suspended');
		assert.ok(
			st.deviceEvents.some((e: any) => e.kind === 'track-ended'),
			'device events exposed',
		);
		assert.ok(
			st.recoveryEvents.some((e: any) => e.result === 'recovered'),
			'recovery events exposed',
		);
		assert.equal(st.deviceEventsDropped, 0);
		assert.equal(st.recoveryEventsDropped, 0);
		h.t.disconnect();
	});
});
