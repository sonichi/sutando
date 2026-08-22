/**
 * Coordinator tests (Phase 1 armed mode): the impure driver around the pure
 * reducer, driven end-to-end against a fake bodhi session — the integration
 * fake from the design's test plan (a transport that activates, fails, or
 * goes silent exactly as scripted) — with a virtual clock and manual timers.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import {
	VoiceSilenceRecoveryCoordinator,
	parseRetryUpstreamCommand,
	recoverySurfaceSupported,
} from '../src/voice-silence-recovery-coordinator.js';
import type { MatrixFactsLike } from '../src/voice-active-silence-watchdog.js';

interface FakeTimer {
	fn: () => void;
	at: number;
	cleared: boolean;
	fired: boolean;
}

function makeHarness(opts: { requiredTicks?: number } = {}) {
	let now = 100_000;
	const timers: FakeTimer[] = [];
	const sent: Array<Record<string, unknown>> = [];
	const rows: Array<Record<string, unknown>> = [];
	const restarts: Array<{
		reason: string;
		dialGen: number;
		resolve: () => void;
		reject: (e: unknown) => void;
	}> = [];
	let dialGen = 1; // the initial session dial took generation 1
	const session = {
		recoverUpstream(args: { reason: string }) {
			dialGen += 1;
			let resolveA!: () => void;
			let rejectA!: (e: unknown) => void;
			const activated = new Promise<void>((res, rej) => {
				resolveA = res;
				rejectA = rej;
			});
			activated.catch(() => {});
			restarts.push({ reason: args.reason, dialGen, resolve: resolveA, reject: rejectA });
			return {
				attemptEpoch: dialGen,
				activated,
				incumbentClosed: Promise.resolve('closed' as const),
			};
		},
		sendJsonToClient(m: Record<string, unknown>) {
			sent.push(m);
		},
		getRecoveryCapabilities() {
			return {
				version: 1,
				recoverUpstream: true,
				reconnectBoundary: true,
				turnStartPublication: true,
				transportGenerations: true,
				syntheticHold: true,
			};
		},
	};
	const coord = new VoiceSilenceRecoveryCoordinator({
		voiceSessionId: 'vs_test',
		session,
		requiredTicks: opts.requiredTicks ?? 3,
		nowFn: () => now,
		wallNowFn: () => now,
		record: (row) => rows.push(row),
		setTimer: (fn, ms) => {
			const t: FakeTimer = { fn, at: now + ms, cleared: false, fired: false };
			timers.push(t);
			return t;
		},
		clearTimer: (h) => {
			(h as FakeTimer).cleared = true;
		},
	});
	const advance = (ms: number) => {
		now += ms;
		for (const t of timers) {
			if (!t.cleared && !t.fired && t.at <= now) {
				t.fired = true;
				t.fn();
			}
		}
	};
	const facts = (over: Partial<MatrixFactsLike> = {}): MatrixFactsLike => ({
		factsAvailable: true,
		speechInWindow: true,
		speechObservedAt: null,
		ingressAdvanced: true,
		modelSilentFor15s: true,
		...over,
	});
	const tick = (over: {
		lastAboveFloorAt?: number | null;
		pendingToolCount?: number;
		delivered?: { epoch: number | null; chunksEnded: number; egressFrames: number; heartbeatSeen: boolean };
		sessionState?: string;
	} = {}) => {
		coord.observeTick({
			at: now,
			sessionState: over.sessionState ?? 'ACTIVE',
			facts: facts(),
			lastAboveFloorAt: over.lastAboveFloorAt ?? null,
			pendingToolCount: over.pendingToolCount ?? 0,
			delivered:
				over.delivered ?? { epoch: null, chunksEnded: 0, egressFrames: 0, heartbeatSeen: false },
		});
	};
	const setupOk = (dial: number, gen: number) =>
		coord.handleLifecycleEvent({ kind: 'setup-ok', connectAttemptId: `att_${dial}`, transportGeneration: gen });
	const flush = () => new Promise<void>((r) => setImmediate(r));
	return { coord, session, advance, tick, setupOk, flush, sent, rows, restarts, timers, nowOf: () => now };
}

/** Attach a client, activate generation 1 ordinarily, latch speech, and walk
 *  three qualifying ticks to the first authorized restart. */
