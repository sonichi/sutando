/**
 * Voice metrics disconnect test — verifies that metrics are (or aren't)
 * written when a client disconnects vs when a session ends.
 *
 * Reproduces the bug: bodhi's handleClientDisconnected() doesn't trigger
 * onSessionEnd, so writeVoiceMetrics() never fires on client disconnect.
 */
import { describe, it, expect, beforeEach } from 'vitest';

// Mirror the voice agent observability state
interface VoiceObservability {
	events: Array<{ event: string; timestamp: string }>;
	toolCalls: Array<{ name: string; durationMs: number; timestamp: string }>;
	transcript: Array<{ role: string; text: string }>;
	sessionStart: number;
	metricsWritten: boolean;
	metricsOutput: any | null; // captures what would be written
}

function createObservability(): VoiceObservability {
	return {
		events: [],
		toolCalls: [],
		transcript: [],
		sessionStart: Date.now(),
		metricsWritten: false,
		metricsOutput: null,
	};
}

// Mirrors writeVoiceMetrics() from voice-agent.ts
function writeVoiceMetrics(obs: VoiceObservability): boolean {
	if (obs.metricsWritten) return false;
	obs.metricsWritten = true;
	obs.metricsOutput = {
		timestamp: new Date().toISOString(),
		sessionId: 'test_session',
		source: 'voice',
		durationMs: Date.now() - obs.sessionStart,
		transcriptLines: obs.transcript.length,
		toolCalls: obs.toolCalls,
		toolCount: obs.toolCalls.length,
		events: obs.events,
	};
	return true;
}

// Mirrors onSessionStart hook
function onSessionStart(obs: VoiceObservability) {
	obs.sessionStart = Date.now();
	obs.metricsWritten = false;
	obs.events.length = 0;
	obs.toolCalls.length = 0;
	obs.transcript.length = 0;
	obs.events.push({ event: 'session_started', timestamp: new Date().toISOString() });
}

// Mirrors onSessionEnd hook
function onSessionEnd(obs: VoiceObservability, reason: string) {
	obs.events.push({ event: `session_ended:${reason}`, timestamp: new Date().toISOString() });
	writeVoiceMetrics(obs);
}

// Mirrors bodhi's handleClientDisconnected — just sets a flag, no hooks
function handleClientDisconnected(_obs: VoiceObservability) {
	// This is all bodhi does — no onSessionEnd, no event, nothing
	// _clientConnected = false;
}

// Proposed fix: also flush metrics on client disconnect
function handleClientDisconnectedFixed(obs: VoiceObservability) {
	obs.events.push({ event: 'client_disconnected', timestamp: new Date().toISOString() });
	writeVoiceMetrics(obs);
}

describe('Voice metrics on disconnect', () => {
	let obs: VoiceObservability;

	beforeEach(() => {
		obs = createObservability();
		onSessionStart(obs);
		// Simulate some activity
		obs.toolCalls.push({ name: 'describe_screen', durationMs: 1200, timestamp: new Date().toISOString() });
		obs.events.push({ event: 'tool_call:describe_screen', timestamp: new Date().toISOString() });
		obs.transcript.push({ role: 'user', text: 'what am I looking at' });
		obs.transcript.push({ role: 'assistant', text: 'You are looking at a GitHub repo.' });
	});

	it('writes metrics on session end', () => {
		onSessionEnd(obs, 'user_hangup');
		expect(obs.metricsWritten).toBe(true);
		expect(obs.metricsOutput).not.toBeNull();
		expect(obs.metricsOutput.source).toBe('voice');
		expect(obs.metricsOutput.toolCount).toBe(1);
		expect(obs.metricsOutput.transcriptLines).toBe(2);
	});

	it('BUG: does NOT write metrics on client disconnect (current behavior)', () => {
		handleClientDisconnected(obs);
		expect(obs.metricsWritten).toBe(false);
		expect(obs.metricsOutput).toBeNull();
	});

	it('FIX: writes metrics on client disconnect (proposed behavior)', () => {
		handleClientDisconnectedFixed(obs);
		expect(obs.metricsWritten).toBe(true);
		expect(obs.metricsOutput).not.toBeNull();
		expect(obs.metricsOutput.toolCount).toBe(1);
		expect(obs.metricsOutput.events).toContainEqual(
			expect.objectContaining({ event: 'client_disconnected' })
		);
	});

	it('metricsWritten flag prevents duplicate writes', () => {
		handleClientDisconnectedFixed(obs);
		const firstOutput = obs.metricsOutput;
		// Session end after disconnect should not re-write
		const wrote = writeVoiceMetrics(obs);
		expect(wrote).toBe(false);
		expect(obs.metricsOutput).toBe(firstOutput);
	});

	it('new session resets metricsWritten flag', () => {
		onSessionEnd(obs, 'user_hangup');
		expect(obs.metricsWritten).toBe(true);
		// Start a new session
		onSessionStart(obs);
		expect(obs.metricsWritten).toBe(false);
		expect(obs.events).toHaveLength(1);
		expect(obs.events[0].event).toBe('session_started');
	});
});