async function fireOnce(h: ReturnType<typeof makeHarness>) {
	h.coord.handleClientConnected();
	h.setupOk(1, 1);
	h.advance(10_000);
	const speechAt = h.nowOf();
	h.advance(20_000);
	h.tick({ lastAboveFloorAt: speechAt }); // speech + streak 1
	h.advance(30_000);
	h.tick({ lastAboveFloorAt: speechAt }); // streak 2
	h.advance(30_000);
	h.tick({ lastAboveFloorAt: speechAt }); // streak 3 -> authorize
}

describe('recovery coordinator: fire and restart', () => {
	it('three qualifying ticks authorize one injection-free held restart', async () => {
		const h = makeHarness();
		await fireOnce(h);
		assert.equal(h.restarts.length, 1);
		assert.equal(h.restarts[0].reason, 'active-silence');
		assert.equal(h.coord.phase, 'restarting');
		// Correlated activation returns to idle on the recovery dial's setup-ok.
		h.setupOk(h.restarts[0].dialGen, 2);
		assert.equal(h.coord.phase, 'idle');
		assert.equal(h.coord.state.currentTransportEpoch, 2);
		assert.equal(h.coord.state.episodeAttempts, 1);
	});

	it('a non-ACTIVE state or pending tool never fires', async () => {
		const h = makeHarness();
		h.coord.handleClientConnected();
		h.setupOk(1, 1);
		h.advance(10_000);
		const speechAt = h.nowOf();
		h.advance(20_000);
		h.coord.noteToolCall('t1', 'inline');
		for (let i = 0; i < 5; i += 1) {
			h.tick({ lastAboveFloorAt: speechAt });
			h.advance(30_000);
		}
		assert.equal(h.restarts.length, 0);
		h.coord.noteToolSettled('t1'); // advances the anchor via toolOutcome
		for (let i = 0; i < 5; i += 1) {
			h.tick({ lastAboveFloorAt: speechAt, sessionState: 'CONNECTING' });
			h.advance(30_000);
		}
		assert.equal(h.restarts.length, 0);
	});

	it('background tool calls never veto', async () => {
		const h = makeHarness();
		h.coord.noteToolCall('bg1', 'background');
		assert.equal(h.coord.pendingToolCount, 0);
	});
});

describe('recovery coordinator: retry ladder and terminal', () => {
	it('dial failure schedules a cooldown retry that re-authorizes', async () => {
		const h = makeHarness();
		await fireOnce(h);
		h.restarts[0].reject(new Error('dial failed'));
		await h.flush();
		assert.equal(h.coord.phase, 'waiting-retry');
		// The retry timer targets lastActionAt + 60s cooldown.
		h.advance(60_001);
		assert.equal(h.restarts.length, 2);
		assert.equal(h.coord.state.episodeAttempts, 2);
	});

	it('three failed attempts latch terminal and push voice-stalled', async () => {
		const h = makeHarness();
		await fireOnce(h);
		for (let attempt = 0; attempt < 3; attempt += 1) {
			h.restarts[h.restarts.length - 1].reject(new Error('dial failed'));
			await h.flush();
			if (attempt < 2) h.advance(60_001);
		}
		assert.equal(h.restarts.length, 3);
		assert.equal(h.coord.phase, 'terminal');
		assert.equal(h.coord.isTerminal, true);
		const stalled = h.sent.filter((m) => m.type === 'voice-stalled');
		assert.equal(stalled.length, 1);
		assert.equal(stalled[0].voiceSessionId, 'vs_test');
		assert.equal(stalled[0].clientEpoch, 1);
		assert.equal(stalled[0].stalledAttemptEpoch, 3);
		assert.equal(stalled[0].episodeAttempts, 3);
		assert.equal(stalled[0].reason, 'active-silence-attempts-exhausted');
	});

	it('terminal state is re-sent on client reattach with a fresh epoch', async () => {
		const h = makeHarness();
		await fireOnce(h);
		for (let attempt = 0; attempt < 3; attempt += 1) {
			h.restarts[h.restarts.length - 1].reject(new Error('dial failed'));
			await h.flush();
			if (attempt < 2) h.advance(60_001);
		}
		h.coord.handleClientDisconnected();
		h.coord.handleClientConnected();
		const stalled = h.sent.filter((m) => m.type === 'voice-stalled');
		assert.equal(stalled.length, 2);
		assert.equal(stalled[1].clientEpoch, 2);
		// enteredAtUnixMs is the LATCH time, not the resend time.
		assert.equal(stalled[1].enteredAtUnixMs, stalled[0].enteredAtUnixMs);
	});

	it('a current-generation close after a recovered episode re-enters the ladder', async () => {
		const h = makeHarness();
		await fireOnce(h);
		h.setupOk(h.restarts[0].dialGen, 2); // recovery succeeded -> idle
		h.coord.handleLifecycleEvent({ kind: 'generation-close', transportGeneration: 2 });
		assert.equal(h.coord.phase, 'waiting-retry');
	});
});

async function driveToTerminal(h: ReturnType<typeof makeHarness>) {
	await fireOnce(h);
	for (let attempt = 0; attempt < 3; attempt += 1) {
		h.restarts[h.restarts.length - 1].reject(new Error('dial failed'));
		await h.flush();
		if (attempt < 2) h.advance(60_001);
	}
	assert.equal(h.coord.phase, 'terminal');
}

const retryCmd = (over: Record<string, unknown> = {}): Record<string, unknown> => ({
	type: 'voice.retryUpstream',
	version: 1,
	voiceSessionId: 'vs_test',
	clientEpoch: 1,
	stalledAttemptEpoch: 3,
	requestId: 'req-1',
	...over,
});

describe('recovery coordinator: human retry wire', () => {
	it('a matching retry restarts a fresh episode and acks accepted with the new epoch', async () => {
		const h = makeHarness();
		await driveToTerminal(h);
		h.advance(120_000);
		h.coord.handleClientCommand(retryCmd());
		assert.equal(h.restarts.length, 4);
		assert.equal(h.restarts[3].reason, 'human-retry');
		const ack = h.sent.filter((m) => m.type === 'voice.retryUpstream.ack');
		assert.equal(ack.length, 1);
		assert.equal(ack[0].disposition, 'accepted');
		assert.equal(ack[0].acceptedAttemptEpoch, 4);
		assert.equal(ack[0].requestId, 'req-1');
		assert.equal(h.coord.state.episodeAttempts, 1); // fresh episode
	});

	it('a duplicate requestId returns the original ack and never redials', async () => {
		const h = makeHarness();
		await driveToTerminal(h);
		h.advance(120_000);
		h.coord.handleClientCommand(retryCmd());
		const restartsAfterFirst = h.restarts.length;
		h.coord.handleClientCommand(retryCmd());
		assert.equal(h.restarts.length, restartsAfterFirst);
		const acks = h.sent.filter((m) => m.type === 'voice.retryUpstream.ack');
		assert.equal(acks.length, 2);
		assert.deepEqual(acks[1], acks[0]);
	});

	it('a stale attempt epoch acks stale and changes nothing', async () => {
		const h = makeHarness();
		await driveToTerminal(h);
		h.advance(120_000);
		h.coord.handleClientCommand(retryCmd({ stalledAttemptEpoch: 2, requestId: 'req-old' }));
		const ack = h.sent.filter((m) => m.type === 'voice.retryUpstream.ack');
		assert.equal(ack.length, 1);
		assert.equal(ack[0].disposition, 'stale');
		assert.equal(ack[0].acceptedAttemptEpoch, null);
		assert.equal(h.restarts.length, 3);
		assert.equal(h.coord.phase, 'terminal');
	});

	it('a retry outside terminal acks not-terminal', async () => {
		const h = makeHarness();
		h.coord.handleClientConnected();
		h.setupOk(1, 1);
		h.coord.handleClientCommand(retryCmd({ stalledAttemptEpoch: 1 }));
		const ack = h.sent.filter((m) => m.type === 'voice.retryUpstream.ack');
		assert.equal(ack.length, 1);
		assert.equal(ack[0].disposition, 'not-terminal');
		assert.equal(ack[0].acceptedAttemptEpoch, null);
		assert.equal(h.restarts.length, 0);
	});

	it('a schema-invalid retry is dropped without an ack', async () => {
		const h = makeHarness();
		await driveToTerminal(h);
		h.coord.handleClientCommand(retryCmd({ extra: true }));
		assert.equal(h.sent.filter((m) => m.type === 'voice.retryUpstream.ack').length, 0);
	});

	it('retry during fatal backoff parks silently, then dials as human-retry with the minted epoch', async () => {
		const h = makeHarness();
		await driveToTerminal(h);
		h.advance(120_000);
		h.coord.handleFatalBackoff(h.nowOf() + 300_000);
		h.coord.handleClientCommand(retryCmd());
		// Parked: NO ack yet — an accepted naming a stale epoch would hide the
		// client banner against the wrong attempt.
		assert.equal(h.sent.filter((m) => m.type === 'voice.retryUpstream.ack').length, 0);
		assert.equal(h.restarts.length, 3); // no dial yet
		assert.equal(h.coord.phase, 'waiting-retry');
		// A duplicate of the parked request stays silent too (its ack is pending).
		h.coord.handleClientCommand(retryCmd());
		assert.equal(h.sent.filter((m) => m.type === 'voice.retryUpstream.ack').length, 0);
		h.advance(300_001);
		assert.equal(h.restarts.length, 4); // dialed once the backoff cleared
		assert.equal(h.restarts[3].reason, 'human-retry'); // provenance preserved
		const ack = h.sent.filter((m) => m.type === 'voice.retryUpstream.ack');
		assert.equal(ack.length, 1);
		assert.equal(ack[0].disposition, 'accepted');
		// The acknowledged epoch IS the authorization's minted epoch.
		assert.equal(ack[0].acceptedAttemptEpoch, h.coord.state.attemptEpoch);
		assert.equal(ack[0].requestId, 'req-1');
	});
});

describe('recovery coordinator: terminal clears', () => {
	it('a current-epoch user-visible response clears terminal (functional recovery)', async () => {
		const h = makeHarness();
		await driveToTerminal(h);
		h.tick({ delivered: { epoch: 7, chunksEnded: 3, egressFrames: 3, heartbeatSeen: true } });
		assert.equal(h.coord.phase, 'idle');
		assert.equal(h.coord.isTerminal, false);
		assert.equal(h.coord.state.episodeAttempts, 0);
	});

	it('modelEvent, detach, and reattach do NOT clear terminal', async () => {
		const h = makeHarness();
		await driveToTerminal(h);
		h.coord.handleClientDisconnected();
		h.coord.handleClientConnected();
		h.tick({}); // ordinary tick, nothing delivered
		assert.equal(h.coord.phase, 'terminal');
	});
});

describe('capability validation and schema', () => {
	it('recoverySurfaceSupported requires version 1 and every normative capability', () => {
		const caps = {
			version: 1,
			recoverUpstream: true,
			reconnectBoundary: true,
			turnStartPublication: true,
			transportGenerations: true,
			syntheticHold: true,
		};
		const full = {
			recoverUpstream() {},
			sendJsonToClient() {},
			getRecoveryCapabilities: () => ({ ...caps }),
		};
		assert.equal(recoverySurfaceSupported(full), true);
		assert.equal(recoverySurfaceSupported({ ...full, recoverUpstream: undefined }), false);
		assert.equal(recoverySurfaceSupported({ ...full, sendJsonToClient: undefined }), false);
		for (const field of [
			'recoverUpstream',
			'reconnectBoundary',
			'turnStartPublication',
			'transportGenerations',
			'syntheticHold',
		] as const) {
			assert.equal(
				recoverySurfaceSupported({
					...full,
					getRecoveryCapabilities: () => ({ ...caps, [field]: false }),
				}),
				false,
				`descriptor with ${field}=false must not arm`,
			);
		}
		assert.equal(
			recoverySurfaceSupported({
				...full,
				getRecoveryCapabilities: () => ({ ...caps, version: 99 }),
			}),
			false,
		);
		assert.equal(
			recoverySurfaceSupported({
				...full,
				getRecoveryCapabilities: () => {
					throw new Error('old pin');
				},
			}),
			false,
		);
	});

	it('parseRetryUpstreamCommand is schema-faithful with character lengths', () => {
		assert.ok(parseRetryUpstreamCommand(retryCmd()));
		assert.equal(parseRetryUpstreamCommand(retryCmd({ extra: 1 })), null);
		assert.equal(parseRetryUpstreamCommand(retryCmd({ version: 2 })), null);
		assert.equal(parseRetryUpstreamCommand(retryCmd({ stalledAttemptEpoch: 0 })), null);
		assert.ok(parseRetryUpstreamCommand(retryCmd({ requestId: '\u{1F600}'.repeat(65) })));
		assert.equal(parseRetryUpstreamCommand(retryCmd({ requestId: '\u{1F600}'.repeat(129) })), null);
	});
});

describe('recovery coordinator: review-round regressions', () => {
	it('model progress resets the streak (generation-fenced anchor advance)', async () => {
		const h = makeHarness();
		h.coord.handleClientConnected();
		h.setupOk(1, 1);
		h.advance(10_000);
		const speechAt = h.nowOf();
		h.advance(20_000);
		h.tick({ lastAboveFloorAt: speechAt }); // streak 1
		h.advance(30_000);
		h.tick({ lastAboveFloorAt: speechAt }); // streak 2
		h.coord.handleModelEvent(1); // model spoke — anchor advances, streak 0
		h.advance(30_000);
		h.tick({ lastAboveFloorAt: speechAt }); // would have been the firing tick
		assert.equal(h.restarts.length, 0);
		// A stale-generation model event must NOT touch the anchor: rebuild the
		// streak and confirm the fire still happens on schedule.
		h.advance(30_000);
		h.tick({ lastAboveFloorAt: speechAt });
		h.coord.handleModelEvent(99); // unknown/stale generation — record-only
		h.advance(30_000);
		h.tick({ lastAboveFloorAt: speechAt });
		assert.equal(h.restarts.length, 1);
	});

	it('an admitted successor generation prunes stranded tools; their late completions are inert', async () => {
		const h = makeHarness();
		h.coord.handleClientConnected();
		h.setupOk(1, 1);
		h.coord.noteToolCall('t1', 'inline'); // issued under generation 1
		h.setupOk(1, 2); // bodhi-internal reconnect: ordinary activation, gen 2
		// The stranded generation-1 tool must not veto recovery forever…
		assert.equal(h.coord.pendingToolCount, 0);
		const anchorBefore = h.coord.state.silenceAnchorAt;
		h.advance(5_000);
		h.coord.noteToolSettled('t1'); // its late completion is inert
		assert.equal(h.coord.state.silenceAnchorAt, anchorBefore); // anchor untouched
		// …and a CURRENT-generation tool with the same reused id keeps its
		// veto, settling only on its own completion (which advances the anchor).
		h.coord.noteToolCall('t1', 'inline'); // generation 2 reuses the id
		assert.equal(h.coord.pendingToolCount, 1);
		h.advance(5_000);
		h.coord.noteToolSettled('t1');
		assert.equal(h.coord.pendingToolCount, 0);
		assert.equal(h.coord.state.silenceAnchorAt, h.nowOf()); // anchor advanced
	});

	it('an accepted ack survives unbounded non-accepted churn (dedup never forgets accepts)', async () => {
		const h = makeHarness();
		await driveToTerminal(h);
		h.advance(120_000);
		h.coord.handleClientCommand(retryCmd({ requestId: 'req-accepted' }));
		const accepted = h.sent.filter((m) => m.type === 'voice.retryUpstream.ack')[0];
		assert.equal(accepted.disposition, 'accepted');
		for (let i = 0; i < 70; i += 1) {
			h.coord.handleClientCommand(
				retryCmd({ voiceSessionId: 'someone-else', requestId: `noise-${i}` }),
			);
		}
		const restartsBefore = h.restarts.length;
		h.coord.handleClientCommand(retryCmd({ requestId: 'req-accepted' }));
		assert.equal(h.restarts.length, restartsBefore); // never redials twice
		const replays = h.sent.filter(
			(m) => m.type === 'voice.retryUpstream.ack' && m.requestId === 'req-accepted',
		);
		assert.equal(replays.length, 2);
		assert.deepEqual(replays[1], replays[0]);
	});

	it('stop() is terminal: late rejections, lifecycle events and commands are inert', async () => {
		const h = makeHarness();
		await fireOnce(h);
		h.coord.stop();
		h.restarts[0].reject(new Error('late dial failure'));
		await h.flush();
		assert.equal(h.coord.phase, 'restarting'); // frozen, not waiting-retry
		assert.equal(h.timers.filter((t) => !t.cleared && !t.fired).length, 0);
		h.coord.handleLifecycleEvent({ kind: 'generation-close', transportGeneration: 1 });
		h.coord.handleClientCommand(retryCmd());
		assert.equal(h.sent.filter((m) => m.type === 'voice-stalled').length, 0);
		assert.equal(h.sent.filter((m) => m.type === 'voice.retryUpstream.ack').length, 0);
	});

	it('first tick of a fresh epoch with positive PLAYBACK counters counts as delivery', async () => {
		const h = makeHarness();
		await driveToTerminal(h);
		// Baseline was epoch null; the new epoch resets counters from zero and
		// already shows client-confirmed playback — the response landed before
		// this tick.
		h.tick({ delivered: { epoch: 9, chunksEnded: 1, egressFrames: 1, heartbeatSeen: true } });
		assert.equal(h.coord.phase, 'idle');
		const rows = h.rows.filter((r) => r.row === 'first-user-visible-response');
		assert.equal(rows.length, 1);
	});

	it('server egress without heartbeat confirmation NEVER clears terminal (sent is not delivered)', async () => {
		const h = makeHarness();
		await driveToTerminal(h);
		h.tick({ delivered: { epoch: 9, chunksEnded: 0, egressFrames: 5, heartbeatSeen: false } });
		assert.equal(h.coord.phase, 'terminal');
		h.tick({ delivered: { epoch: 9, chunksEnded: 0, egressFrames: 9, heartbeatSeen: false } });
		assert.equal(h.coord.phase, 'terminal');
		assert.equal(h.coord.state.episodeAttempts, 3); // budget untouched
	});

	it('ownsRecovery spans the whole episode, not just terminal', async () => {
		const h = makeHarness();
		assert.equal(h.coord.ownsRecovery, false);
		await fireOnce(h);
		assert.equal(h.coord.ownsRecovery, true); // restarting
		h.setupOk(h.restarts[0].dialGen, 2);
		assert.equal(h.coord.phase, 'idle');
		assert.equal(h.coord.ownsRecovery, true); // recovered origin retained
		// Functional recovery (client-confirmed playback) releases ownership.
		h.tick({ delivered: { epoch: 3, chunksEnded: 2, egressFrames: 2, heartbeatSeen: true } });
		assert.equal(h.coord.ownsRecovery, false);
	});

	it('a stopped coordinator owns nothing, even mid-episode', async () => {
		const h = makeHarness();
		await fireOnce(h);
		assert.equal(h.coord.ownsRecovery, true, 'precondition: an episode is in flight');
		// stop() today is only reached from shutdown(), which exits the process --
		// so this is latent. Any other caller would leave legacy F5 redial stood
		// down (voice-agent.ts) against a coordinator that can no longer dial,
		// and neither side would recover the session.
		h.coord.stop();
		assert.equal(h.coord.ownsRecovery, false, 'stopped must release ownership');
	});
});
